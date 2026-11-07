"""Route local connection and OCF repairs to the existing entry."""

import voluptuous as vol
from homeassistant.components.repairs import FlowType, RepairsFlow
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.core import HomeAssistant

from .ocf_repairs import ISSUE_PREFIX as OCF_ISSUE_PREFIX
from .ocf_repairs import LOCAL_CONFIGURATION_ISSUE_PREFIX


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data):
    if issue_id.startswith(LOCAL_CONFIGURATION_ISSUE_PREFIX) and data:
        return LocalConfigurationRepairFlow()
    if issue_id.startswith(OCF_ISSUE_PREFIX) and data:
        return OcfRepairFlow()
    raise ValueError("Unknown Samsung Soundbar issue")


class LocalConfigurationRepairFlow(RepairsFlow):
    async def async_step_init(self, user_input=None):
        return await self.async_step_choose()

    async def async_step_choose(self, user_input=None):
        entry = self.hass.config_entries.async_get_entry(self.data["entry_id"])
        if entry is None:
            return self.async_create_entry(title="", data={})
        if user_input is not None:
            result = await self.hass.config_entries.flow.async_init(
                entry.domain,
                context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
            )
            if "flow_id" not in result:
                return self.async_abort(reason="repair_flow_unavailable")
            return self.async_abort(
                reason="reconfigure",
                next_flow=(FlowType.CONFIG_FLOW, result["flow_id"]),
            )
        return self.async_show_form(
            step_id="choose",
            data_schema=vol.Schema({}),
            description_placeholders={"name": entry.title},
        )


class OcfRepairFlow(RepairsFlow):
    async def async_step_init(self, user_input=None):
        return await self.async_step_choose()

    async def async_step_choose(self, user_input=None):
        entry = self.hass.config_entries.async_get_entry(self.data["entry_id"])
        if entry is None:
            return self.async_create_entry(title="", data={})
        if user_input is not None:
            result = await self.hass.config_entries.options.async_init(
                entry.entry_id, data={"ocf_action": "import"}
            )
            if "flow_id" not in result:
                return self.async_abort(reason="repair_flow_unavailable")
            return self.async_abort(
                reason="reconfigure",
                next_flow=(FlowType.OPTIONS_FLOW, result["flow_id"]),
            )
        return self.async_show_form(
            step_id="choose",
            data_schema=vol.Schema({}),
            description_placeholders={"name": entry.title},
        )
