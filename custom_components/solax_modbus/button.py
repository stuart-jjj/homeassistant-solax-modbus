import logging
from dataclasses import replace
from datetime import timedelta
from time import time
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    BUTTONREPEAT_FIRST,
    BUTTONREPEAT_LOOP,
    CONF_MODBUS_ADDR,
    DEFAULT_MODBUS_ADDR,
    DOMAIN,
    WRITE_MULTI_MODBUS,
    WRITE_MULTISINGLE_MODBUS,
    WRITE_SINGLE_MODBUS,
    BaseModbusButtonEntityDescription,
    autorepeat_set,
    matches_modbus_protocol,
)

_LOGGER = logging.getLogger(__name__)

# The Gen3 X1 AC remote-control command expires on the inverter after 4 s with
# no refresh (register 0x9F "export duration", left at its hardware default —
# see plugin_solax.py:_compute_gen3_active_power). Rather than extending that
# timer, refresh well inside the window via a dedicated timer independent of
# any scan-group poll cadence — mirrors se-vpp-client/vpp-local's ControlLoop,
# which refreshes its setpoint every 250 ms against the same 4 s expiry
# (scaled down here since an HA integration doesn't need sub-second cadence
# to stay comfortably inside a 4 s window).
GEN3_RC_TRIGGER_KEY = "remotecontrol_trigger_gen3"
GEN3_RC_KEEPALIVE_INTERVAL_S = 2


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> bool:
    if entry.data:  # old style - remove soon
        hub_name = entry.data[CONF_NAME]
        modbus_addr = entry.data.get(CONF_MODBUS_ADDR, DEFAULT_MODBUS_ADDR)
    else:  # new style
        hub_name = entry.options[CONF_NAME]
        modbus_addr = entry.options.get(CONF_MODBUS_ADDR, DEFAULT_MODBUS_ADDR)
    hub = hass.data[DOMAIN][hub_name]["hub"]

    plugin = hub.plugin
    inverter_name_suffix = ""
    if hub.inverterNameSuffix is not None and hub.inverterNameSuffix != "":
        inverter_name_suffix = hub.inverterNameSuffix + " "

    entities = []
    for button_info in plugin.BUTTON_TYPES:
        if plugin.matchInverterWithMask(
            hub._invertertype, button_info.allowedtypes, hub.seriesnumber, button_info.blacklist
        ) and matches_modbus_protocol(hub, button_info):
            if not (button_info.name.startswith(inverter_name_suffix)):
                button_info = replace(button_info, name=inverter_name_suffix + button_info.name)
            button = SolaXModbusButton(hub_name, hub, modbus_addr, hub.device_info, button_info)
            entities.append(button)
            if button_info.key == plugin.wakeupButton():
                hub.wakeupButton = button_info
            if button_info.value_function:
                hub.computedEntities[button_info.key] = button_info
            elif button_info.command is None:
                _LOGGER.warning(f"button without command and without value_function found: {button_info.key}")

            # register dependency chain
            deplist = button_info.depends_on
            if isinstance(deplist, str):
                deplist = (deplist,)
            if isinstance(
                deplist,
                (
                    list,
                    tuple,
                ),
            ):
                _LOGGER.debug(f"{hub.name}: {button_info.key} depends on entities {deplist}")
                for dep_on in deplist:  # register inter-sensor dependencies (e.g. for value functions)
                    if dep_on != button_info.key:
                        hub.entity_dependencies.setdefault(dep_on, []).append(button_info.key)  # can be more than one

    async_add_entities(entities)
    _LOGGER.info(f"hub.wakeuButton: {hub.wakeupButton}")
    return True


class SolaXModbusButton(ButtonEntity):
    """Representation of an SolaX Modbus button."""

    def __init__(
        self,
        platform_name: str,
        hub: Any,
        modbus_addr: int,
        device_info: DeviceInfo,
        button_info: BaseModbusButtonEntityDescription,
    ) -> None:
        """Initialize the button."""
        self._platform_name = platform_name
        self._hub = hub
        self._modbus_addr = modbus_addr
        self._attr_device_info = device_info
        # self.entity_id = "button." + platform_name + "_" + button_info.key
        self._name = button_info.name
        self._key = button_info.key
        self.button_info = button_info
        self._register = button_info.register
        self._command = button_info.command
        self._attr_icon = button_info.icon
        self._attr_entity_category = button_info.entity_category
        self._write_method = button_info.write_method
        self._gen3_keepalive_unsub = None

    @property
    def name(self) -> str:
        """Return the name."""
        return f"{self._platform_name} {self._name}"

    @property
    def unique_id(self) -> str | None:
        return f"{self._platform_name}_{self._key}"

    async def async_added_to_hass(self) -> None:
        """Start the dedicated Gen3 RC keepalive timer, if this is that button."""
        if self._key == GEN3_RC_TRIGGER_KEY:
            self._gen3_keepalive_unsub = async_track_time_interval(
                self.hass, self._async_gen3_rc_keepalive_tick, timedelta(seconds=GEN3_RC_KEEPALIVE_INTERVAL_S)
            )

    async def async_will_remove_from_hass(self) -> None:
        if self._gen3_keepalive_unsub is not None:
            self._gen3_keepalive_unsub()
            self._gen3_keepalive_unsub = None

    async def _async_gen3_rc_keepalive_tick(self, _now: Any = None) -> None:
        """Refresh the Gen3 RC command every GEN3_RC_KEEPALIVE_INTERVAL_S, independent
        of any scan-group poll cadence — see the module docstring above.

        Only resends the *existing* session (does not renew its expiry, matching
        the semantics of the scan-group-driven autorepeat loop in __init__.py);
        a fresh async_press() is what extends _repeatUntil. No-ops silently
        when there's no active RC session for this button to avoid racing the
        expiry/cleanup handling already owned by that loop.
        """
        repeat_until = self._hub.data.get("_repeatUntil", {}).get(self._key, 0)
        if repeat_until <= 0 or time() >= repeat_until:
            return
        if not self.button_info.value_function:
            return
        res = self.button_info.value_function(BUTTONREPEAT_LOOP, self.button_info, self._hub.data)
        if not res:
            return
        action = res.get("action")
        if action != WRITE_MULTI_MODBUS:
            return
        await self._hub.async_write_registers_multi(
            unit=self._modbus_addr,
            address=res.get("register", self._register),
            payload=res.get("data"),
        )

    async def async_press(self) -> None:
        """Write the button value."""
        if self._write_method == WRITE_MULTISINGLE_MODBUS:
            _LOGGER.info(f"writing {self._platform_name} button register {self._register} value {self._command}")
            await self._hub.async_write_registers_single(
                unit=self._modbus_addr,
                address=self._register,
                payload=self._command,
                register_data_type=getattr(self.button_info, "register_data_type", None),
            )
        elif self._write_method == WRITE_SINGLE_MODBUS:
            _LOGGER.info(f"writing {self._platform_name} button register {self._register} value {self._command}")
            await self._hub.async_write_register(
                unit=self._modbus_addr,
                address=self._register,
                payload=self._command,
                register_data_type=getattr(self.button_info, "register_data_type", None),
            )
        elif self._write_method == WRITE_MULTI_MODBUS:
            if self.button_info.autorepeat:
                duration = self._hub.data.get(self.button_info.autorepeat, 0)
                autorepeat_set(self._hub.data, self.button_info.key, time() + duration - 0.5)
            if self.button_info.value_function:
                res = self.button_info.value_function(BUTTONREPEAT_FIRST, self.button_info, self._hub.data)  # initval = 0 means first manual run
                if res:
                    if self.button_info.autorepeat:  # different return value structure for autorepeat value function
                        reg = res.get("register", self._register)
                        data = res.get("data", None)
                        action = res.get("action")
                        if not action:
                            _LOGGER.error(f"autorepeat value function for {self._key} must return dict containing action")
                        _LOGGER.info(f"writing {self._platform_name} button register {self._register} value {res}")
                        if action == WRITE_MULTI_MODBUS:
                            await self._hub.async_write_registers_multi(unit=self._modbus_addr, address=reg, payload=data)
                    else:
                        await self._hub.async_write_registers_multi(unit=self._modbus_addr, address=self._register, payload=res)
