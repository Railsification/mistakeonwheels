# core/facts.py
import re
import html
import random

import aiohttp

from .logger import warn

HEADERS = {"User-Agent": "HotBot/1.6 (contact: you@example.com)"}
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=6)

WIKI_SEARCH = "https://en.wikipedia.org/w/rest.php/v1/search/title"
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def first_sentences(text: str, n: int = 2) -> str:
    parts = _SENT_SPLIT.split(text.strip())
    return " ".join(parts[:max(1, n)]).strip()


async def get_random_fact(topic: str, *, max_sentences: int = 2) -> str | None:
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT, headers=HEADERS) as s:
        r = await s.get(WIKI_SEARCH, params={"q": topic, "limit": 20})
        if r.status != 200:
            warn(f"wiki search status {r.status}")
            return None
        data = await r.json()
        pages = data.get("pages") or []
        if not pages:
            return None
        pool = pages[:10]
        for _ in range(6):
            title = random.choice(pool)["title"]
            r2 = await s.get(WIKI_SUMMARY.format(title=title.replace(" ", "_")))
            if r2.status != 200:
                continue
            info = await r2.json()
            if info.get("type") == "disambiguation":
                continue
            extract = (info.get("extract") or "").strip()
            if len(extract) < 40:
                continue
            fact = first_sentences(html.unescape(extract), n=max_sentences)
            if fact:
                return f"**{title}** — {fact}"
    return None
