"""Real web search + image fetch for Nebula Assist's on-screen search cards.

For a general-knowledge question that falls all the way through to the LLM
fallback agent, this module does a real web search (Brave Search API) and a
real image search, validates the candidate images are actually reachable
image files, then asks the fallback agent to write ONE short natural-language
summary from those real results. That summary is used as *both* the spoken
reply and the on-screen description — never two independently-generated
answers that could disagree with each other.

Returns `None` on any failure (no API key configured, the search API errors,
no results, or zero images validate) so the caller can fall back to a normal
free-form LLM answer — this feature is entirely optional and a self-hoster
who never signs up for a Brave Search key just gets voice-only answers,
exactly like before this module existed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

_WEB_URL = "https://api.search.brave.com/res/v1/web/search"
_IMAGE_URL = "https://api.search.brave.com/res/v1/images/search"
_MAX_RESULTS = 3
_SEARCH_TIMEOUT = 8
_IMAGE_CHECK_TIMEOUT = 5


async def async_build_search_screen(
    hass: HomeAssistant,
    query: str,
    api_key: str,
    agent_id: str,
    persona: str,
) -> dict[str, Any] | None:
    """Build a `nebula_screen` search directive, or None to fall back to a
    plain free-form LLM answer (no key, no results, no validated images)."""
    if not api_key:
        return None

    session = async_get_clientsession(hass)
    headers = {"Accept": "application/json", "X-Subscription-Token": api_key}

    web_results = await _web_search(session, query, headers)
    if not web_results:
        return None

    image_candidates = await _image_search(session, query, headers)
    good_images = await _validate_images(session, image_candidates)
    if not good_images:
        # Real search worked but no reachable images — per design, degrade
        # to a plain answer rather than show a card screen with no pictures.
        return None

    results: list[dict[str, Any]] = []
    for i, w in enumerate(web_results[:_MAX_RESULTS]):
        if i >= len(good_images):
            break
        results.append(
            {
                "title": w.get("title") or "",
                "snippet": w.get("description") or "",
                "source_url": w.get("url") or "",
                "image_url": good_images[i],
            }
        )
    if not results:
        return None

    summary = await _summarize(hass, query, results, agent_id, persona)
    if not summary:
        return None

    return {
        "v": 1,
        "type": "search",
        "query": query,
        "description": summary,
        "results": [
            {"title": r["title"], "image_url": r["image_url"], "source_url": r["source_url"]}
            for r in results
        ],
    }


async def _web_search(session, query: str, headers: dict) -> list[dict]:
    try:
        async with asyncio.timeout(_SEARCH_TIMEOUT):
            resp = await session.get(
                _WEB_URL, params={"q": query, "count": 5}, headers=headers
            )
            if resp.status != 200:
                _LOGGER.warning("Nebula search: web search HTTP %s", resp.status)
                return []
            data = await resp.json()
    except Exception:  # noqa: BLE001 - network/timeout/parse, always degrade gracefully
        _LOGGER.warning("Nebula search: web search failed", exc_info=True)
        return []
    return (data.get("web") or {}).get("results") or []


async def _image_search(session, query: str, headers: dict) -> list[str]:
    try:
        async with asyncio.timeout(_SEARCH_TIMEOUT):
            resp = await session.get(
                _IMAGE_URL,
                params={"q": query, "count": _MAX_RESULTS * 2},
                headers=headers,
            )
            if resp.status != 200:
                _LOGGER.info("Nebula search: image search HTTP %s", resp.status)
                return []
            data = await resp.json()
    except Exception:  # noqa: BLE001
        _LOGGER.info("Nebula search: image search failed", exc_info=True)
        return []
    candidates = (data.get("results") or [])[: _MAX_RESULTS * 2]
    urls = []
    for c in candidates:
        url = (c.get("properties") or {}).get("url") or (c.get("thumbnail") or {}).get("src")
        if url:
            urls.append(url)
    return urls


async def _validate_images(session, urls: list[str]) -> list[str]:
    async def check(url: str) -> str | None:
        try:
            async with asyncio.timeout(_IMAGE_CHECK_TIMEOUT):
                resp = await session.head(url, headers={"Accept": "image/*"})
                ctype = resp.headers.get("Content-Type", "")
                if resp.status < 300 and ctype.startswith("image/"):
                    return url
        except Exception:  # noqa: BLE001 - unreachable/timeout/bad host, just skip it
            pass
        return None

    checked = await asyncio.gather(*(check(u) for u in urls))
    return [u for u in checked if u][:_MAX_RESULTS]


async def _summarize(
    hass: HomeAssistant,
    query: str,
    results: list[dict[str, Any]],
    agent_id: str,
    persona: str,
) -> str | None:
    """Ask the fallback agent to turn the real results into ONE short spoken
    summary — this exact string becomes both the TTS speech and the on-screen
    description, so it must read naturally on its own."""
    bullets = "\n".join(
        f"- {r['title']}: {r['snippet']}" for r in results if r.get("title")
    )
    prompt = (
        f"{persona}\n\n"
        "The user asked a question that needed a real web search. Here are "
        f'the top real results for "{query}":\n{bullets}\n\n'
        "Write ONE short, natural, spoken-style summary (2-3 sentences, no "
        "bullet points, no markdown, no preamble like \"here's what I found\") "
        "that answers the question using these results. This exact text will "
        "be spoken aloud AND shown on screen, so it must be fully "
        "self-contained and conversational on its own."
    )
    try:
        result = await conversation.async_converse(
            hass,
            prompt,
            None,
            Context(),
            agent_id=agent_id,
        )
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Nebula search: summary call failed", exc_info=True)
        return None

    if result.response.response_type == intent.IntentResponseType.ERROR:
        return None
    speech = result.response.speech or {}
    text = (speech.get("plain") or {}).get("speech")
    return text.strip() if isinstance(text, str) and text.strip() else None
