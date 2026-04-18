# cogs/images.py
from __future__ import annotations

import discord
from discord.ext import commands
from discord import app_commands

from core.logger import log_cmd, warn
from core.utils import DATA_DIR, load_json, save_json
from core.vault import is_image

PROFILES_FILE = DATA_DIR / "profiles.json"


def load_profiles():
    return load_json(PROFILES_FILE, {})


def save_profiles(data):
    save_json(PROFILES_FILE, data)


class ImagesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings = bot.settings
        self.profiles = load_profiles()
        self.media_channel_id = bot.hot_config["media_channel_id"]

    # =====================================================================
    # Helper: save attachment into media-vault
    # =====================================================================

    async def _save_to_media_vault(
        self,
        guild: discord.Guild,
        attachment: discord.Attachment,
    ) -> tuple[str, int, int]:
        """
        Re-upload the attachment into the media vault channel as a normal message.
        Returns (url, channel_id, message_id).
        """
        channel = guild.get_channel(self.media_channel_id)
        if channel is None:
            channel = await guild.fetch_channel(self.media_channel_id)

        file = await attachment.to_file()
        msg = await channel.send(file=file)

        if not msg.attachments:
            raise RuntimeError("Media vault message has no attachments")

        a = msg.attachments[0]
        return a.url, msg.channel.id, msg.id

    # =====================================================================
    # Listener: auto-tag images when members are mentioned
    # =====================================================================

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.author.bot:
            return

        if not msg.guild:
            return

        guild_id = msg.guild.id
        channel_id = msg.channel.id

        if not self.settings.is_feature_allowed(guild_id, channel_id, "tag_image"):
            return

        for m in msg.mentions:
            pid = str(m.id)
            if pid in self.profiles:
                data = self.profiles[pid]
                url = data.get("image") or data.get("img")
                if not url:
                    continue

                e = discord.Embed(title=data.get("name", m.display_name))
                e.set_image(url=url)

                try:
                    await msg.channel.send(embed=e)
                except discord.HTTPException:
                    await msg.channel.send(f"{data.get('name', m.display_name)}\n{url}")

    # =====================================================================
    # Slash command: /tag_member_image
    # =====================================================================

    @app_commands.command(
        name="tag_member_image",
        description="Save the image to show when a member is mentioned.",
    )
    @app_commands.describe(
        member="Member whose image you want to set",
        img="Upload the image or GIF",
    )
    async def tag_member_image(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        img: discord.Attachment,
    ):
        log_cmd("tag_member_image", interaction)

        # channel lock
        if not self.settings.is_feature_allowed(
            interaction.guild_id, interaction.channel_id, "tag_image"
        ):
            try:
                await interaction.response.send_message(
                    "❌ `/tag_member_image` can only be used in the configured image/tag channel(s).",
                    ephemeral=True,
                )
            except discord.HTTPException as e:
                warn(f"tag_member_image: channel lock send failed: {e!r}")
            return

        # validate file
        if not is_image(img):
            try:
                await interaction.response.send_message(
                    "❌ That file isn't an image (png/jpg/gif/webp).",
                    ephemeral=True,
                )
            except discord.HTTPException as e:
                warn(f"tag_member_image: bad image send failed: {e!r}")
            return

        # Defer ephemerally. If this fails, interaction is already dead → do nothing.
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException as e:
            warn(f"tag_member_image: defer failed (interaction dead?): {e!r}")
            return

        # upload into media-vault
        try:
            if interaction.guild is None:
                raise RuntimeError("This command must be used in a guild.")

            url, ch_id, msg_id = await self._save_to_media_vault(
                interaction.guild,
                img,
            )
        except Exception as e:
            warn(f"tag_member_image: vault error: {e!r}")
            try:
                await interaction.edit_original_response(
                    content=f"⚠️ Failed to save image into media-vault: {e}",
                )
            except discord.HTTPException as ee:
                warn(f"tag_member_image: edit_original_response (error) failed: {ee!r}")
            return

        # store profile info
        self.profiles[str(member.id)] = {
            "name": member.display_name,
            "image": url,
            "vault_channel_id": ch_id,
            "vault_message_id": msg_id,
        }
        save_profiles(self.profiles)

        # final ephemeral confirmation
        try:
            await interaction.edit_original_response(
                content=f"✅ Saved image for **{member.display_name}**.",
            )
        except discord.HTTPException as e:
            warn(f"tag_member_image: edit_original_response (success) failed: {e!r}")


async def setup(bot: commands.Bot):
    if not hasattr(bot, "settings"):
        from core.settings import SettingsManager

        bot.settings = SettingsManager(bot.hot_config)

    cog = ImagesCog(bot)

    guild_obj = discord.Object(id=bot.hot_config["guild_id"])
    for cmd in cog.get_app_commands():
        cmd._guild_ids = {bot.hot_config["guild_id"]}
        cmd.guilds = (guild_obj,)

    await bot.add_cog(cog)
