"""Entry-scoped local transport warnings, containing no credentials."""

from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

ISSUE_PREFIX = "ocf_credential_"
LOCAL_CONFIGURATION_ISSUE_PREFIX = "local_configuration_"


def update_ocf_issue(hass, entry, kind):
    issue_id = f"{ISSUE_PREFIX}{entry.entry_id}"
    if kind is None:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    elif kind in {"auth", "identity", "credential", "missing_credential"}:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=(
                "ocf_missing_credential"
                if kind == "missing_credential"
                else "ocf_credential"
            ),
            translation_placeholders={"name": entry.title},
            data={"entry_id": entry.entry_id},
        )


def update_local_configuration_issue(hass, entry, required):
    """Route missing local settings to the existing entry's reconfigure flow."""
    issue_id = f"{LOCAL_CONFIGURATION_ISSUE_PREFIX}{entry.entry_id}"
    if not required:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="local_configuration",
        translation_placeholders={"name": entry.title},
        data={"entry_id": entry.entry_id},
    )
