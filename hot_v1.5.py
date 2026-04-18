# hot_v1.5.py
# pip install -U discord.py python-dotenv aiohttp colorama

import os, time, asyncio, re, html, json, random, io
from pathlib import Path
from datetime import datetime

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv, set_key
from colorama import init as colorama_init, Fore, Style

# ========= Logging =========
colorama_init(autoreset=True)

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")

def ok(msg: str): print(f"[{_ts()}] {Fore.GREEN}[OK]{Style.RESET_ALL} {msg}")
def warn(msg: str): print(f"[{_ts()}] {Fore.YELLOW}[WARN]{Style.RESET_ALL} {msg}")
def err(msg: str): print(f"[{_ts()}] {Fore.RED}[ERR]{Style.RESET_ALL} {msg}")
def info(msg: str): print(f"[{_ts()}] {Fore.CYAN}[INFO]{Style.RESET_ALL} {msg}")

def log_cmd(name: str, interaction: discord.Interaction):
    user = f"{interaction.user} ({interaction.user.id})"
    chan = f"#{interaction.channel}" if interaction.channel else "DM"
    guild = f"{interaction.guild}" if interaction.guild else "DM"
    print(f"[{_ts()}] {Fore.CYAN}[CMD]{Style.RESET_ALL} /{name} by {user} in {chan} ({guild})")

# ========= Env / constants =========
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID_STR = os.getenv("GUILD_ID", "0")

GUILD_ID = int(GUILD_ID_STR) if GUILD_ID_STR.isdigit() else 0
TOPIC = (os.getenv("TOPIC") or "science").strip()
ENV_PATH = Path(".env")
MEDIA_CHANNEL_ID = int(os.getenv("MEDIA_CHANNEL_ID", "0"))  # optional vault channel for images

PROFILES_FILE = "profiles.json"
POLLS_FILE = "polls.json"
SPEECH_FILE = "speech_styles.json"    # speech conversion per user
SETTINGS_FILE = "settings.json"       # per-guild feature/channel config

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1").strip()
PFP_THEME = (os.getenv("PFP_THEME") or "").strip()

SPEECH_FILE = "speech_styles.json"    # speech conversion per user

# ========= Bot setup =========
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ========= JSON helpers =========
def _load_json(path: str, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            warn(f"Could not load {path}: {e!r}")
    return default

def _save_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

profiles: dict[str, dict] = _load_json(PROFILES_FILE, {})
def load_polls(): return _load_json(POLLS_FILE, [])
def save_polls(polls): _save_json(POLLS_FILE, polls)

speech_styles: dict[str, dict] = _load_json(SPEECH_FILE, {})
def save_speech():
    _save_json(SPEECH_FILE, speech_styles)

# Per-guild feature/channel settings
settings: dict[str, dict] = _load_json(SETTINGS_FILE, {})
def save_settings():
    _save_json(SETTINGS_FILE, settings)

def _guild_settings(gid: int) -> dict:
    key = str(gid)
    if key not in settings:
        settings[key] = {"channels": {}}
    # ensure 'channels' is a dict
    if "channels" not in settings[key] or not isinstance(settings[key]["channels"], dict):
        settings[key]["channels"] = {}
    return settings[key]

def get_feature_channels(gid: int, feature: str) -> list[int]:
    g = _guild_settings(gid)
    channels = g["channels"].get(feature)
    if not isinstance(channels, list):
        channels = []
        g["channels"][feature] = channels
    return channels

def is_feature_allowed(gid: int | None, channel_id: int | None, feature: str) -> bool:
    if gid is None or channel_id is None:
        return False
    chans = get_feature_channels(gid, feature)
    if not chans:
        # locked: feature disabled until at least one channel is added
        return False
    return channel_id in chans

_active_poll_tasks: dict[str, asyncio.Task] = {}

# ========= Utilities =========
def humanize_secs(s: int) -> str:
    s = max(0, int(s))
    d, s = divmod(s, 86400); h, s = divmod(s, 3600); m, s = divmod(s, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s or not parts: parts.append(f"{s}s")
    return " ".join(parts)

def parse_timer(timer_str: str) -> int:
    m = re.fullmatch(r"(\d+)([smhdSMHD]?)", timer_str.strip())
    if not m:
        raise ValueError("Use formats like 30s, 5m, 2h, or 1d.")
    val = int(m.group(1))
    unit = (m.group(2) or "s").lower()
    return val if unit == "s" else val*60 if unit == "m" else val*3600 if unit == "h" else val*86400

def _is_image(att: discord.Attachment) -> bool:
    if att.content_type:
        return att.content_type.startswith("image/")
    return att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))

async def safe_ack(inter: discord.Interaction, content: str):
    try:
        if not inter.response.is_done():
            await inter.response.send_message(content, ephemeral=True)
            return
    except Exception:
        pass
    try:
        await inter.followup.send(content, ephemeral=True)
    except Exception:
        pass

async def ensure_deferred(interaction: discord.Interaction, *, ephemeral: bool = False):
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral, thinking=True)
    except Exception as e:
        warn(f"ensure_deferred failed: {e!r}")

async def _persist_attachment_silent(guild: discord.Guild, att: discord.Attachment) -> tuple[str, int, int]:
    """
    Upload the attachment to the media channel, get a durable CDN URL,
    then delete the upload message after a short delay so nobody sees it.
    Returns (url, channel_id, message_id).
    """
    chan = None
    if MEDIA_CHANNEL_ID:
        chan = guild.get_channel(MEDIA_CHANNEL_ID)
    if not chan:
        # fallback: first writable text channel with files allowed
        for c in guild.text_channels:
            perms = c.permissions_for(guild.me)
            if perms.send_messages and perms.attach_files:
                chan = c
                break
    if not chan:
        raise RuntimeError("No channel where I can upload files (need Send Messages + Attach Files).")

    # Upload to Discord CDN
    data = await att.read()
    filename = att.filename or "image.png"
    msg = await chan.send(file=discord.File(io.BytesIO(data), filename=filename))

    if not msg.attachments:
        raise RuntimeError("Upload succeeded but message has no attachments.")
    durable_url = msg.attachments[0].url

    # Delete message after a second (to avoid visible notification)
    async def _cleanup():
        await asyncio.sleep(1)
        try:
            await msg.delete()
        except Exception as e:
            print(f"[vault delete warn] {e!r}")

    asyncio.create_task(_cleanup())
    return durable_url, chan.id, msg.id

# ========= Wikipedia fact fetcher =========
HEADERS = {"User-Agent": "HotBot/1.5 (contact: you@example.com)"}
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=6)
WIKI_SEARCH  = "https://en.wikipedia.org/w/rest.php/v1/search/title"
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")

def first_sentences(text: str, n: int = 2) -> str:
    parts = _SENT_SPLIT.split(text.strip())
    return " ".join(parts[:max(1, n)]).strip()

async def get_random_fact(topic: str, *, max_sentences: int = 2) -> str | None:
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT, headers=HEADERS) as s:
        r = await s.get(WIKI_SEARCH, params={"q": topic, "limit": 20})
        if r.status != 200: return None
        data = await r.json()
        pages = data.get("pages") or []
        if not pages: return None
        pool = pages[:10]
        for _ in range(6):
            title = random.choice(pool)["title"]
            r2 = await s.get(WIKI_SUMMARY.format(title=title.replace(" ", "_")))
            if r2.status != 200: continue
            info = await r2.json()
            if info.get("type") == "disambiguation": continue
            extract = (info.get("extract") or "").strip()
            if len(extract) < 40: continue
            fact = first_sentences(html.unescape(extract), n=max_sentences)
            if fact: return f"**{title}** — {fact}"
    return None

# ========= Poll storage helpers =========
def _guild_polls(gid: int) -> list[dict]:
    return [p for p in load_polls() if p.get("guild_id") == gid]

def _save_without(pid: str):
    polls = load_polls()
    polls = [p for p in polls if p["id"] != pid]
    save_polls(polls)

# ========= Poll scheduler/finalize =========
async def _finalize_poll(record: dict):
    guild = bot.get_guild(record["guild_id"])
    if not guild:
        return
    channel = bot.get_channel(record["channel_id"]) or await bot.fetch_channel(record["channel_id"])
    msgs = []
    for mid in record["message_ids"]:
        try:
            msgs.append(await channel.fetch_message(mid))
        except Exception:
            pass
    emojis = record["emoji_list"]
    def count(msg, e): return next((max(0, r.count - 1) for r in msg.reactions if str(r.emoji) == e), 0)
    counts = [count(msgs[i], emojis[i]) if i < len(msgs) else 0 for i in range(len(emojis))]
    top = max(counts) if counts else 0
    winners = [i for i, v in enumerate(counts) if v == top]
    def bar(v, vmax, w=14): return "█" * max(0, round(w * (v / vmax))) if vmax else ""
    title = "🧐 No votes." if top == 0 else f"🤝 Tie! {top} each." if len(winners) > 1 else f"🏆 Winner {emojis[winners[0]]} ({top})"
    emb = discord.Embed(title="📊 Poll Results", description=title)
    for i, v in enumerate(counts):
        emb.add_field(name=f"{emojis[i]} Option {i+1}", value=f"`{bar(v, top)}` {v}", inline=False)
    await channel.send(embed=emb)
    if top > 0:
        for i in winners:
            if i < len(record.get("attachment_urls", [])):
                await channel.send(record["attachment_urls"][i])
    _save_without(record["id"])
    t = _active_poll_tasks.pop(record["id"], None)
    if t: t.cancel()

async def _schedule_poll(record: dict):
    delay = max(0, record["end_ts"] - time.time())
    await asyncio.sleep(delay)
    await _finalize_poll(record)

# ========= on_ready =========
@bot.event
async def on_ready():
    ok(f"HotBot v1.5 started as {bot.user}")
    guild = discord.Object(id=GUILD_ID)
    synced = await tree.sync(guild=guild)
    info(f"Synced {len(synced)} cmds: {[c.name for c in synced]}")
    # resume active polls
    for rec in load_polls():
        if rec.get("end_ts", 0) > time.time():
            _active_poll_tasks[rec["id"]] = bot.loop.create_task(_schedule_poll(rec))
    ok("Resumed active polls if any.")

# ========= Events =========
async def post_join_fact(member: discord.Member):
    """Post a join fact only in channels allowed for the 'join_fact' feature."""
    guild = member.guild
    if not guild:
        return

    ch: discord.abc.MessageableChannel | None = None
    # Prefer explicitly configured join_fact channels
    chan_ids = get_feature_channels(guild.id, "join_fact")
    for cid in chan_ids:
        c = guild.get_channel(cid)
        if c and c.permissions_for(guild.me).send_messages:
            ch = c
            break

    # If no configured/usable channel, do nothing (feature locked)
    if not ch:
        return

    intro = f"👋 Welcome {member.mention}! Here's a random **{TOPIC}** fact:"
    try:
        fact = await get_random_fact(TOPIC)
        await ch.send(f"{intro}\n📘 {fact or '😕 None found.'}")
    except Exception as e:
        err(f"join fact: {e!r}")

@bot.event
async def on_member_join(member: discord.Member):
    if not member.bot:
        await post_join_fact(member)

@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot:
        return

    # Determine guild for feature checks
    guild = msg.guild

    # Tag images when a mentioned member has a saved profile (only in allowed channels)
    if guild and is_feature_allowed(guild.id, msg.channel.id, "tag_image"):
        for m in msg.mentions:
            pid = str(m.id)
            if pid in profiles:
                d = profiles[pid]
                url = d.get("image") or d.get("img")
                if url:
                    e = discord.Embed(title=d.get("name", m.display_name))
                    e.set_image(url=url)
                    try:
                        await msg.channel.send(embed=e)
                    except discord.HTTPException:
                        await msg.channel.send(f"{d.get('name', m.display_name)}\n{url}")

    # Speech conversion, if configured for this author and channel
    if guild and is_feature_allowed(guild.id, msg.channel.id, "speech"):
        cfg = speech_styles.get(str(msg.author.id))
        if cfg and cfg.get("enabled", True) and OPENAI_API_KEY:
            # don't touch bot commands or very short content
            content = msg.content or ""
            if content and not content.startswith(("/", "!")) and len(content.strip()) >= 3:
                style = cfg.get("style", "").strip()
                if style:
                    from openai import AsyncOpenAI
                    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
                    try:
                        completion = await client.chat.completions.create(
                            model=OPENAI_MODEL,
                            messages=[
                                {"role": "system", "content": f"Rewrite the user's message in this style: {style}"},
                                {"role": "user", "content": content},
                            ],
                            max_tokens=200,
                        )
                        styled = (completion.choices[0].message.content or "").strip()
                    except Exception as e:
                        err(f"speech OpenAI error: {e!r}")
                        styled = None

                    if styled and styled != content.strip():
                        try:
                            await msg.reply(styled[:2000])
                        except Exception as e:
                            err(f"speech reply error: {e!r}")

    await bot.process_commands(msg)

# ========= Commands =========

# --- join fact topic ---
@tree.command(
    name="join_fact_topic_set",
    description="Set topic for random join facts",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(topic="New topic for random facts when someone joins")
async def join_fact_topic_set(inter: discord.Interaction, topic: str):
    log_cmd("join_fact_topic_set", inter)
    await ensure_deferred(inter, ephemeral=True)
    new = topic.strip()
    set_key(str(ENV_PATH), "TOPIC", new)
    global TOPIC
    TOPIC = new
    await inter.followup.send(f"✅ Topic set to **{TOPIC}**", ephemeral=True)

@tree.command(
    name="join_fact_topic_check",
    description="Check the current join fact topic",
    guild=discord.Object(id=GUILD_ID),
)
async def join_fact_topic_check(inter: discord.Interaction):
    log_cmd("join_fact_topic_check", inter)
    await ensure_deferred(inter, ephemeral=True)
    await inter.followup.send(f"Current topic: **{TOPIC}**", ephemeral=True)

# --- fact command ---
@tree.command(
    name="fact",
    description="Get a random fact",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(topic="Topic (defaults to current join topic)")
async def fact(inter: discord.Interaction, topic: str | None = None):
    log_cmd("fact", inter)
    await ensure_deferred(inter)
    topic = (topic or TOPIC or "science").strip()
    fact_txt = await get_random_fact(topic)
    await inter.followup.send(f"📘 Random fact about **{topic}**:\n{fact_txt or '😕 None found.'}")

# --- image poll ---
@tree.command(
    name="image_poll",
    description="Vote between 2–5 images",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(
    message="Poll text",
    img_1="Image 1",
    img_2="Image 2",
    img_3="(Optional) Image 3",
    img_4="(Optional) Image 4",
    img_5="(Optional) Image 5",
    timer="Duration (e.g. 30s, 5m, 1h)",
)
async def image_poll(
    inter: discord.Interaction,
    message: str,
    img_1: discord.Attachment,
    img_2: discord.Attachment,
    img_3: discord.Attachment | None = None,
    img_4: discord.Attachment | None = None,
    img_5: discord.Attachment | None = None,
    timer: str = "30s",
):
    log_cmd("image_poll", inter)
    atts = [a for a in (img_1, img_2, img_3, img_4, img_5) if a]
    if len(atts) < 2:
        await inter.response.send_message("❌ Need at least 2 images.", ephemeral=True)
        return
    try:
        dur = parse_timer(timer)
    except Exception as e:
        await inter.response.send_message(f"❌ {e}", ephemeral=True)
        return

    await safe_ack(inter, "Starting poll…")

    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    posts: list[discord.Message] = []
    urls: list[str] = []
    for i, a in enumerate(atts):
        emb = discord.Embed(title=f"Option {emojis[i]}", description=message)
        emb.set_image(url=a.url)
        m = await inter.channel.send(embed=emb)
        await m.add_reaction(emojis[i])
        posts.append(m)
        urls.append(a.url)

    await inter.channel.send(f"🗳️ Voting started! Ends in {timer.lower()}.")

    pid = f"{inter.guild_id}-{int(time.time())}-{random.randint(1000, 9999)}"
    rec = {
        "id": pid,
        "guild_id": inter.guild_id,
        "channel_id": inter.channel_id,
        "message_ids": [m.id for m in posts],
        "emoji_list": emojis[:len(atts)],
        "end_ts": time.time() + dur,
        "title": message,
        "attachment_urls": urls,
    }
    polls = load_polls()
    polls.append(rec)
    save_polls(polls)
    _active_poll_tasks[pid] = bot.loop.create_task(_schedule_poll(rec))
    await inter.followup.send("Poll active — use `/poll_list` or `/poll_cancel`.", ephemeral=True)

@tree.command(
    name="poll_list",
    description="List active polls",
    guild=discord.Object(id=GUILD_ID),
)
async def poll_list(inter: discord.Interaction):
    log_cmd("poll_list", inter)
    await ensure_deferred(inter, ephemeral=True)
    polls = _guild_polls(inter.guild_id)
    if not polls:
        await inter.followup.send("No active polls.", ephemeral=True)
        return
    now = time.time()
    lines = [
        f"{p['id'][-6:]} | <#{p['channel_id']}> | {len(p['emoji_list'])} opts | "
        f"ends in {humanize_secs(int(p['end_ts'] - now))}"
        for p in polls
    ]
    await inter.followup.send("Active polls:\n```\n" + "\n".join(lines) + "\n```", ephemeral=True)

@tree.command(
    name="poll_cancel",
    description="Cancel or end a poll",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(
    poll_id="Full id or last 6 chars shown in /poll_list",
    finalize_now="Post results now (True) or just cancel (False)",
)
async def poll_cancel(
    inter: discord.Interaction,
    poll_id: str,
    finalize_now: bool = True,
):
    log_cmd("poll_cancel", inter)
    await ensure_deferred(inter, ephemeral=True)
    polls = _guild_polls(inter.guild_id)
    m = [p for p in polls if p["id"] == poll_id or p["id"].endswith(poll_id)]
    if not m:
        await inter.followup.send("No poll match.", ephemeral=True)
        return
    rec = m[0]
    pid = rec["id"]
    t = _active_poll_tasks.pop(pid, None)
    if t:
        t.cancel()
    if finalize_now:
        await _finalize_poll(rec)
        await inter.followup.send(f"Poll {pid[-6:]} ended.", ephemeral=True)
    else:
        _save_without(pid)
        await inter.followup.send(f"Poll {pid[-6:]} cancelled.", ephemeral=True)

# --- Feature channel management ---
_FEATURE_CHOICES = [
    app_commands.Choice(name="Speech rewriter", value="speech"),
    app_commands.Choice(name="Tagged member images", value="tag_image"),
    app_commands.Choice(name="PFP generator", value="pfp"),
    app_commands.Choice(name="Join facts", value="join_fact"),
]

@tree.command(
    name="feature_channel_add",
    description="Allow a feature to run in a specific channel",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(
    feature="Which feature you want to allow in a channel",
    channel="Channel to enable the feature in",
)
@app_commands.choices(feature=_FEATURE_CHOICES)
async def feature_channel_add(
    inter: discord.Interaction,
    feature: app_commands.Choice[str],
    channel: discord.TextChannel,
):
    log_cmd("feature_channel_add", inter)
    if not inter.user.guild_permissions.manage_guild:
        await inter.response.send_message(
            "❌ You need **Manage Server** to change feature channels.",
            ephemeral=True,
        )
        return

    await ensure_deferred(inter, ephemeral=True)

    gid = inter.guild_id
    if gid is None:
        await inter.followup.send("This command can only be used in a server.", ephemeral=True)
        return

    chans = get_feature_channels(gid, feature.value)
    if channel.id not in chans:
        chans.append(channel.id)
        save_settings()

    await inter.followup.send(
        f"✅ Feature **{feature.name}** is now allowed in {channel.mention}.",
        ephemeral=True,
    )

@tree.command(
    name="feature_channel_remove",
    description="Remove a feature from a specific channel",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(
    feature="Which feature you want to restrict",
    channel="Channel to disallow the feature in",
)
@app_commands.choices(feature=_FEATURE_CHOICES)
async def feature_channel_remove(
    inter: discord.Interaction,
    feature: app_commands.Choice[str],
    channel: discord.TextChannel,
):
    log_cmd("feature_channel_remove", inter)
    if not inter.user.guild_permissions.manage_guild:
        await inter.response.send_message(
            "❌ You need **Manage Server** to change feature channels.",
            ephemeral=True,
        )
        return

    await ensure_deferred(inter, ephemeral=True)

    gid = inter.guild_id
    if gid is None:
        await inter.followup.send("This command can only be used in a server.", ephemeral=True)
        return

    chans = get_feature_channels(gid, feature.value)
    if channel.id in chans:
        chans.remove(channel.id)
        save_settings()

    await inter.followup.send(
        f"✅ Feature **{feature.name}** is no longer allowed in {channel.mention}.",
        ephemeral=True,
    )

@tree.command(
    name="feature_channels",
    description="List which channels each feature is allowed in",
    guild=discord.Object(id=GUILD_ID),
)
async def feature_channels(inter: discord.Interaction):
    log_cmd("feature_channels", inter)
    await ensure_deferred(inter, ephemeral=True)

    gid = inter.guild_id
    if gid is None:
        await inter.followup.send("This command can only be used in a server.", ephemeral=True)
        return

    gconf = _guild_settings(gid)
    chan_map = gconf.get("channels", {})

    if not chan_map:
        await inter.followup.send(
            "No feature channels configured yet.\n"
            "Use `/feature_channel_add` to enable features in specific channels.",
            ephemeral=True,
        )
        return

    lines = ["__**Feature Channel Configuration**__"]
    feature_labels = {
        "speech": "Speech rewriter",
        "tag_image": "Tagged member images",
        "pfp": "PFP generator",
        "join_fact": "Join facts",
    }

    for key, label in feature_labels.items():
        ids = chan_map.get(key) or []
        if not ids:
            lines.append(f"• **{label}** — *(no channels; disabled)*")
        else:
            mentions = ", ".join(f"<#{cid}>" for cid in ids)
            lines.append(f"• **{label}** — {mentions}")

    await inter.followup.send("\n".join(lines), ephemeral=True)

# --- Member image tagging ---
@tree.command(
    name="tag_member_image",
    description="Save the image to show when a member is mentioned.",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(member="Member whose image you want to set", img="Upload the image or GIF")
async def tag_member_image(
    interaction: discord.Interaction,
    member: discord.Member,
    img: discord.Attachment,
):
    # Quiet ephemeral ack
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"⏳ Saving image for **{member.display_name}**…",
                ephemeral=True,
            )
    except Exception:
        pass

    if not _is_image(img):
        # Prefer editing the ephemeral ack; fallback only if needed
        try:
            msg = await interaction.original_response()
            await msg.edit(content="❌ That file isn't an image (png/jpg/gif/webp).")
        except Exception:
            try:
                await interaction.followup.send(
                    "❌ That file isn't an image (png/jpg/gif/webp).",
                    ephemeral=True,
                )
            except Exception:
                await interaction.channel.send("❌ That file isn't an image (png/jpg/gif/webp).")
        return

    try:
        # re-host to get a durable URL (silent vault)
        url, ch_id, msg_id = await _persist_attachment_silent(interaction.guild, img)

        profiles[str(member.id)] = {
            "name": member.display_name,
            "image": url,                # durable
            "vault_channel_id": ch_id,   # for future cleanup/replace
            "vault_message_id": msg_id,
        }
        save_polls  # just to keep lints quiet if imported elsewhere
        _ = save_polls

        # Save profiles
        _save_json(PROFILES_FILE, profiles)

        try:
            m = await interaction.original_response()
            await m.edit(content=f"✅ Saved image for **{member.display_name}**.")
        except Exception:
            await interaction.followup.send(
                f"✅ Saved image for **{member.display_name}**.",
                ephemeral=True,
            )
    except Exception as e:
        # last-resort error surface
        try:
            m = await interaction.original_response()
            await m.edit(content=f"⚠️ Failed to save image: {e}")
        except Exception:
            try:
                await interaction.followup.send(
                    f"⚠️ Failed to save image: {e}",
                    ephemeral=True,
                )
            except Exception:
                await interaction.channel.send(f"⚠️ Failed to save image: {e}")

# --- Speech style commands ---
@tree.command(
    name="speech_convert",
    description="Set or update how I rewrite your messages",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(style="Describe the style, e.g. 'in pirate speak', 'formal', etc.")
async def speech_convert(
    interaction: discord.Interaction,
    style: str,
):
    log_cmd("speech_convert", interaction)
    await ensure_deferred(interaction, ephemeral=True)

    uid = str(interaction.user.id)
    cfg = speech_styles.get(uid, {})
    cfg["style"] = style.strip()
    cfg["enabled"] = True
    speech_styles[uid] = cfg
    save_speech()

    await interaction.followup.send(
        f"✅ I'll try to rewrite your messages in this style:\n> **{style.strip()}**",
        ephemeral=True,
    )

@tree.command(
    name="speech_toggle",
    description="Enable or disable speech rewriting for yourself",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(enabled="True = on, False = off")
async def speech_toggle(
    interaction: discord.Interaction,
    enabled: bool,
):
    log_cmd("speech_toggle", interaction)
    await ensure_deferred(interaction, ephemeral=True)

    uid = str(interaction.user.id)
    cfg = speech_styles.get(uid, {})
    cfg["enabled"] = enabled
    speech_styles[uid] = cfg
    save_speech()

    state = "enabled ✅" if enabled else "disabled ❌"
    await interaction.followup.send(
        f"Speech rewriting is now **{state}** for you.",
        ephemeral=True,
    )

@tree.command(
    name="speech_lookup",
    description="Check your current speech rewrite style & status",
    guild=discord.Object(id=GUILD_ID),
)
async def speech_lookup(interaction: discord.Interaction):
    log_cmd("speech_lookup", interaction)
    await ensure_deferred(interaction, ephemeral=True)

    uid = str(interaction.user.id)
    cfg = speech_styles.get(uid)
    if not cfg:
        await interaction.followup.send("You don't have a speech style set yet.", ephemeral=True)
        return

    style = cfg.get("style", "(none)")
    enabled = cfg.get("enabled", True)
    state = "enabled ✅" if enabled else "disabled ❌"

    await interaction.followup.send(
        f"**Your speech settings**\n"
        f"Style: `{style}`\n"
        f"Status: **{state}**",
        ephemeral=True,
    )

# --- PFP theme & generator ---
@tree.command(
    name="pfp_theme",
    description="Set the global PFP prompt theme",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(theme="Overall style, e.g. 'whiteout survivor in heavy parka'")
async def pfp_theme_cmd(interaction: discord.Interaction, theme: str):
    log_cmd("pfp_theme", interaction)

    # Optional: restrict to people who can manage the server
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "❌ You need **Manage Server** permission to change the PFP theme.",
            ephemeral=True,
        )
        return

    await ensure_deferred(interaction, ephemeral=True)

    new_theme = theme.strip()
    if not new_theme:
        await interaction.followup.send("❌ Theme can't be empty.", ephemeral=True)
        return

    global PFP_THEME
    PFP_THEME = new_theme
    set_key(str(ENV_PATH), "PFP_THEME", PFP_THEME)

    ok(f"PFP theme set to: {PFP_THEME}")
    await interaction.followup.send(
        f"✅ PFP theme set to:\n> **{PFP_THEME}**\n\n"
        "All future `/pfp` renders will follow this theme.",
        ephemeral=True,
    )

@tree.command(
    name="pfp",
    description="Generate a themed profile picture",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(
    description="Describe yourself / what you want in the avatar"
)
async def pfp(interaction: discord.Interaction, description: str):
    log_cmd("pfp", interaction)

    # Restrict to allowed PFP channels
    if not interaction.guild or not is_feature_allowed(interaction.guild.id, interaction.channel_id, "pfp"):
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ `/pfp` can only be used in approved PFP channels.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "❌ `/pfp` can only be used in approved PFP channels.",
                    ephemeral=True,
                )
        except Exception:
            pass
        return

    # ephemeral “thinking…” so others don’t see the setup
    await ensure_deferred(interaction, ephemeral=True)

    theme = (PFP_THEME or "cute cartoon character portrait").strip()

    # Small status message (ephemeral)
    await interaction.followup.send(
        f"🖼️ Generating your PFP with theme:\n"
        f"> **{theme}**\n"
        f"and your description:\n"
        f"> **{description}**\n\n"
        f"This can take a few seconds…",
        ephemeral=True,
    )

    if not OPENAI_API_KEY:
        await interaction.followup.send(
            "⚠️ PFP generation is not configured (missing OPENAI_API_KEY).",
            ephemeral=True,
        )
        return

    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=OPENAI_API_KEY)

    prompt = (
        f"{theme}\n\n"
        f"User description: {description}\n\n"
        "Make it an appealing Discord avatar, centered, with clear silhouette."
    )

    try:
        img_resp = await client.images.generate(
            model=OPENAI_IMAGE_MODEL,
            prompt=prompt,
            size="1024x1024",
            n=1,
        )
        b64 = img_resp.data[0].b64_json
        import base64
        raw = base64.b64decode(b64)

        file = discord.File(io.BytesIO(raw), filename="pfp.png")
        await interaction.followup.send(
            "✨ Here is your generated PFP:",
            file=file,
            ephemeral=True,
        )
    except Exception as e:
        err(f"PFP HTTP error: {e!r}")
        await interaction.followup.send(
            "⚠️ Failed to generate PFP (model or org not enabled yet).",
            ephemeral=True,
        )

# --- Utilities / tests / help / sync ---
@tree.command(
    name="hello",
    description="Sanity check",
    guild=discord.Object(id=GUILD_ID),
)
async def hello(inter: discord.Interaction):
    log_cmd("hello", inter)
    await ensure_deferred(inter, ephemeral=True)
    await inter.followup.send("✅ hello works", ephemeral=True)

@tree.command(
    name="acktest",
    description="Check interaction latency",
    guild=discord.Object(id=GUILD_ID),
)
async def acktest(inter: discord.Interaction):
    log_cmd("acktest", inter)
    await ensure_deferred(inter, ephemeral=True)
    age = (discord.utils.utcnow() - inter.created_at).total_seconds()
    await inter.followup.send(f"Ack OK (age {age:.3f}s)", ephemeral=True)

@tree.command(
    name="test_join_fact",
    description="Simulate join fact for a member",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(member="Member to test with (defaults to you)")
async def test_join_fact(inter: discord.Interaction, member: discord.Member | None = None):
    log_cmd("test_join_fact", inter)
    await ensure_deferred(inter)
    target = member or inter.user
    await post_join_fact(target)
    await inter.followup.send(f"🧪 Posted join fact for {target.mention}.", ephemeral=True)

@tree.command(
    name="help",
    description="List all commands",
    guild=discord.Object(id=GUILD_ID),
)
async def help_cmd(inter: discord.Interaction):
    log_cmd("help", inter)
    await ensure_deferred(inter, ephemeral=True)
    cmds = sorted(tree.get_commands(guild=discord.Object(id=GUILD_ID)), key=lambda c: c.name)
    lines = ["__**Bot Commands**__\n"] + [f"• `/{c.name}` — {c.description}" for c in cmds]
    await inter.followup.send("\n".join(lines), ephemeral=True)

@tree.command(
    name="sync",
    description="Force re-sync",
    guild=discord.Object(id=GUILD_ID),
)
async def sync_cmd(inter: discord.Interaction):
    log_cmd("sync", inter)
    await ensure_deferred(inter, ephemeral=True)
    synced = await tree.sync(guild=discord.Object(id=GUILD_ID))
    await inter.followup.send(
        f"Synced {len(synced)} commands:\n```\n" + ", ".join(c.name for c in synced) + "\n```",
        ephemeral=True,
    )

# ========= Run =========
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("BOT_TOKEN missing")
    bot.run(TOKEN)
