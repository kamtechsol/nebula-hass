"""Constants for the Nebula integration."""

DOMAIN = "nebula"

# Data stored on hass.data[DOMAIN]
DATA_MANAGER = "manager"
DATA_PANEL = "panel"

# Panel <-> integration shared secret (config-entry option; auto-generated).
CONF_PANEL_TOKEN = "panel_token"

# call domains that are routed to the panel instead of Home Assistant services.
PANEL_CALL_DOMAINS = ("panel", "media", "nebula")

# Zeroconf service advertised on the LAN so the Nebula app can find this
# Home Assistant instance without the user typing a URL.
ZEROCONF_TYPE = "_nebula._tcp.local."
ZEROCONF_NAME = "Nebula @ {location} ({instance})"

# Pairing
PAIR_PIN_TTL = 300  # seconds a pairing PIN stays valid
PAIR_TOKEN_NAME = "Nebula app"
CLIENT_NAME_MAX = 64

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
)

SIGNAL_CLIENTS_CHANGED = f"{DOMAIN}_clients_changed"
