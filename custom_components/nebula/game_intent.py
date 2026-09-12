"""Voice intent: "Hey Nebula, let's play a game."

Opens the panel's Game Hub and speaks back which games can be played by
voice — closing the gap the assist audit flagged: on-screen navigation
existed (`nebula.panel_command` / ControlCommands' "navigate" action) but
nothing in Home Assistant ever triggered it from speech.

HA only matches a custom intent against sentences it's been told about, so
this self-installs a bundled sentence file into the config's own
`custom_sentences/en/` on first setup — no manual automation or YAML editing
needed. `conversation.py`'s local-first routing already tries the built-in
agent (where custom intents live) before falling back to the LLM, so no
change is needed there.

A newly-written custom_sentences file is only picked up on the next Home
Assistant restart (or a "Reload" of the conversation default agent) — the
same way `config_flow.py` edits needed a restart earlier this session.
"""

from __future__ import annotations

import logging
import os

from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent

from .const import DATA_PANEL, DOMAIN

_LOGGER = logging.getLogger(__name__)

INTENT_PLAY_GAME = "NebulaPlayGame"

# Keep in sync with the panel's voice-enabled games (GameVoiceCapture) —
# app/src/main/java/.../TwentyQuestionsActivity.kt, WordChainActivity.kt.
_VOICE_GAMES = ["20 Questions", "Word Chain"]

_SENTENCES_YAML = """language: "en"
intents:
  NebulaPlayGame:
    data:
      - sentences:
          - "let's play a game"
          - "lets play a game"
          - "play a game"
          - "let's play something"
          - "lets play something"
          - "i want to play a game"
          - "can we play a game"
          - "do you want to play a game"
"""


class NebulaPlayGameIntent(intent.IntentHandler):
    """Navigates the panel to the Game Hub and names the voice-playable games."""

    intent_type = INTENT_PLAY_GAME

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        panel = hass.data.get(DOMAIN, {}).get(DATA_PANEL)
        opened = False
        if panel is not None:
            opened = await panel.send_command("navigate", screen="games")
        else:
            _LOGGER.warning("NebulaPlayGame: no panel channel")

        games = " or ".join(_VOICE_GAMES)
        resp = intent_obj.create_response()
        if opened:
            resp.async_set_speech(f"Here's the game hub. You can play {games} by voice.")
        else:
            resp.async_set_speech(
                f"I couldn't reach the panel, but the game hub has {games} to play by voice."
            )
        return resp


def async_register(hass: HomeAssistant) -> None:
    """Register the intent and self-install its sentences. Call once per HA run."""
    intent.async_register(hass, NebulaPlayGameIntent())
    hass.async_create_task(_async_install_sentences(hass))


async def _async_install_sentences(hass: HomeAssistant) -> None:
    path = hass.config.path("custom_sentences", "en", "nebula_games.yaml")

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
