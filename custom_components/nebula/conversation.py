"""Nebula conversation agent.

`conversation.nebula` — one voice for the home:

1. **Local first.** Every turn is offered to Home Assistant's built-in intent
   agent. If it can act (turn on/off, set, scene, timer, exposed-entity state,
   "what's the weather") the reply comes straight back — fast, offline,
   deterministic. Device control never waits on the cloud.

2. **Hand off for everything else.** Forecasts, general knowledge, "is it gonna
   rain tomorrow", semi-personal questions — anything the built-in agent can't
   match is forwarded to the configured fallback conversation agent (an LLM),
   with a warm "Nebula" persona injected as the system prompt.

3. **Degrade gracefully.** If the fallback is rate-limited / unreachable, retry
   once, then give a short honest reply instead of a raw error — and local
   control still works throughout.

The panel keeps talking to HA's Assist pipeline exactly as before; point the
pipeline's conversation agent at this entity.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import re

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, intent
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import search_intent
from .const import (
    CONF_ASSIST_ENABLED,
    CONF_ASSIST_FALLBACK_AGENT,
    CONF_ASSIST_LOCAL_FIRST,
    CONF_ASSIST_PERSONA,
    CONF_SEARCH_API_KEY,
    DEFAULT_PERSONA,
    DOMAIN,
)
from .device import panel_device_info

_LOGGER = logging.getLogger(__name__)

BUILTIN_AGENT = "conversation.home_assistant"

# Built-in "couldn't do it" outcomes that should fall through to the LLM.
_FALL_THROUGH = {
    intent.IntentResponseErrorCode.NO_INTENT_MATCH,
    intent.IntentResponseErrorCode.NO_VALID_TARGETS,
}

# Fallback-agent error shapes that are worth one retry (transient overload).
_RETRYABLE = re.compile(
    r"overload|high.demand|quota|rate.limit|resource.exhausted|too many requests"
    r"|\b(429|500|502|503|504)\b",
    re.IGNORECASE,
)

_FILLER = re.compile(
    r"^\s*(as an ai(?: language model)?|i'?m (?:just|only) an ai|i am an ai)[,:]?\s*",
    re.IGNORECASE,
)

# Nebula Assist visual responses (see os/COSMOS-UI.md-style plan: "Nebula
# Assist — Visual Response Screens") — cheap regex classification on the raw
# transcript, done here rather than on the panel, since this is the one place
# that already knows which agent is about to answer (or just answered).
_GREETING = re.compile(
    r"^\s*(hi|hey|hello|yo|good (morning|afternoon|evening|night)|"
    r"how(’|'| a)re you|how(’|'| i)s it going|what'?s up|"
    r"thank(s| you)( (very|so) much)?|you'?re welcome|"
    r"good (boy|girl|job)|who are you)\b",
    re.IGNORECASE,
)
_WEATHER = re.compile(
    r"\b(weather|forecast|temperature|rain(ing|y)?|snow(ing|y)?|sunny|cloudy|"
    r"windy|humid(ity)?|how (hot|cold|warm) is it|"
    r"is it (going to|gonna) (rain|snow))\b",
    re.IGNORECASE,
)


def _attach_screen(response: intent.IntentResponse, screen: dict) -> None:
    """Patch this specific response instance so its as_dict() also carries a
    `nebula_screen` directive for the panel. Response objects here are built
    by whichever agent actually answered the turn (the built-in agent for
    weather, the fallback LLM for search/greeting) — there's no single
    construction point in this class to subclass IntentResponse at, so the
    instance's own as_dict is wrapped instead.
    """
    original_as_dict = response.as_dict

    def _as_dict_with_screen():
        d = original_as_dict()
        d.setdefault("data", {})["nebula_screen"] = screen
        return d

    response.as_dict = _as_dict_with_screen  # type: ignore[method-assign]


def _day_label(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.date() == dt_util.now().date():
        return "Today"
    return parsed.strftime("%a")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Nebula conversation agent."""
    if not entry.options.get(CONF_ASSIST_ENABLED, True):
        _LOGGER.debug("Nebula conversation agent disabled by options")
        return
    async_add_entities([NebulaConversationEntity(hass, entry)])


class NebulaConversationEntity(conversation.ConversationEntity):
    """Local-first router with an LLM hand-off and a Nebula persona."""

    _attr_has_entity_name = False
    _attr_name = "Nebula"
    _attr_supported_features = conversation.ConversationEntityFeature.CONTROL

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_conversation"
        self._attr_device_info = panel_device_info()

    @property
    def supported_languages(self) -> list[str] | str:
        # We delegate to sub-agents; accept anything and let them sort it out.
        return MATCH_ALL

    # ---------------------------------------------------------------- options

    def _opt(self, key: str, default):
        return self._entry.options.get(key, default)

    def _persona(self) -> str:
        return str(self._opt(CONF_ASSIST_PERSONA, DEFAULT_PERSONA)).strip()

    def _fallback_agent(self) -> str | None:
        """Configured fallback agent, else deliberately pick the Gemini agent.

        Auto-detect used to mean "whatever non-builtin conversation entity
        happens to exist" — fine when Google Generative AI was the only
        option, but fragile: any other conversation-capable integration
        (another HACS agent, a second LLM) would silently steal the fallback
        slot just by loading first. Prefer a `google_generative_ai_conversation`
        platform entity by name; only fall back to "first other agent found"
        if no Gemini entity exists.
        """
        configured = self._opt(CONF_ASSIST_FALLBACK_AGENT, "")
        if configured:
            return configured

        ent_reg = er.async_get(self.hass)
        candidates = [
            ent
            for ent in ent_reg.entities.values()
            if ent.domain == "conversation"
            and ent.entity_id not in (BUILTIN_AGENT, self.entity_id)
            and not ent.disabled
        ]
        for ent in candidates:
            if ent.platform == "google_generative_ai_conversation":
                return ent.entity_id
        return candidates[0].entity_id if candidates else None

    # ---------------------------------------------------------------- process

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        text = (user_input.text or "").strip()
        if not text:
            return self._say(user_input, "I didn't catch that.")

        local_first = bool(self._opt(CONF_ASSIST_LOCAL_FIRST, True))

        # 1) built-in intent agent — device commands and "what's the weather"
        # both answer here. A weather match gets a real forecast screen
        # attached; anything else (a device command) is untouched, voice-only.
        if local_first:
            local = await self._converse(user_input, BUILTIN_AGENT)
            if local is not None and not self._should_fall_through(local):
                if not self._is_error(local) and _WEATHER.search(text):
                    await self._attach_weather_screen(local)
                return local

        # 2) fallback LLM with the Nebula persona
        agent = self._fallback_agent()
        if not agent:
            _LOGGER.warning("Nebula: no fallback conversation agent available")
            return self._degraded(user_input)

        # 2a) small talk — a client-side-only animation, no backend call
        # beyond the normal Gemini reply itself.
        if _GREETING.search(text):
            llm = await self._converse_with_retry(user_input, agent)
            if llm is not None:
                _attach_screen(llm.response, {"v": 1, "type": "greeting"})
                return self._polish(llm)
            return self._degraded(user_input)

        # 2b) general knowledge / search bucket by elimination. Real search +
        # image fetch, summarized into a single string used as BOTH the
        # spoken reply and the on-screen description — see search_intent.py.
        # Falls through to the normal free-form answer below on any failure
        # (no API key configured, search error, no validated images).
        api_key = self._opt(CONF_SEARCH_API_KEY, "")
        screen = await search_intent.async_build_search_screen(
            self.hass, text, api_key=api_key, agent_id=agent, persona=self._persona()
        )
        if screen is not None:
            resp = intent.IntentResponse(language=user_input.language or "en")
            resp.async_set_speech(screen["description"])
            _attach_screen(resp, screen)
            return conversation.ConversationResult(
                response=resp, conversation_id=user_input.conversation_id
            )

        # 2c) plain free-form answer, exactly as before this feature existed.
        llm = await self._converse_with_retry(user_input, agent)
        if llm is not None:
            return self._polish(llm)

        return self._degraded(user_input)

    async def _converse_with_retry(
        self, user_input: conversation.ConversationInput, agent: str
    ) -> conversation.ConversationResult | None:
        """The fallback-agent call plus the one-retry-on-transient-overload
        behavior, factored out since three call sites need it identically."""
        llm = await self._converse(user_input, agent, persona=self._persona())
        if llm is not None and not self._is_error(llm):
            return llm
        if llm is not None and self._is_retryable(llm):
            _LOGGER.info("Nebula: fallback agent busy, retrying once")
            await asyncio.sleep(1.5)
            llm = await self._converse(user_input, agent, persona=self._persona())
            if llm is not None and not self._is_error(llm):
                return llm
        return None

    async def _attach_weather_screen(
        self, result: conversation.ConversationResult
    ) -> None:
        """Best-effort: pull real multi-day forecast data and attach it as a
        `weather` screen directive. Never raises — a failure here should
        never break the weather answer itself, just leave it voice-only."""
        try:
            entity_ids = self.hass.states.async_entity_ids("weather")
            if not entity_ids:
                return
            entity_id = entity_ids[0]
            forecast = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": "daily"},
                target={"entity_id": entity_id},
                blocking=True,
                return_response=True,
            )
            raw_days = ((forecast or {}).get(entity_id) or {}).get("forecast") or []
            if not raw_days:
                return
            state = self.hass.states.get(entity_id)
            unit = (state.attributes.get("temperature_unit") if state else None) or "°F"
            days = [
                {
                    "label": _day_label(d.get("datetime")),
                    "condition": d.get("condition") or "unknown",
                    "hi": round(d["temperature"]) if d.get("temperature") is not None else None,
                    "lo": round(d["templow"]) if d.get("templow") is not None else None,
                    "precip_pct": d.get("precipitation_probability"),
                }
                for d in raw_days[:7]
            ]
            screen = {
                "v": 1,
                "type": "weather",
                "entity_id": entity_id,
                "location": (state.attributes.get("friendly_name") if state else None)
                or "Home",
                "unit": "F" if "F" in unit else "C",
                "days": days,
            }
            _attach_screen(result.response, screen)
        except Exception:  # noqa: BLE001 - best effort only
            _LOGGER.exception("Nebula: couldn't attach weather screen")

    # ---------------------------------------------------------------- helpers

    async def _converse(
        self,
        user_input: conversation.ConversationInput,
        agent_id: str,
        persona: str | None = None,
    ) -> conversation.ConversationResult | None:
        kwargs: dict = {}
        if persona:
            kwargs["extra_system_prompt"] = persona
        try:
            return await conversation.async_converse(
                self.hass,
                user_input.text,
                user_input.conversation_id,
                user_input.context,
                language=user_input.language,
                agent_id=agent_id,
                device_id=user_input.device_id,
                **kwargs,
            )
        except TypeError:
            # Older core: no extra_system_prompt kwarg — fold persona into the text.
            try:
                text = user_input.text
                if persona:
                    text = f"[System: {persona}]\n\n{text}"
                return await conversation.async_converse(
                    self.hass,
                    text,
                    user_input.conversation_id,
                    user_input.context,
                    language=user_input.language,
                    agent_id=agent_id,
                    device_id=user_input.device_id,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Nebula: converse via %s failed", agent_id)
                return None
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Nebula: converse via %s failed", agent_id)
            return None

    @staticmethod
    def _should_fall_through(result: conversation.ConversationResult) -> bool:
        resp = result.response
        if resp.response_type != intent.IntentResponseType.ERROR:
            return False
        return resp.error_code in _FALL_THROUGH

    @staticmethod
    def _is_error(result: conversation.ConversationResult) -> bool:
        return result.response.response_type == intent.IntentResponseType.ERROR

    @classmethod
    def _is_retryable(cls, result: conversation.ConversationResult) -> bool:
        if not cls._is_error(result):
            return False
        blob = " ".join(
            filter(
                None,
                [
                    str(result.response.error_code or ""),
                    result.response.speech.get("plain", {}).get("speech", "")
                    if result.response.speech
                    else "",
                ],
            )
        )
        return bool(_RETRYABLE.search(blob))

    def _polish(
        self, result: conversation.ConversationResult
    ) -> conversation.ConversationResult:
        """Trim AI-filler and tidy whitespace on the spoken reply."""
        speech = result.response.speech or {}
        plain = speech.get("plain", {})
        said = plain.get("speech")
        if isinstance(said, str) and said:
            cleaned = _FILLER.sub("", said).strip()
            cleaned = re.sub(r"\s+", " ", cleaned)
            if cleaned and cleaned[0].islower():
                cleaned = cleaned[0].upper() + cleaned[1:]
            if cleaned != said:
                plain["speech"] = cleaned
                speech["plain"] = plain
                result.response.async_set_speech(cleaned)
        return result

    def _say(
        self, user_input: conversation.ConversationInput, text: str
    ) -> conversation.ConversationResult:
        resp = intent.IntentResponse(language=user_input.language or "en")
        resp.async_set_speech(text)
        return conversation.ConversationResult(
            response=resp, conversation_id=user_input.conversation_id
        )

    def _degraded(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        return self._say(
            user_input,
            "I can't reach my assistant right now, but I can still run the house. "
            "Try me again in a moment.",
        )
