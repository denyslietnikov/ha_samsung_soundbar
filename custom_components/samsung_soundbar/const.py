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
CONF_CONTROL_MODE = "control_mode"
CONF_LOCAL_FALLBACK_TO_CLOUD = "local_fallback_to_cloud"
CONF_LOCAL_HOST = "local_host"
CONF_LOCAL_PORT = "local_port"
CONF_LOCAL_TIMEOUT = "local_timeout"
CONF_LOCAL_VERIFY_SSL = "local_verify_ssl"

CONTROL_MODE_SMARTTHINGS_CLOUD = "smartthings_cloud"
CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS = "hybrid_local_smartthings"
CONTROL_MODES = (
    CONTROL_MODE_SMARTTHINGS_CLOUD,
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
)
CONTROL_MODE_LABELS = {
    CONTROL_MODE_SMARTTHINGS_CLOUD: "SmartThings Cloud",
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS: "Hybrid Local + SmartThings Cloud",
}

SERVICE_DUMP_EXECUTE_PAYLOAD = "dump_execute_payload"
SERVICE_DUMP_DISCOVERY_SNAPSHOT = "dump_discovery_snapshot"
SERVICE_DUMP_LOCAL_RPC = "dump_local_rpc"
SERVICE_DUMP_STATUS_SUMMARY = "dump_status_summary"
CONF_EXECUTE_HREFS = "execute_hrefs"
CONF_HREF = "href"
CONF_INCLUDE_EXECUTE_STATUS = "include_execute_status"
CONF_INCLUDE_FLATTENED_STATUS = "include_flattened_status"
CONF_LOCAL_RPC_HOST = "host"
CONF_LOCAL_RPC_METHODS = "methods"
CONF_LOCAL_RPC_PORT = "port"
CONF_LOCAL_RPC_TIMEOUT = "timeout"
CONF_LOCAL_RPC_VERIFY_SSL = "verify_ssl"
CONF_LOCAL_RPC_WRITE_METHOD = "write_method"
CONF_LOCAL_RPC_WRITE_PARAMS = "write_params"
CONF_INCLUDE_NULL = "include_null"
CONF_INCLUDE_RAW_STATUS = "include_raw_status"
CONF_PRESET = "preset"
CONF_WRITE_PROPERTY = "write_property"
CONF_WRITE_VALUE = "write_value"

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
    "q800f_ui": (
        EXECUTE_PAYLOAD_HREFS["advancedaudio"],
        EXECUTE_PAYLOAD_HREFS["soundmode"],
        EXECUTE_PAYLOAD_HREFS["eq"],
        EXECUTE_PAYLOAD_HREFS["spacefitSound"],
        EXECUTE_PAYLOAD_HREFS["activeVoiceAmplifier"],
        EXECUTE_PAYLOAD_HREFS["woofer"],
        EXECUTE_PAYLOAD_HREFS["channelVolume"],
        EXECUTE_PAYLOAD_HREFS["surroundspeaker"],
        EXECUTE_PAYLOAD_HREFS["moderateBass"],
        EXECUTE_PAYLOAD_HREFS["virtual"],
        EXECUTE_PAYLOAD_HREFS["sync"],
    ),
    **{preset: (href,) for preset, href in EXECUTE_PAYLOAD_HREFS.items()},
}

DEFAULT_NAME = DOMAIN

BUTTON = BUTTON_DOMAIN
SWITCH = SWITCH_DOMAIN
MEDIA_PLAYER = MEDIA_PLAYER_DOMAIN
SELECT = SELECT_DOMAIN
SUPPORTED_DOMAINS = ["media_player", "switch"]


PLATFORMS = [SWITCH, MEDIA_PLAYER, SELECT, BUTTON]
