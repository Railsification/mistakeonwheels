import os
import time

import discord
from discord.ext import commands
from dotenv import load_dotenv

from core.logger import ok, err, info


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

# Manual /sync only.
AUTO_SYNC_ON_STARTUP = False

# Backoff so Railway/container restarts do not hammer Discord login when Cloudflare
# has temporarily rate-limited the bot/IP.
LOGIN_RETRY_BASE_SECONDS = 30
LOGIN_RETRY_MAX_SECONDS = 900


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

    retry_delay = LOGIN_RETRY_BASE_SECONDS

    while True:
        bot = HotBot()
        try:
            bot.run(TOKEN)
            retry_delay = LOGIN_RETRY_BASE_SECONDS
            break
        except KeyboardInterrupt:
            raise
        except discord.HTTPException as e:
            err(f"Discord HTTPException during startup/runtime: {e!r}")
            err(f"Backing off for {retry_delay} seconds before retry.")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, LOGIN_RETRY_MAX_SECONDS)
        except Exception as e:
            err(f"Bot crashed: {e!r}")
            err(f"Backing off for {retry_delay} seconds before retry.")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, LOGIN_RETRY_MAX_SECONDS)


if __name__ == "__main__":
    run_bot_forever()
