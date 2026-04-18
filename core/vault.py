# core/vault.py
import io
import asyncio
from typing import Tuple

import discord

from .logger import warn


def is_image(att: discord.Attachment) -> bool:
    if att.content_type:
        return att.content_type.startswith("image/")
    return att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))


async def persist_attachment_silent(
    guild: discord.Guild,
    att: discord.Attachment,
    media_channel_id: int | None,
) -> Tuple[str, int, int]:
    """
    Upload attachment to a vault/media channel, grab a durable CDN URL,
    delete the upload message after a second so it's effectively silent.
    Returns (url, channel_id, message_id).
    """
    chan: discord.TextChannel | None = None

    if media_channel_id:
        ch = guild.get_channel(media_channel_id)
        if isinstance(ch, discord.TextChannel):
            chan = ch

    # fallback: first text channel with send+attach perms
    if chan is None:
        for c in guild.text_channels:
            perms = c.permissions_for(guild.me)
            if perms.send_messages and perms.attach_files:
                chan = c
                break

    if chan is None:
        raise RuntimeError("No channel where I can upload files (need Send Messages + Attach Files).")

    data = await att.read()
    filename = att.filename or "image.png"
    msg = await chan.send(file=discord.File(io.BytesIO(data), filename=filename))

    if not msg.attachments:
        raise RuntimeError("Upload succeeded but message has no attachments.")
    durable_url = msg.attachments[0].url

    async def _cleanup():
        await asyncio.sleep(1)
        try:
            await msg.delete()
        except Exception as e:
            warn(f"[vault delete warn] {e!r}")

    asyncio.create_task(_cleanup())
    return durable_url, chan.id, msg.id
