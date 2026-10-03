"""Per-entry Cloud Repairs with bounded retries and no credentials in issues."""

from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from pysmartthings.exceptions import SmartThingsConnectionError

from .cloud_errors import CloudAccessError, CloudRateLimitError, normalize_cloud_error
from .const import DOMAIN

ISSUE_PREFIX = "cloud_"
ISSUE_KINDS = ("auth", "access", "payment", "limit", "unavailable")
_DATA_REPAIRS = f"{DOMAIN}_cloud_repairs"


def get_cloud_repairs(hass, entry):
    """Keep the failure count across setup retries until recovery or removal."""
    if not isinstance(hass.data, dict):
        return CloudRepairs(hass, entry)
    managers = hass.data.setdefault(_DATA_REPAIRS, {})
    if entry.entry_id not in managers:
        managers[entry.entry_id] = CloudRepairs(hass, entry)
    return managers[entry.entry_id]


def clear_cloud_issues(hass, entry) -> None:
    """Clear only this entry's issues after recovery or explicit Local-only migration."""
    for kind in ISSUE_KINDS:
        ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_PREFIX}{kind}_{entry.entry_id}")
    if isinstance(hass.data, dict):
        hass.data.get(_DATA_REPAIRS, {}).pop(entry.entry_id, None)


class CloudRepairs:
    def __init__(self, hass, entry) -> None:
        self.hass, self.entry = hass, entry
        self.failures = 0

    def failed(self, error: Exception) -> float:
        """Report actionable failures; transient outages require three observations."""
        error = normalize_cloud_error(error)
        if isinstance(self.hass.data, dict):
            self.hass.data.setdefault(_DATA_REPAIRS, {})[self.entry.entry_id] = self
        retry_after = 15
        if isinstance(error, ConfigEntryAuthFailed):
            self.failures = 0
            kind = "auth"
        elif isinstance(error, CloudAccessError):
            self.failures = 0
            kind = "payment" if error.status == 402 else "access"
            retry_after = error.retry_after
        elif isinstance(error, CloudRateLimitError):
            self.failures = 0
            kind = "limit"
            retry_after = error.retry_after
        else:
            if not isinstance(error, (ConfigEntryNotReady, SmartThingsConnectionError)):
                return retry_after
            self.failures += 1
            if self.failures < 3:
                return retry_after
            kind = "unavailable"

        for other in ISSUE_KINDS:
            if other != kind:
                ir.async_delete_issue(
                    self.hass, DOMAIN, f"{ISSUE_PREFIX}{other}_{self.entry.entry_id}"
                )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_PREFIX}{kind}_{self.entry.entry_id}",
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=f"cloud_{kind}",
            translation_placeholders={"name": self.entry.title},
            data={"entry_id": self.entry.entry_id, "kind": kind},
        )
        return retry_after

    def recovered(self) -> None:
        self.failures = 0
        clear_cloud_issues(self.hass, self.entry)
