"""Verified Q800F EQ route; context never changes implicitly from a control."""

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import DOMAIN
from .local_ocf import OcfError
from .ocf_profiles import EQ_PREFIX, Q800F_EQ, Q800F_WOOFER

EQ_FIELDS = (
    "equalizer_schema_valid",
    "active_equalizer_preset",
    "supported_equalizer_presets",
    "equalizer_action",
    "equalizer_band_levels",
)
CONTEXT_FIELDS = (
    ("/sec/networkaudio/advancedaudio", ("bass_mode",)),
    ("/sec/networkaudio/currentEQMode", ("current_equalizer_mode",)),
)


class OcfEqualizer:
    def __init__(self, device):
        self.device = device
        self._profile = None

    def value(self, field):
        coordinator = self.device.coordinator
        if coordinator is None:
            return None
        record = coordinator.state.records["ocf"].get(field)
        return (
            record.value
            if record is not None and record.fresh(coordinator.state.clock())
            else None
        )

    @property
    def profile(self):
        coordinator = self.device.coordinator
        if (
            coordinator is None
            or not coordinator.state.use_ocf
            or coordinator.ocf is None
        ):
            return None
        if self._profile is not None:
            return self._profile
        if (
            coordinator.ocf.validated is True
            and (
                self.device.model == Q800F_WOOFER.model
                or self.device.local_identifier == Q800F_WOOFER.rpc_identifier
            )
            and self.value("equalizer_schema_valid") is True
            and all(coordinator.ocf_field_available(key) for key in EQ_FIELDS)
            and all(
                coordinator.state.records["ocf"][key].observed_at
                >= coordinator.state.written_at.get(key, float("-inf"))
                for key in EQ_FIELDS
            )
        ):
            self._profile = Q800F_EQ
        return self._profile

    @property
    def options(self):
        return (
            list(self.value("supported_equalizer_presets") or [])
            if self.profile is not None
            else []
        )

    @property
    def available(self):
        coordinator = self.device.coordinator
        if self.profile is None:
            return False
        power = coordinator.state.resolve("power")
        mode = coordinator.state.resolve("sound_mode")
        return bool(
            self.value("equalizer_schema_valid") is True
            and all(
                coordinator.ocf_field_available(key)
                for key in EQ_FIELDS + ("bass_mode", "current_equalizer_mode")
            )
            and self.value("current_equalizer_mode") == "usereq"
            and self.value("bass_mode") is False
            and coordinator.state.snapshot().local_available
            and power is not None
            and power.source == "rpc"
            and power.value == "on"
            and mode is not None
            and mode.source == "rpc"
            and mode.value == "Standard"
        )

    def require_available(self):
        if not self.available:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="ocf_eq_unavailable"
            )

    async def async_select(self, preset):
        if type(preset) is not str or preset not in self.options:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="ocf_eq_invalid"
            )
        self.require_available()

        def guard():
            self.require_available()
            if preset not in self.options:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="ocf_eq_invalid"
                )

        try:
            result = await self.device.coordinator.async_write_ocf(
                Q800F_EQ.href,
                {EQ_PREFIX + "EQname": preset, EQ_PREFIX + "action": "setEQmode"},
                eq_profile=Q800F_EQ,
                guard=guard,
                required_fields=EQ_FIELDS,
                context_fields=CONTEXT_FIELDS,
                rpc_fields=("power", "sound_mode"),
            )
        except OcfError:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="ocf_eq_unconfirmed"
            ) from None
        if (
            result.readback_matches is not True
            or self.value("active_equalizer_preset") != preset
            or not self.available
        ):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="ocf_eq_unconfirmed"
            )
