from __future__ import annotations

import os
import random
import time
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from core.command_scope import all_guild_ids
from core.config import load_bot_config
from core.logger import err, info, ok
from core.settings import SettingsManager
from core.storage import ensure_storage_dirs
from core.version import BOT_NAME, BOT_VERSION

try:
    import fcntl  # Linux only; Railway container supports this
except ImportError:  # pragma: no cover
    fcntl = None


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
COGS_DIR = BASE_DIR / "cogs"
CONFIG = load_bot_config()

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

        ensure_storage_dirs()
        self.hot_config = CONFIG.as_hot_config()
        self.settings = SettingsManager(self.hot_config)
        self.version = BOT_VERSION

    def _discover_cogs(self) -> list[str]:
        if not COGS_DIR.exists():
            raise RuntimeError(f"Cog directory not found: {COGS_DIR}")

        preferred_first = ["admin", "help"]
        discovered = [
            path.stem
            for path in sorted(COGS_DIR.glob("*.py"))
            if path.name != "__init__.py" and not path.name.startswith("_")
        ]

        ordered: list[str] = []
        for name in preferred_first:
            if name in discovered:
                ordered.append(name)

        for name in discovered:
            if name not in ordered:
                ordered.append(name)

        return ordered

    async def sync_configured_guilds(self) -> dict[int, list[str]]:
        results: dict[int, list[str]] = {}
        for guild_id in all_guild_ids(self):
            guild = discord.Object(id=guild_id)
            synced = await self.tree.sync(guild=guild)
            results[guild_id] = sorted(c.name for c in synced)
        return results

    async def setup_hook(self):
        loaded_names: list[str] = []
        failed_names: list[str] = []

        for cog_name in self._discover_cogs():
            extension_name = f"cogs.{cog_name}"
            try:
                await self.load_extension(extension_name)
                loaded_names.append(cog_name)
            except Exception as e:
                failed_names.append(cog_name)
                err(f"Failed to load {extension_name}: {e!r}")

        ok(f"Loaded {len(loaded_names)} cog(s)")
        if loaded_names:
            info(f"Cogs: {', '.join(loaded_names)}")
        if failed_names:
            err(f"Failed cog(s): {', '.join(failed_names)}")

        if not CONFIG.all_guild_ids:
            err("No configured guild IDs. Set ADMIN_GUILD_ID and PUBLIC_GUILD_IDS, or fallback GUILD_ID.")
            return

        info(f"Admin guild: {CONFIG.admin_guild_id or 'not set'}")
        info(f"Public guilds: {', '.join(str(x) for x in CONFIG.public_guild_ids) or 'none'}")

        if not CONFIG.auto_sync_on_startup:
            info("Startup auto-sync disabled. Use /council sync from the admin server.")
            return

        try:
            results = await self.sync_configured_guilds()
            for guild_id, names in results.items():
                ok(f"Synced {len(names)} command(s) to guild {guild_id}")
                info(f"Commands for guild {guild_id}: {', '.join(names)}")
        except discord.HTTPException as e:
            err(f"Command sync rate-limited/failed at startup: {e!r}")
        except Exception as e:
            err(f"Command sync failed at startup: {e!r}")

    async def on_ready(self):
        ok(f"{BOT_NAME} v{BOT_VERSION} started as {self.user}")


def run_bot_forever():
    if not CONFIG.token:
        raise SystemExit("BOT_TOKEN missing in .env")
    if not CONFIG.all_guild_ids:
        raise SystemExit("Set ADMIN_GUILD_ID/PUBLIC_GUILD_IDS, or fallback GUILD_ID")

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
                bot.run(CONFIG.token)
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
