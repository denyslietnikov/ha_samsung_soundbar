"""OCF credential failures never initiate Samsung OAuth reauthentication."""

from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

ISSUE_PREFIX = "ocf_credential_"


def update_ocf_issue(hass, entry, kind):
    if kind is None:
        ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_PREFIX}{entry.entry_id}")
    elif kind in {"auth", "identity", "credential"}:
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{ISSUE_PREFIX}{entry.entry_id}",
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="ocf_credential",
            translation_placeholders={"name": entry.title},
            data={"entry_id": entry.entry_id},
        )
