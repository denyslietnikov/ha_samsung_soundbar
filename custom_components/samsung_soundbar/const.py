from homeassistant.components.media_player import DOMAIN as MEDIA_PLAYER_DOMAIN
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN

DOMAIN = "samsung_soundbar"
CONF_ENTRY_DEVICE_ID = "device_id"
CONF_ENTRY_DEVICE_NAME = "device_name"
CONF_ENTRY_MAX_VOLUME = "device_volume"
CONF_CONTROL_MODE = "control_mode"
CONF_LOCAL_HOST = "local_host"
CONF_LOCAL_IDENTITY = "local_identity"
CONF_LOCAL_PORT = "local_port"
CONF_LOCAL_TIMEOUT = "local_timeout"
CONF_LOCAL_VERIFY_SSL = "local_verify_ssl"
CONF_OCF_CONFIGURED = "ocf_configured"
CONF_OCF_ACTION = "ocf_action"

CONTROL_MODE_LOCAL_ONLY = "local_only"
CONTROL_MODE_HYBRID_LOCAL_OCF = "hybrid_local_ocf"
CONTROL_MODES = (CONTROL_MODE_LOCAL_ONLY, CONTROL_MODE_HYBRID_LOCAL_OCF)
CONTROL_MODE_LABELS = {
    CONTROL_MODE_LOCAL_ONLY: "Local only (RPC)",
    CONTROL_MODE_HYBRID_LOCAL_OCF: "Hybrid Local + OCF",
}

SERVICE_DUMP_DISCOVERY_SNAPSHOT = "dump_discovery_snapshot"
SERVICE_DUMP_LOCAL_RPC = "dump_local_rpc"
SERVICE_DUMP_OCF_STATUS = "dump_ocf_status"
CONF_LOCAL_RPC_HOST = "host"
CONF_LOCAL_RPC_METHODS = "methods"
CONF_LOCAL_RPC_PORT = "port"
CONF_LOCAL_RPC_TIMEOUT = "timeout"
CONF_LOCAL_RPC_VERIFY_SSL = "verify_ssl"
CONF_LOCAL_RPC_WRITE_METHOD = "write_method"
CONF_LOCAL_RPC_WRITE_PARAMS = "write_params"
CONF_INCLUDE_NULL = "include_null"

DEFAULT_NAME = DOMAIN

SWITCH = SWITCH_DOMAIN
MEDIA_PLAYER = MEDIA_PLAYER_DOMAIN
SELECT = SELECT_DOMAIN
SUPPORTED_DOMAINS = ["media_player", "switch"]


PLATFORMS = [SWITCH, MEDIA_PLAYER, SELECT]
