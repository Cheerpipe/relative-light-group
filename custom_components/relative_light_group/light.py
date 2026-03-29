"""Platform allowing several lights to be grouped into one with relative brightness control."""

from __future__ import annotations

from collections import Counter, deque
import itertools
import logging
from typing import Any, cast

from homeassistant.components import light
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_MODE,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_EFFECT_LIST,
    ATTR_FLASH,
    ATTR_HS_COLOR,
    ATTR_MAX_COLOR_TEMP_KELVIN,
    ATTR_MIN_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ATTR_RGBW_COLOR,
    ATTR_RGBWW_COLOR,
    ATTR_SUPPORTED_COLOR_MODES,
    ATTR_TRANSITION,
    ATTR_WHITE,
    ATTR_XY_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
    filter_supported_color_modes,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    CONF_ENTITIES,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ALL, CONF_REMEMBER_BRIGHTNESS, CONF_REMEMBER_ON_STATE
from .entity import GroupEntity
from .util import (
    coerce_in,
    find_state_attributes,
    mean_circle,
    mean_tuple,
    reduce_attribute,
)

BRIGHTNESS_MAX = 255
BRIGHTNESS_MIN = 1

DEFAULT_NAME = "Relative Light Group"

PARALLEL_UPDATES = 0

SUPPORT_GROUP_LIGHT = (
    LightEntityFeature.EFFECT
    | LightEntityFeature.FLASH
    | LightEntityFeature.TRANSITION
)

_LOGGER = logging.getLogger(__name__)

FORWARDED_ATTRIBUTES = frozenset(
    {
        ATTR_BRIGHTNESS,
        ATTR_COLOR_TEMP_KELVIN,
        ATTR_EFFECT,
        ATTR_FLASH,
        ATTR_HS_COLOR,
        ATTR_RGB_COLOR,
        ATTR_RGBW_COLOR,
        ATTR_RGBWW_COLOR,
        ATTR_TRANSITION,
        ATTR_WHITE,
        ATTR_XY_COLOR,
    }
)

# Attributes that represent visual changes (not brightness or on/off control)
VISUAL_ATTRIBUTES = frozenset(
    {
        ATTR_COLOR_TEMP_KELVIN,
        ATTR_EFFECT,
        ATTR_FLASH,
        ATTR_HS_COLOR,
        ATTR_RGB_COLOR,
        ATTR_RGBW_COLOR,
        ATTR_RGBWW_COLOR,
        ATTR_WHITE,
        ATTR_XY_COLOR,
    }
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Initialize Relative Light Group config entry."""
    registry = er.async_get(hass)
    entities = er.async_validate_entity_ids(
        registry, config_entry.options[CONF_ENTITIES]
    )
    mode = config_entry.options.get(CONF_ALL, False)
    remember_on_state = config_entry.options.get(CONF_REMEMBER_ON_STATE, False)
    remember_brightness = config_entry.options.get(CONF_REMEMBER_BRIGHTNESS, False)

    async_add_entities(
        [
            RelativeLightGroup(
                config_entry.entry_id,
                config_entry.title,
                entities,
                mode,
                remember_on_state,
                remember_brightness,
            )
        ]
    )


@callback
def async_create_preview_light(
    hass: HomeAssistant, name: str, validated_config: dict[str, Any]
) -> RelativeLightGroup:
    """Create a preview light entity."""
    return RelativeLightGroup(
        None,
        name,
        validated_config[CONF_ENTITIES],
        validated_config.get(CONF_ALL, False),
        validated_config.get(CONF_REMEMBER_ON_STATE, False),
        validated_config.get(CONF_REMEMBER_BRIGHTNESS, False),
    )


class RelativeLightGroup(GroupEntity, LightEntity):
    """Representation of a relative light group.

    Brightness changes are distributed proportionally among on lights.
    Color/effect changes are forwarded only to on lights.
    Turn on/off affects all lights (with optional remember behavior).
    When remember_brightness is enabled, base brightness ratios are preserved
    even after hitting brightness limits (0% or 100%).
    """

    _attr_available = False
    _attr_icon = "mdi:lightbulb-group"
    _attr_max_color_temp_kelvin = 6500
    _attr_min_color_temp_kelvin = 2000
    _attr_should_poll = False

    def __init__(
        self,
        unique_id: str | None,
        name: str,
        entity_ids: list[str],
        mode: bool | None,
        remember_on_state: bool,
        remember_brightness: bool,
    ) -> None:
        """Initialize a relative light group."""
        self._entity_ids = entity_ids
        self._attr_name = name
        self._attr_extra_state_attributes = {ATTR_ENTITY_ID: entity_ids}
        self._attr_unique_id = unique_id
        self.mode = any
        if mode:
            self.mode = all

        self._remember_on_state = remember_on_state
        self._remembered_lights: list[str] | None = None

        self._remember_brightness = remember_brightness
        self._base_brightness: dict[str, int] = {}
        self._last_command_contexts: deque[str] = deque(maxlen=50)

        self._attr_color_mode = ColorMode.UNKNOWN
        self._attr_supported_color_modes = {ColorMode.ONOFF}

    def _get_on_lights(self) -> list:
        """Get list of currently on light states."""
        return [
            state
            for entity_id in self._entity_ids
            if (state := self.hass.states.get(entity_id)) is not None
            and state.state == STATE_ON
        ]

    def _get_on_entity_ids(self) -> list[str]:
        """Get list of currently on light entity IDs."""
        return [state.entity_id for state in self._get_on_lights()]

    def _ensure_base_brightness(self, on_lights: list) -> None:
        """Ensure base brightness is captured for all on lights."""
        for state in on_lights:
            eid = state.entity_id
            if eid not in self._base_brightness:
                brightness = state.attributes.get(ATTR_BRIGHTNESS)
                if brightness is not None:
                    self._base_brightness[eid] = int(brightness)

    def _get_base_group_brightness(self, on_entity_ids: list[str]) -> float | None:
        """Get average base brightness for the given on lights."""
        bases = [
            self._base_brightness[eid]
            for eid in on_entity_ids
            if eid in self._base_brightness
        ]
        if bases:
            return sum(bases) / len(bases)
        return None

    async def _apply_brightness_with_base(
        self,
        data: dict[str, Any],
        on_lights: list,
        target_brightness: int,
    ) -> None:
        """Apply brightness using base-relative algorithm.

        When going UP from base: uses relative headroom distribution
        (all lights reach max together). When going DOWN: scales
        proportionally from base (preserving ratios perfectly).
        Always references base brightness, so ratios are never lost
        even after hitting limits.
        """
        on_entity_ids = [state.entity_id for state in on_lights]
        self._ensure_base_brightness(on_lights)

        base_group = self._get_base_group_brightness(on_entity_ids)
        if base_group is None or base_group <= 0:
            return

        direction = target_brightness - base_group
        brightness_map: dict[str, int] = {}

        if direction >= 0:
            # Going UP: use headroom-based distribution from base
            max_headroom = BRIGHTNESS_MAX - base_group
            factor = direction / max_headroom if max_headroom > 0 else 0
            for eid in on_entity_ids:
                base = self._base_brightness.get(eid)
                if base is not None:
                    new_val = base + factor * (BRIGHTNESS_MAX - base)
                    brightness_map[eid] = coerce_in(round(new_val), 1, 255)
        else:
            # Going DOWN: scale proportionally from base
            factor = direction / base_group
            for eid in on_entity_ids:
                base = self._base_brightness.get(eid)
                if base is not None:
                    new_val = base + factor * base  # = base * (1 + factor)
                    brightness_map[eid] = coerce_in(round(new_val), 1, 255)

        if not brightness_map:
            return

        # Group by target brightness to minimize service calls
        groups: dict[int, list[str]] = {}
        for eid, br in brightness_map.items():
            groups.setdefault(br, []).append(eid)

        visual_data = {
            key: value for key, value in data.items() if key in VISUAL_ATTRIBUTES
        }

        for br, eids in groups.items():
            call_data = {**visual_data}
            call_data[ATTR_BRIGHTNESS] = br
            call_data[ATTR_ENTITY_ID] = eids
            if ATTR_TRANSITION in data:
                call_data[ATTR_TRANSITION] = data[ATTR_TRANSITION]

            _LOGGER.debug("Base-relative brightness call: %s", call_data)

            await self.hass.services.async_call(
                light.DOMAIN,
                SERVICE_TURN_ON,
                call_data,
                blocking=True,
                context=self._context,
            )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Forward the turn_on command with relative brightness control.

        Behavior:
        - If the group is OFF (being turned on):
          - With remember_on_state: restore only previously-on lights
          - Without remember_on_state: turn on all lights
        - If brightness is changing:
          - With remember_brightness: use base-relative algorithm
          - Without remember_brightness: use standard relative algorithm
        - If color/effect is changing: only apply to on lights
        """
        if self._context and self._context.id not in self._last_command_contexts:
            self._last_command_contexts.append(self._context.id)

        data = {
            key: value for key, value in kwargs.items() if key in FORWARDED_ATTRIBUTES
        }

        on_lights = self._get_on_lights()
        has_brightness = ATTR_BRIGHTNESS in data
        has_visual_attrs = any(key in data for key in VISUAL_ATTRIBUTES)
        group_is_on = self._attr_is_on

        # Case 1: Group is currently OFF → turning on
        if not group_is_on:
            if self._remember_on_state and self._remembered_lights:
                target_entity_ids = self._remembered_lights
            else:
                target_entity_ids = self._entity_ids

            # If explicit brightness is commended while turning on the group from off,
            # clear the previous base brightness so they start syncing from this new uniform state.
            if has_brightness and self._remember_brightness:
                self._base_brightness.clear()

            data[ATTR_ENTITY_ID] = target_entity_ids
            _LOGGER.debug("Turning on group (was off): %s", data)

            await self.hass.services.async_call(
                light.DOMAIN,
                SERVICE_TURN_ON,
                data,
                blocking=True,
                context=self._context,
            )
            return

        # Case 2: Group is ON and brightness is being changed
        if has_brightness and on_lights:
            if self._remember_brightness:
                # Use base-relative algorithm (preserves ratios)
                await self._apply_brightness_with_base(
                    data, on_lights, data[ATTR_BRIGHTNESS]
                )

            else:
                # Standard relative algorithm
                await self._apply_relative_brightness(data, on_lights)
            return

        # Case 3: Group is ON, no brightness change, but visual attributes
        if has_visual_attrs and on_lights:
            on_entity_ids = [state.entity_id for state in on_lights]
            visual_data = {
                key: value
                for key, value in data.items()
                if key in VISUAL_ATTRIBUTES or key == ATTR_TRANSITION
            }
            visual_data[ATTR_ENTITY_ID] = on_entity_ids

            _LOGGER.debug("Visual-only change to on lights: %s", visual_data)

            await self.hass.services.async_call(
                light.DOMAIN,
                SERVICE_TURN_ON,
                visual_data,
                blocking=True,
                context=self._context,
            )
            return

        # Case 4: Fallback – no special handling needed
        data[ATTR_ENTITY_ID] = self._entity_ids

        _LOGGER.debug("Forwarded turn_on command: %s", data)

        await self.hass.services.async_call(
            light.DOMAIN,
            SERVICE_TURN_ON,
            data,
            blocking=True,
            context=self._context,
        )

    async def _apply_relative_brightness(
        self, data: dict[str, Any], on_lights: list
    ) -> None:
        """Apply standard relative brightness algorithm (oscarb-style)."""
        group_brightness_current = self._attr_brightness
        if group_brightness_current is None or group_brightness_current <= 0:
            return

        group_brightness_new = data[ATTR_BRIGHTNESS]
        group_brightness_change = group_brightness_new - group_brightness_current

        light_entity_ids = [state.entity_id for state in on_lights]

        # Calculate the proportional change factor
        if group_brightness_change > 0:
            brightness_change_factor = group_brightness_change / (
                BRIGHTNESS_MAX - group_brightness_current
            )
        elif group_brightness_change < 0:
            brightness_change_factor = (
                group_brightness_change / group_brightness_current
            )
        else:
            brightness_change_factor = 0

        def brightness_offset(brightness: int) -> float:
            """Adjust brightness proportionally to light group brightness change."""
            if group_brightness_change == 0:
                return 0
            if group_brightness_change > 0:
                return brightness_change_factor * (BRIGHTNESS_MAX - brightness)
            else:
                return brightness_change_factor * brightness

        # Calculate new brightness level for each light
        light_brightness_levels = []
        if group_brightness_change != 0:
            for state in on_lights:
                light_brightness = state.attributes.get(ATTR_BRIGHTNESS)
                if light_brightness is not None:
                    new_brightness = coerce_in(
                        round(light_brightness + brightness_offset(light_brightness)),
                        BRIGHTNESS_MIN,
                        BRIGHTNESS_MAX,
                    )
                    light_brightness_levels.append(new_brightness)
                else:
                    light_brightness_levels.append(group_brightness_new)

        visual_data = {
            key: value for key, value in data.items() if key in VISUAL_ATTRIBUTES
        }

        if group_brightness_change != 0 and light_brightness_levels:
            # Group by new brightness level to reduce number of calls
            brightness_groups: dict[int, list[str]] = {}
            for entity_id, brightness in zip(
                light_entity_ids, light_brightness_levels
            ):
                if brightness in brightness_groups:
                    brightness_groups[brightness].append(entity_id)
                else:
                    brightness_groups[brightness] = [entity_id]

            for brightness, entity_ids in brightness_groups.items():
                call_data = {**visual_data}
                call_data[ATTR_BRIGHTNESS] = brightness
                call_data[ATTR_ENTITY_ID] = entity_ids
                if ATTR_TRANSITION in data:
                    call_data[ATTR_TRANSITION] = data[ATTR_TRANSITION]

                _LOGGER.debug("Relative brightness call: %s", call_data)

                await self.hass.services.async_call(
                    light.DOMAIN,
                    SERVICE_TURN_ON,
                    call_data,
                    blocking=True,
                    context=self._context,
                )
        elif visual_data:
            on_entity_ids = [state.entity_id for state in on_lights]
            call_data = {**visual_data}
            call_data[ATTR_ENTITY_ID] = on_entity_ids
            if ATTR_TRANSITION in data:
                call_data[ATTR_TRANSITION] = data[ATTR_TRANSITION]

            await self.hass.services.async_call(
                light.DOMAIN,
                SERVICE_TURN_ON,
                call_data,
                blocking=True,
                context=self._context,
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Forward the turn_off command to all lights in the light group."""
        if self._context and self._context.id not in self._last_command_contexts:
            self._last_command_contexts.append(self._context.id)

        # Remember which lights are on before turning off
        if self._remember_on_state:
            self._remembered_lights = self._get_on_entity_ids()
            _LOGGER.debug("Remembered on lights: %s", self._remembered_lights)

        data = {ATTR_ENTITY_ID: self._entity_ids}

        if ATTR_TRANSITION in kwargs:
            data[ATTR_TRANSITION] = kwargs[ATTR_TRANSITION]

        await self.hass.services.async_call(
            light.DOMAIN,
            SERVICE_TURN_OFF,
            data,
            blocking=True,
            context=self._context,
        )

    @callback
    def async_update_group_state(self) -> None:
        """Query all members and determine the light group state."""
        self._update_assumed_state_from_members()

        states = [
            state
            for entity_id in self._entity_ids
            if (state := self.hass.states.get(entity_id)) is not None
        ]
        on_states = [state for state in states if state.state == STATE_ON]

        valid_state = self.mode(
            state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE) for state in states
        )

        if not valid_state:
            self._attr_is_on = None
        else:
            self._attr_is_on = self.mode(state.state == STATE_ON for state in states)

        self._attr_available = any(
            state.state != STATE_UNAVAILABLE for state in states
        )

        # Brightness is calculated only from ON lights
        self._attr_brightness = reduce_attribute(on_states, ATTR_BRIGHTNESS)

        # Update base brightness from external changes only.
        # Check each light's state context to see if it was driven by a group command.
        if self._remember_brightness:
            for state in on_states:
                # If the context is missing, or not generated by the group, it's an external change.
                if not state.context or state.context.id not in self._last_command_contexts:
                    brightness = state.attributes.get(ATTR_BRIGHTNESS)
                    if brightness is not None:
                        self._base_brightness[state.entity_id] = int(brightness)

        self._attr_hs_color = reduce_attribute(
            on_states, ATTR_HS_COLOR, reduce=mean_circle
        )
        self._attr_rgb_color = reduce_attribute(
            on_states, ATTR_RGB_COLOR, reduce=mean_tuple
        )
        self._attr_rgbw_color = reduce_attribute(
            on_states, ATTR_RGBW_COLOR, reduce=mean_tuple
        )
        self._attr_rgbww_color = reduce_attribute(
            on_states, ATTR_RGBWW_COLOR, reduce=mean_tuple
        )
        self._attr_xy_color = reduce_attribute(
            on_states, ATTR_XY_COLOR, reduce=mean_tuple
        )

        self._attr_color_temp_kelvin = reduce_attribute(
            on_states, ATTR_COLOR_TEMP_KELVIN
        )
        self._attr_min_color_temp_kelvin = reduce_attribute(
            states, ATTR_MIN_COLOR_TEMP_KELVIN, default=2000, reduce=min
        )
        self._attr_max_color_temp_kelvin = reduce_attribute(
            states, ATTR_MAX_COLOR_TEMP_KELVIN, default=6500, reduce=max
        )

        self._attr_effect_list = None
        all_effect_lists = list(find_state_attributes(states, ATTR_EFFECT_LIST))
        if all_effect_lists:
            self._attr_effect_list = list(set().union(*all_effect_lists))
            self._attr_effect_list.sort()
            if "None" in self._attr_effect_list:
                self._attr_effect_list.remove("None")
                self._attr_effect_list.insert(0, "None")

        self._attr_effect = None
        all_effects = list(find_state_attributes(on_states, ATTR_EFFECT))
        if all_effects:
            effects_count = Counter(itertools.chain(all_effects))
            self._attr_effect = effects_count.most_common(1)[0][0]

        supported_color_modes = {ColorMode.ONOFF}
        all_supported_color_modes = list(
            find_state_attributes(states, ATTR_SUPPORTED_COLOR_MODES)
        )
        if all_supported_color_modes:
            supported_color_modes = filter_supported_color_modes(
                cast(set[ColorMode], set().union(*all_supported_color_modes))
            )
        self._attr_supported_color_modes = supported_color_modes

        self._attr_color_mode = ColorMode.UNKNOWN
        all_color_modes = list(find_state_attributes(on_states, ATTR_COLOR_MODE))
        if all_color_modes:
            color_mode_count = Counter(itertools.chain(all_color_modes))
            if ColorMode.ONOFF in color_mode_count:
                if ColorMode.ONOFF in supported_color_modes:
                    color_mode_count[ColorMode.ONOFF] = -1
                else:
                    color_mode_count.pop(ColorMode.ONOFF)
            if ColorMode.BRIGHTNESS in color_mode_count:
                if ColorMode.BRIGHTNESS in supported_color_modes:
                    color_mode_count[ColorMode.BRIGHTNESS] = 0
                else:
                    color_mode_count.pop(ColorMode.BRIGHTNESS)
            if color_mode_count:
                self._attr_color_mode = color_mode_count.most_common(1)[0][0]
            else:
                self._attr_color_mode = next(iter(supported_color_modes))

        self._attr_supported_features = LightEntityFeature(0)
        for support in find_state_attributes(states, ATTR_SUPPORTED_FEATURES):
            self._attr_supported_features |= support
        self._attr_supported_features &= SUPPORT_GROUP_LIGHT
