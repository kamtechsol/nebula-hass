"""Compound multi-intent command splitting for Nebula Assist.

A single utterance like "turn on the lights and play some jazz" names two
separate device/media commands in one sentence — Home Assistant's built-in
conversation agent only ever matches one sentence template per turn, so on
its own it just fails the whole thing with NO_INTENT_MATCH. This module asks
the fallback LLM to split a likely-compound utterance into one imperative
sentence per command, so each piece can be run back through the built-in
agent individually in `conversation.py` — no new intent-recognition engine,
just reuses HA's own, once per clause.

Returns `None` whenever this isn't actually a multi-command utterance (a
question, a greeting, or a single command that happens to mention "and" —
e.g. "turn on the lights and fan") so the caller falls through to the normal
single-command / conversational handling exactly as before this existed.
"""

from __future__ import annotations

import logging
import re

from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import intent

_LOGGER = logging.getLogger(__name__)

# Cheap pre-filter so the LLM is only asked about utterances that contain
# some conjunction/list signal at all — most turns never touch this, so a
# plain "turn on the lights" never spends an LLM call here. Deliberately
# broad (a lone "and" is enough, even "turn on the lights and fan" which
# isn't really compound) — telling a real multi-command utterance apart from
# a single command that merely mentions "and" is the LLM's job per the
# prompt below, not this regex's; this only exists for cost control.
_COMPOUND_HINT = re.compile(r"\band\b|\bthen\b|,", re.IGNORECASE)

_MAX_PARTS = 5


async def async_split_compound_command(
    hass: HomeAssistant,
    text: str,
    agent_id: str,
) -> list[str] | None:
    """Split `text` into separate imperative commands, or None if it isn't a
    compound multi-command utterance."""
    if not _COMPOUND_HINT.search(text):
        return None

    prompt = (
        "A smart-home voice assistant's built-in command engine can only run "
        "ONE command per sentence. The user said:\n"
        f'"{text}"\n\n'
        "If this names TWO OR MORE separate device/media commands in one "
        'sentence (e.g. "turn on the lights and play some jazz"), reply with '
        "each command as its own short imperative sentence, one per line, "
        "preserving the user's own wording as closely as possible.\n\n"
        "If it is a question, a greeting, or really just ONE command (even "
        'if it mentions multiple things, like "turn on the lights and fan"), '
        "reply with exactly: NONE\n\n"
        "Reply with ONLY the split sentences or NONE — no numbering, no "
        "extra words, no explanation."
    )
    try:
        result = await conversation.async_converse(
            hass, prompt, None, Context(), agent_id=agent_id
        )
    except Exception:  # noqa: BLE001 - degrade to "not compound", never raise
        _LOGGER.info("Nebula multi-intent: split call failed", exc_info=True)
        return None

    if result.response.response_type == intent.IntentResponseType.ERROR:
        return None
    speech = result.response.speech or {}
    raw = (speech.get("plain") or {}).get("speech")
    if not isinstance(raw, str) or not raw.strip():
        return None
    if raw.strip().upper() == "NONE":
        return None

    parts = [p.strip(" -•\t") for p in raw.splitlines()]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        # LLM didn't actually find a second command despite the hint —
        # treat as a single command, not a split.
        return None
    _LOGGER.warning("Nebula multi-intent: split %r -> %r", text, parts)
    return parts[:_MAX_PARTS]
