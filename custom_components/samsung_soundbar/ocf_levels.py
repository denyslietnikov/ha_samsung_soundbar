"""Verified Q800F channel trims and raw AudioSync; no implicit mode or power writes."""

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import DOMAIN
from .local_ocf import OcfError
from .ocf_level_profiles import Q800F_LEVELS, level_payload, validate_level
from .ocf_profiles import Q800F_WOOFER
from .ocf_state import RESOURCE_FIELDS


class OcfLevels:
    def __init__(self, device):
        self.device = device
        self._profiles = {}

    def _enabled(self):
        coordinator = self.device.coordinator
        return bool(
            coordinator is not None
            and self.device.ocf_enabled
            and coordinator.state.use_ocf
            and coordinator.ocf is not None
            and coordinator.ocf.validated is True
            and (
                self.device.model == Q800F_WOOFER.model
                or self.device.local_identifier == Q800F_WOOFER.rpc_identifier
            )
        )

    def _record(self, field):
        if not self._enabled():
            return None
        coordinator = self.device.coordinator
        record = coordinator.state.records["ocf"].get(field)
        if (
            record is None
            or not coordinator.ocf_field_available(field)
            or field in coordinator.state.ocf_unavailable_fields
            or not record.fresh(coordinator.state.clock())
            or record.observed_at
            < coordinator.state.written_at.get(field, float("-inf"))
        ):
            return None
        return record

    @staticmethod
    def _required_fields(profile):
        if profile.wire_name is not None:
            return (
                profile.field,
                "channel_schema_valid",
                profile.field.replace("channel_level_", "channel_status_", 1),
            )
        return (profile.field, "audio_sync_supported")

    def _current_value(self, profile):
        fields = self._required_fields(profile)
        records = [self._record(field) for field in fields]
        if any(record is None for record in records):
            return None
        target, support, *status = (record.value for record in records)
        if support is not True or (
            status and (type(status[0]) is not int or status[0] != 1)
        ):
            return None
        if type(target) is not int:
            return None
        try:
            return validate_level(profile, target)
        except ValueError:
            return None

    def profile(self, field):
        # Discovery is sticky until reload, but never makes cached values available.
        if field in self._profiles:
            return self._profiles[field]
        profile = Q800F_LEVELS.get(field)
        if (
            profile is not None
            and self._enabled()
            and self._current_value(profile) is not None
        ):
            self._profiles[field] = profile
            return profile
        return None

    def value(self, field):
        profile = self.profile(field)
        return None if profile is None else self._current_value(profile)

    def available(self, field):
        if self.value(field) is None:
            return False
        coordinator = self.device.coordinator
        power = coordinator.state.resolve("power")
        return bool(
            coordinator.state.snapshot().local_available
            and power is not None
            and power.source == "rpc"
            and power.value == "on"
            and power.observed_at
            >= coordinator.state.written_at.get("power", float("-inf"))
        )

    def _require_available(self, field):
        if not self.available(field):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="ocf_level_unavailable"
            )

    async def async_set(self, field, value):
        try:
            profile = Q800F_LEVELS.get(field)
            if profile is None:
                raise ValueError
            value = validate_level(profile, value)
            payload = level_payload(profile, value)
        except ValueError:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="ocf_level_invalid"
            ) from None
        self._require_available(field)
        coordinator = self.device.coordinator
        before = {}

        def guard():
            self._require_available(field)
            if profile.wire_name is not None:
                for neighbor in RESOURCE_FIELDS[profile.href] - {
                    field,
                    "channel_schema_valid",
                }:
                    record = self._record(neighbor)
                    if record is not None and type(record.value) is int:
                        before[neighbor] = record

        try:
            result = await coordinator.async_write_ocf(
                profile.href,
                payload,
                setting_profile=profile,
                guard=guard,
                required_fields=self._required_fields(profile),
                rpc_fields=("power",),
            )
        except OcfError:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="ocf_level_unconfirmed"
            ) from None
        if (
            result.readback_matches is not True
            or not self.available(field)
            or self.value(field) != value
            or any(
                (after := self._record(neighbor)) is None
                or after is previous
                or type(after.value) is not int
                or after.value != previous.value
                for neighbor, previous in before.items()
            )
        ):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="ocf_level_unconfirmed"
            )
