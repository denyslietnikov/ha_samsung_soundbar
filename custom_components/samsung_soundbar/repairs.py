"""Forward Cloud repair choices to the existing Options or reauth flow."""

import voluptuous as vol
from homeassistant.components.repairs import FlowType, RepairsFlow
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector

from .cloud_repairs import ISSUE_PREFIX
from .const import CONF_CONTROL_MODE, CONTROL_MODE_LOCAL_ONLY
from .entry_options import get_entry_option


class CloudRepairFlow(RepairsFlow):
    async def async_step_init(self, user_input=None):
        return await self.async_step_choose()

    async def async_step_choose(self, user_input=None):
        entry = self.hass.config_entries.async_get_entry(self.data["entry_id"])
        if (
            entry is None
            or get_entry_option(entry, CONF_CONTROL_MODE) == CONTROL_MODE_LOCAL_ONLY
        ):
            return self.async_create_entry(title="", data={})
        if user_input is not None:
            if user_input["action"] == "reauth" and self.data["kind"] == "auth":
                result = await self.hass.config_entries.flow.async_init(
                    entry.domain,
                    context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
                    data=dict(entry.data),
                )
                flow_type = FlowType.CONFIG_FLOW
            else:
                result = await self.hass.config_entries.options.async_init(
                    entry.entry_id
                )
                flow_type = FlowType.OPTIONS_FLOW
            if "flow_id" not in result:
                return self.async_abort(reason="repair_flow_unavailable")
            return self.async_abort(
                reason="reconfigure", next_flow=(flow_type, result["flow_id"])
            )
        actions = ["options"]
        if self.data["kind"] == "auth":
            actions.append("reauth")
        return self.async_show_form(
            step_id="choose",
            data_schema=vol.Schema(
                {
                    vol.Required("action", default="options"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=actions, translation_key="cloud_repair_action"
                        )
                    )
                }
            ),
            description_placeholders={"name": entry.title},
        )


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data):
    if not issue_id.startswith(ISSUE_PREFIX) or not data:
        raise ValueError("Unknown Samsung Soundbar issue")
    return CloudRepairFlow()
