"""Config flow for Relative Light Group integration."""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Mapping
from typing import Any, cast

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.const import CONF_ENTITIES
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, selector
from homeassistant.helpers.schema_config_entry_flow import (
    SchemaCommonFlowHandler,
    SchemaConfigFlowHandler,
    SchemaFlowFormStep,
    SchemaOptionsFlowHandler,
    entity_selector_without_own_entities,
)

from .const import (
    CONF_ALL,
    CONF_HIDE_MEMBERS,
    CONF_REMEMBER_BRIGHTNESS,
    CONF_REMEMBER_ON_STATE,
    DOMAIN,
)
from .entity import GroupEntity
from .light import async_create_preview_light


def light_config_schema() -> vol.Schema:
    """Generate config schema for light groups."""
    return vol.Schema(
        {
            vol.Required("name"): selector.TextSelector(),
            vol.Required(CONF_ENTITIES): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="light", multiple=True, reorder=True
                ),
            ),
            vol.Required(CONF_ALL, default=False): selector.BooleanSelector(),
            vol.Required(CONF_HIDE_MEMBERS, default=False): selector.BooleanSelector(),
            vol.Required(
                CONF_REMEMBER_ON_STATE, default=False
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_REMEMBER_BRIGHTNESS, default=False
            ): selector.BooleanSelector(),
        }
    )


async def light_options_schema(
    handler: SchemaCommonFlowHandler | None,
) -> vol.Schema:
    """Generate options schema for light groups."""
    entity_selector_field: selector.Selector[Any] | vol.Schema
    if handler is None:
        entity_selector_field = selector.selector(
            {"entity": {"domain": "light", "multiple": True, "reorder": True}}
        )
    else:
        entity_selector_field = entity_selector_without_own_entities(
            cast(SchemaOptionsFlowHandler, handler.parent_handler),
            selector.EntitySelectorConfig(
                domain="light", multiple=True, reorder=True
            ),
        )

    return vol.Schema(
        {
            vol.Required(CONF_ENTITIES): entity_selector_field,
            vol.Required(CONF_ALL, default=False): selector.BooleanSelector(),
            vol.Required(CONF_HIDE_MEMBERS, default=False): selector.BooleanSelector(),
            vol.Required(
                CONF_REMEMBER_ON_STATE, default=False
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_REMEMBER_BRIGHTNESS, default=False
            ): selector.BooleanSelector(),
        }
    )


LIGHT_CONFIG_SCHEMA = light_config_schema()

CONFIG_FLOW = {
    "user": SchemaFlowFormStep(
        LIGHT_CONFIG_SCHEMA,
        preview="relative_light_group",
    ),
}

OPTIONS_FLOW = {
    "init": SchemaFlowFormStep(
        light_options_schema,
        preview="relative_light_group",
    ),
}

PREVIEW_OPTIONS_SCHEMA: dict[str, vol.Schema] = {}

CREATE_PREVIEW_ENTITY: dict[
    str,
    Callable[[HomeAssistant, str, dict[str, Any]], GroupEntity],
] = {
    "light": async_create_preview_light,
}


class RelativeLightGroupConfigFlowHandler(SchemaConfigFlowHandler, domain=DOMAIN):
    """Handle a config or options flow for relative light groups."""

    config_flow = CONFIG_FLOW
    options_flow = OPTIONS_FLOW
    options_flow_reloads = True

    @callback
    def async_config_entry_title(self, options: Mapping[str, Any]) -> str:
        """Return config entry title."""
        return cast(str, options["name"]) if "name" in options else ""

    @callback
    def async_config_flow_finished(self, options: Mapping[str, Any]) -> None:
        """Hide the group members if requested."""
        if options.get(CONF_HIDE_MEMBERS, False):
            _async_hide_members(
                self.hass, options[CONF_ENTITIES], er.RegistryEntryHider.INTEGRATION
            )

    @callback
    @staticmethod
    def async_options_flow_finished(
        hass: HomeAssistant, options: Mapping[str, Any]
    ) -> None:
        """Hide or unhide the group members as requested."""
        hidden_by = (
            er.RegistryEntryHider.INTEGRATION
            if options.get(CONF_HIDE_MEMBERS, False)
            else None
        )
        _async_hide_members(hass, options[CONF_ENTITIES], hidden_by)

    @staticmethod
    async def async_setup_preview(hass: HomeAssistant) -> None:
        """Set up preview WS API."""
        schema = cast(
            Callable[
                [SchemaCommonFlowHandler | None],
                Coroutine[Any, Any, vol.Schema],
            ],
            OPTIONS_FLOW["init"].schema,
        )
        PREVIEW_OPTIONS_SCHEMA["light"] = await schema(None)
        websocket_api.async_register_command(hass, ws_start_preview)


def _async_hide_members(
    hass: HomeAssistant,
    members: list[str],
    hidden_by: er.RegistryEntryHider | None,
) -> None:
    """Hide or unhide group members."""
    registry = er.async_get(hass)
    for member in members:
        if not (entity_id := er.async_resolve_entity_id(registry, member)):
            continue
        if entity_id not in registry.entities:
            continue
        registry.async_update_entity(entity_id, hidden_by=hidden_by)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "relative_light_group/start_preview",
        vol.Required("flow_id"): str,
        vol.Required("flow_type"): vol.Any("config_flow", "options_flow"),
        vol.Required("user_input"): dict,
    }
)
@callback
def ws_start_preview(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Generate a preview."""
    entity_registry_entry: er.RegistryEntry | None = None
    if msg["flow_type"] == "config_flow":
        flow_status = hass.config_entries.flow.async_get(msg["flow_id"])
        form_step = cast(SchemaFlowFormStep, CONFIG_FLOW["user"])
        schema = cast(vol.Schema, form_step.schema)
        validated = schema(msg["user_input"])
        name = validated["name"]
    else:
        flow_status = hass.config_entries.options.async_get(msg["flow_id"])
        config_entry_id = flow_status["handler"]
        config_entry = hass.config_entries.async_get_entry(config_entry_id)
        if not config_entry:
            raise HomeAssistantError

        name = config_entry.options["name"]
        validated = PREVIEW_OPTIONS_SCHEMA["light"](msg["user_input"])
        entity_registry = er.async_get(hass)
        entries = er.async_entries_for_config_entry(entity_registry, config_entry_id)
        if entries:
            entity_registry_entry = entries[0]

    @callback
    def async_preview_updated(state: str, attributes: Mapping[str, Any]) -> None:
        """Forward config entry state events to websocket."""
        connection.send_message(
            websocket_api.event_message(
                msg["id"], {"attributes": attributes, "state": state}
            )
        )

    preview_entity = CREATE_PREVIEW_ENTITY["light"](hass, name, validated)
    preview_entity.hass = hass
    preview_entity.registry_entry = entity_registry_entry

    connection.send_result(msg["id"])
    connection.subscriptions[msg["id"]] = preview_entity.async_start_preview(
        async_preview_updated
    )
