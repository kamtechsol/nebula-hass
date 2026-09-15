"""Constants for the Nebula integration."""

DOMAIN = "nebula"

# Data stored on hass.data[DOMAIN]
DATA_MANAGER = "manager"
DATA_PANEL = "panel"

# Panel <-> integration shared secret (config-entry option; auto-generated).
CONF_PANEL_TOKEN = "panel_token"

# Spotify account link (config-entry options; entered once in the integration
# options flow). See spotify_link.py — the "open a link / scan a QR" broker.
CONF_SPOTIFY_CLIENT_ID = "spotify_client_id"
CONF_SPOTIFY_CLIENT_SECRET = "spotify_client_secret"

# --- conversation.nebula (voice agent) options ------------------------------
CONF_ASSIST_ENABLED = "assist_enabled"            # bool, default True
CONF_ASSIST_LOCAL_FIRST = "assist_local_first"    # bool, try built-in intents first
CONF_ASSIST_FALLBACK_AGENT = "assist_fallback_agent"  # entity_id, "" = auto-detect
CONF_ASSIST_PERSONA = "assist_persona"            # system-prompt text for the LLM

DEFAULT_PERSONA = (
    "You are Nebula, the voice of this home. Speak warmly, calmly and plainly, "
    "like a friendly, capable house manager — never robotic, never bubbly, and "
    "never say things like \"as an AI\". Avoid exclamation marks. Keep spoken "
    "replies to one or two short sentences unless asked for detail. You know this "
    "home's devices and can use tools to check weather, forecasts, the calendar "
    "and entity states before answering. Give a concrete answer rather than "
    "hedging; if you genuinely cannot help, say so briefly and kindly.\n\n"
    "You are only asked questions the home's own fast local intents couldn't "
    "already answer — device on/off, dimming, timers, alarms, the weather, the "
    "date/time, and the panel's voice-playable games (20 Questions, Word Chain, "
    "started by \"let's play a game\") all run instantly without reaching you. "
    "Don't re-offer those as if they were new features; just note them briefly "
    "if someone asks what you can do, and spend your own strength on the "
    "questions a fixed intent grammar can't cover — open-ended knowledge, "
    "reasoning, planning, and anything conversational."
)

# call domains that are routed to the panel instead of Home Assistant services.
PANEL_CALL_DOMAINS = ("panel", "media", "nebula")

# Zeroconf service advertised on the LAN so the Nebula app can find this
# Home Assistant instance without the user typing a URL.
ZEROCONF_TYPE = "_nebula._tcp.local."
ZEROCONF_NAME = "Nebula @ {location} ({instance})"

# Pairing
PAIR_PIN_TTL = 300  # seconds a manually-generated pairing PIN stays valid
PAIR_CODE_TTL = 1800  # seconds the QR pairing code (shown in a notification) stays valid
PAIR_MAX_FAILS = 8  # wrong-PIN guesses before pairing locks out
PAIR_LOCKOUT_S = 60  # seconds pairing is refused after PAIR_MAX_FAILS wrong guesses
PAIR_TOKEN_NAME = "Nebula app"
PAIR_TOKEN_PREFIX = "Nebula: "  # long-lived token client_name prefix — one per paired app
CLIENT_NAME_MAX = 64

# persistent_notification ids
NOTIFY_PAIRING = "nebula_pairing"

# Client kinds reported by subscribers
CLIENT_APP = "app"
CLIENT_PANEL = "panel"
CLIENT_KINDS = (CLIENT_APP, CLIENT_PANEL)

# How stale a client heartbeat may get before it is considered disconnected.
CLIENT_TIMEOUT = 90  # seconds

# Domains the app cares about for its room / scene / automation views.
CONTROLLABLE_DOMAINS = ("light", "switch", "fan", "input_boolean", "cover", "lock")
INTERESTING_DOMAINS = CONTROLLABLE_DOMAINS + (
    "scene",
    "script",
    "automation",
    "media_player",
    "climate",
    "sensor",
    "binary_sensor",
    "todo",
)

SIGNAL_CLIENTS_CHANGED = f"{DOMAIN}_clients_changed"
