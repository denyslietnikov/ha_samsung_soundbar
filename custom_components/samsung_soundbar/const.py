from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.media_player import DOMAIN as MEDIA_PLAYER_DOMAIN
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN

DOMAIN = "samsung_soundbar"
CONF_CLOUD_INTEGRATION = "cloud_integration"
CONF_ENTRY_API_KEY = "api_key"
CONF_ENTRY_DEVICE_ID = "device_id"
CONF_ENTRY_DEVICE_NAME = "device_name"
CONF_ENTRY_MAX_VOLUME = "device_volume"

SMARTTHINGS_AUTHORIZE_URL = "https://api.smartthings.com/oauth/authorize"
SMARTTHINGS_TOKEN_URL = "https://auth-global.api.smartthings.com/oauth/token"
SMARTTHINGS_OAUTH_SCOPES = [
    "r:devices:*",
    "x:devices:*",
    "r:locations:*",
]

CONF_ENTRY_SETTINGS_ADVANCED_AUDIO_SWITCHES = "settings_advanced_audio"
CONF_ENTRY_SETTINGS_EQ_SELECTOR = "settings_eq"
CONF_ENTRY_SETTINGS_SOUNDMODE_SELECTOR = "settings_soundmode"
CONF_ENTRY_SETTINGS_WOOFER_NUMBER = "settings_woofer"

SERVICE_DUMP_EXECUTE_PAYLOAD = "dump_execute_payload"
SERVICE_DUMP_STATUS_SUMMARY = "dump_status_summary"
CONF_HREF = "href"
CONF_INCLUDE_NULL = "include_null"
CONF_PRESET = "preset"

EXECUTE_PAYLOAD_HREFS = {
    "advancedaudio": "/sec/networkaudio/advancedaudio",
    "spacefitSound": "/sec/networkaudio/spacefitSound",
    "activeVoiceAmplifier": "/sec/networkaudio/activeVoiceAmplifier",
    "channelVolume": "/sec/networkaudio/channelVolume",
    "surroundspeaker": "/sec/networkaudio/surroundspeaker",
    "eq": "/sec/networkaudio/eq",
    "soundmode": "/sec/networkaudio/soundmode",
    "woofer": "/sec/networkaudio/woofer",
    "sync": "/sec/networkaudio/sync",
    "virtual": "/sec/networkaudio/virtual",
    "moderateBass": "/sec/networkaudio/moderateBass",
    "soundGrouping": "/sec/networkaudio/soundGrouping",
    "privateRear": "/sec/networkaudio/privateRear",
}

EXECUTE_PAYLOAD_PRESETS = {
    "all": tuple(EXECUTE_PAYLOAD_HREFS.values()),
    **{preset: (href,) for preset, href in EXECUTE_PAYLOAD_HREFS.items()},
}

DEFAULT_NAME = DOMAIN

BUTTON = BUTTON_DOMAIN
SWITCH = SWITCH_DOMAIN
MEDIA_PLAYER = MEDIA_PLAYER_DOMAIN
SELECT = SELECT_DOMAIN
SUPPORTED_DOMAINS = ["media_player", "switch"]


PLATFORMS = [SWITCH, MEDIA_PLAYER, SELECT, BUTTON]
