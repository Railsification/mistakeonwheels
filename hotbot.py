import os
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
        await self.load_extension("cogs.chest_pattern")

        if not GUILD_ID:
            err("GUILD_ID missing or invalid in .env")
            return

        guild = discord.Object(id=GUILD_ID)
        try:
            synced = await self.tree.sync(guild=guild)
            names = ", ".join(sorted(c.name for c in synced))
            ok(f"Synced {len(synced)} commands to guild {GUILD_ID}")
            info(f"Commands: {names}")
        except Exception as e:
            err(f"Command sync failed: {e!r}")

    async def on_ready(self):
        ok(f"HotBot v1.6 started as {self.user}")


bot = HotBot()

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("BOT_TOKEN missing in .env")
    if not GUILD_ID:
        raise SystemExit("GUILD_ID missing or invalid in .env")

    bot.run(TOKEN)
