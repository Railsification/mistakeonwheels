import os
import time
import random
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from core.logger import ok, err, info

try:
    import fcntl  # Linux only; Railway container supports this
except ImportError:  # pragma: no cover
    fcntl = None


load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID_STR = os.getenv("GUILD_ID", "0")
GUILD_ID = int(GUILD_ID_STR) if GUILD_ID_STR.isdigit() else 0
MEDIA_CHANNEL_ID = int(os.getenv("MEDIA_CHANNEL_ID", "0") or 0)
TOPIC_DEFAULT = (os.getenv("TOPIC") or "science").strip()
PFP_THEME_DEFAULT = (os.getenv("PFP_THEME") or "").strip()
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
OPENAI_IMAGE_MODEL = (os.getenv("OPENAI_IMAGE_MODEL") or "gpt-image-1").strip()

AUTO_SYNC_ON_STARTUP = False

LOGIN_RETRY_BASE_SECONDS = 30
LOGIN_RETRY_MAX_SECONDS = 900

CF1015_FIRST_WAIT_SECONDS = 900
CF1015_MAX_WAIT_SECONDS = 3600

LOCK_DIR = Path("data")
LOCK_FILE = LOCK_DIR / "hotbot.startup.lock"


def _is_cloudflare_1015(exc: Exception) -> bool:
    text = str(exc)
    return (
        "1015" in text
        or "Cloudflare" in text
        or "discord.com used Cloudflare to restrict access" in text
        or "You are being rate limited" in text
    )


class SingleInstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.fp = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = self.path.open("a+")
        if fcntl is None:
            return True
        try:
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fp.seek(0)
            self.fp.truncate()
            self.fp.write(str(os.getpid()))
            self.fp.flush()
            return True
        except BlockingIOError:
            return False

    def release(self) -> None:
        if self.fp is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self.fp.close()
        except Exception:
            pass
        self.fp = None


class HotBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        intents.members = True

        super().__init__(command_prefix="!", intents=intents)

        self.hot_config = {
            "guild_id": GUILD_ID,
            "media_channel_id": MEDIA_CHANNEL_ID,
            "topic_default": TOPIC_DEFAULT,
            "pfp_theme_default": PFP_THEME_DEFAULT,
            "openai_api_key": OPENAI_API_KEY,
            "openai_model": OPENAI_MODEL,
            "openai_image_model": OPENAI_IMAGE_MODEL,
        }

    async def setup_hook(self):
        await self.load_extension("cogs.joins")
        await self.load_extension("cogs.polls")
        await self.load_extension("cogs.images")
        await self.load_extension("cogs.speech")
        await self.load_extension("cogs.pfp")
        await self.load_extension("cogs.admin")
        await self.load_extension("cogs.misc")
        await self.load_extension("cogs.games")
        await self.load_extension("cogs.tictactoe")
        await self.load_extension("cogs.connect4")
        await self.load_extension("cogs.canyon")
        await self.load_extension("cogs.wos_furnace_calc")
        await self.load_extension("chest_pattern")

        loaded = len(list(self.extensions.keys()))
        ok(f"Loaded {loaded} cog(s)")

        if not GUILD_ID:
            err("GUILD_ID missing or invalid in .env")
            return

        if not AUTO_SYNC_ON_STARTUP:
            info("Startup auto-sync disabled. Use /sync manually.")
            return

        guild = discord.Object(id=GUILD_ID)
        try:
            synced = await self.tree.sync(guild=guild)
            names = ", ".join(sorted(c.name for c in synced))
            ok(f"Synced {len(synced)} commands to guild {GUILD_ID}")
            info(f"Commands: {names}")
        except discord.HTTPException as e:
            err(f"Command sync rate-limited/failed at startup: {e!r}")
        except Exception as e:
            err(f"Command sync failed: {e!r}")

    async def on_ready(self):
        ok(f"HotBot v1.6 started as {self.user}")


def run_bot_forever():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN missing in .env")
    if not GUILD_ID:
        raise SystemExit("GUILD_ID missing or invalid in .env")

    lock = SingleInstanceLock(LOCK_FILE)
    if not lock.acquire():
        err("Another HotBot instance already holds the startup/login lock. This instance will stay idle.")
        while True:
            time.sleep(300)

    retry_delay = LOGIN_RETRY_BASE_SECONDS
    cf_retry_delay = CF1015_FIRST_WAIT_SECONDS

    try:
        while True:
            bot = HotBot()
            try:
                bot.run(TOKEN)
                retry_delay = LOGIN_RETRY_BASE_SECONDS
                cf_retry_delay = CF1015_FIRST_WAIT_SECONDS
                break
            except KeyboardInterrupt:
                raise
            except discord.HTTPException as e:
                if _is_cloudflare_1015(e):
                    sleep_for = min(cf_retry_delay + random.randint(0, 60), CF1015_MAX_WAIT_SECONDS)
                    err(f"Discord/Cloudflare 1015 block during login/startup: {e!r}")
                    err(f"Backing off for {sleep_for} seconds before retry.")
                    time.sleep(sleep_for)
                    cf_retry_delay = min(cf_retry_delay * 2, CF1015_MAX_WAIT_SECONDS)
                    retry_delay = LOGIN_RETRY_BASE_SECONDS
                    continue

                err(f"Discord HTTPException during startup/runtime: {e!r}")
                err(f"Backing off for {retry_delay} seconds before retry.")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, LOGIN_RETRY_MAX_SECONDS)
            except Exception as e:
                err(f"Bot crashed: {e!r}")
                err(f"Backing off for {retry_delay} seconds before retry.")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, LOGIN_RETRY_MAX_SECONDS)
    finally:
        lock.release()


if __name__ == "__main__":
    run_bot_forever()
