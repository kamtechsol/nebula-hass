"""Voice intent: "Hey Nebula, what can you do?"

Alexa and Google Home both answer a bare capabilities question locally and
instantly — Nebula had no equivalent, so every "what can you do" fell through
to the LLM fallback and got a generic, occasionally wrong answer. This intent
answers it the same way `game_intent.py` answers "let's play a game": matched
and spoken entirely by the built-in agent, zero LLM round-trip.

Sentences self-install into `custom_sentences/en/` on first setup, same
mechanism as `game_intent.py` — see that file for why a restart is needed to
pick up a newly-written sentence file.
"""

from __future__ import annotations

import logging
import os

from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent

_LOGGER = logging.getLogger(__name__)

INTENT_HELP = "NebulaHelp"

_SPEECH = (
    "I can control lights, switches, locks and thermostats, run scenes, set "
    "timers and alarms, tell you the weather, date and time, play music, and "
    "play voice games like 20 Questions and Word Chain — just say what you "
    "need. Anything past that, I'll think it over."
)

_SENTENCES_YAML = """language: "en"
intents:
  NebulaHelp:
    data:
      - sentences:
          - "what can you do"
          - "what can I ask you"
          - "what do you can do"
          - "help"
          - "what are you capable of"
          - "list your features"
          - "what commands do you support"
          - "what commands can I use"
"""


class NebulaHelpIntent(intent.IntentHandler):
    """Speaks a short, fixed capability summary — no LLM involved."""

    intent_type = INTENT_HELP

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        resp = intent_obj.create_response()
        resp.async_set_speech(_SPEECH)
        return resp


def async_register(hass: HomeAssistant) -> None:
    """Register the intent and self-install its sentences. Call once per HA run."""
    intent.async_register(hass, NebulaHelpIntent())
    hass.async_create_task(_async_install_sentences(hass))


async def _async_install_sentences(hass: HomeAssistant) -> None:
    path = hass.config.path("custom_sentences", "en", "nebula_help.yaml")

    def _write() -> bool:
        if os.path.exists(path):
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(_SENTENCES_YAML)
        return True

    wrote = await hass.async_add_executor_job(_write)
    if wrote:
        _LOGGER.info(
            "Nebula: installed %s — restart Home Assistant (or reload the "
            "conversation agent) to pick up the new sentences",
            path,
        )
