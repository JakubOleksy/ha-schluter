"""Break out thermostat telemetry into separate sensor entities."""
from __future__ import annotations

from datetime import datetime, timezone

from .api import Thermostat

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature, UnitOfEnergy, UnitOfPower
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import SchluterData
from .const import DOMAIN, ZERO_WATTS
from .entity import SchluterEntity


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Add sensors for passed config_entry in HA."""
    data: SchluterData = hass.data[DOMAIN][config_entry.entry_id]

    # Add the Temperature Sensor
    async_add_entities(
        SchluterTemperatureSensor(data.coordinator, thermostat_id)
        for thermostat_id in data.coordinator.data
    )

    # Add the Target Temperature Sensor
    async_add_entities(
        SchluterTargetTemperatureSensor(data.coordinator, thermostat_id)
        for thermostat_id in data.coordinator.data
    )

    # Add the Power Sensor
    async_add_entities(
        SchluterPowerSensor(data.coordinator, thermostat_id)
        for thermostat_id in data.coordinator.data
    )

    # Add the price per kwh Sensor
    async_add_entities(
        SchluterEnergyPriceSensor(data.coordinator, thermostat_id)
        for thermostat_id in data.coordinator.data
    )

    # Add the virtual/calculated KwH Sensor
    async_add_entities(
        SchluterEnergySensor(data.coordinator, thermostat_id)
        for thermostat_id in data.coordinator.data
    )


class SchluterTargetTemperatureSensor(
    SchluterEntity, SensorEntity
):
    """Representation of a Sensor."""

    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[str, dict[str, Thermostat]]],
        thermostat_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, thermostat_id)
        self._attr_name = coordinator.data[thermostat_id].name + " Target Temperature"
        self._thermostat_id = thermostat_id
        self._attr_unique_id = (
            f"{coordinator.data[thermostat_id].name}-target-{self._attr_device_class}"
        )

    @property
    def device_info(self):
        """Return information to link this entity."""
        return {
            "identifiers": {(DOMAIN, self._thermostat_id)},
        }

    @property
    def native_value(self) -> float:
        """Return the state of the sensor."""
        return self.coordinator.data[self._thermostat_id].set_point_temp


class SchluterTemperatureSensor(SchluterEntity, SensorEntity):
    """Representation of a Sensor."""

    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[str, dict[str, Thermostat]]],
        thermostat_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, thermostat_id)
        self._attr_name = coordinator.data[thermostat_id].name + " Current Temperature"
        self._thermostat_id = thermostat_id
        self._attr_unique_id = (
            f"{coordinator.data[thermostat_id].name}-{self._attr_device_class}"
        )

    @property
    def device_info(self):
        """Return information to link this entity."""
        return {
            "identifiers": {(DOMAIN, self._thermostat_id)},
        }

    @property
    def native_value(self) -> float:
        """Return the state of the sensor."""
        return self.coordinator.data[self._thermostat_id].temperature


class SchluterPowerSensor(SchluterEntity, SensorEntity):
    """Representation of a Sensor."""

    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[str, dict[str, Thermostat]]],
        thermostat_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, thermostat_id)
        self._attr_name = coordinator.data[thermostat_id].name + " Power"
        self._thermostat_id = thermostat_id
        self._attr_unique_id = (
            f"{coordinator.data[thermostat_id].name}-{self._attr_device_class}"
        )

    @property
    def device_info(self):
        """Return information to link this entity."""
        return {
            "identifiers": {(DOMAIN, self._thermostat_id)},
        }

    @property
    def native_value(self) -> int:
        """Return the state of the sensor."""
        if self.coordinator.data[self._thermostat_id].is_heating:
            return self.coordinator.data[self._thermostat_id].load_measured_watt
        return ZERO_WATTS


class SchluterEnergySensor(SchluterEntity, RestoreEntity, SensorEntity):
    """Energy sensor derived by integrating measured watts over time."""

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[str, dict[str, Thermostat]]],
        thermostat_id: str,
    ) -> None:
        super().__init__(coordinator, thermostat_id)
        self._attr_name = coordinator.data[thermostat_id].name + " Energy"
        self._thermostat_id = thermostat_id
        self._attr_unique_id = (
            f"{coordinator.data[thermostat_id].name}-{self._attr_device_class}"
        )
        self._accumulated_kwh = 0.0
        self._last_sample_at: datetime | None = None
        self._last_power_w = 0.0

    async def async_added_to_hass(self) -> None:
        """Restore previous accumulated value after restart."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            try:
                self._accumulated_kwh = float(last_state.state)
            except (TypeError, ValueError):
                self._accumulated_kwh = 0.0

            restored_last_sample = last_state.attributes.get("last_sample_at")
            if restored_last_sample:
                try:
                    self._last_sample_at = datetime.fromisoformat(restored_last_sample)
                except ValueError:
                    self._last_sample_at = None

            try:
                self._last_power_w = float(
                    last_state.attributes.get("last_power_w", 0.0)
                )
            except (TypeError, ValueError):
                self._last_power_w = 0.0

        self._integrate_energy()

    @property
    def device_info(self):
        return {"identifiers": {(DOMAIN, self._thermostat_id)}}

    @property
    def extra_state_attributes(self) -> dict[str, str | float | None]:
        return {
            "last_sample_at": self._last_sample_at.isoformat()
            if self._last_sample_at
            else None,
            "last_power_w": round(self._last_power_w, 3),
            "source": "integrated_load_measured_watt",
        }

    @property
    def native_value(self) -> float:
        self._integrate_energy()
        return round(self._accumulated_kwh, 3)

    def _current_power_w(self) -> float:
        thermostat = self.coordinator.data[self._thermostat_id]
        if thermostat.is_heating:
            return float(thermostat.load_measured_watt)
        return 0.0

    def _integrate_energy(self) -> None:
        now = datetime.now(timezone.utc)
        current_power_w = self._current_power_w()

        if self._last_sample_at is not None:
            elapsed_hours = (
                max((now - self._last_sample_at).total_seconds(), 0) / 3600.0
            )
            self._accumulated_kwh += (self._last_power_w * elapsed_hours) / 1000.0

        self._last_sample_at = now
        self._last_power_w = current_power_w


class SchluterEnergyPriceSensor(SchluterEntity, SensorEntity):
    """Representation of a Sensor."""

    _attr_native_unit_of_measurement = "$/kWh"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[str, dict[str, Thermostat]]],
        thermostat_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, thermostat_id)
        self._attr_name = coordinator.data[thermostat_id].name + " Price"
        self._thermostat_id = thermostat_id
        self._attr_unique_id = (
            f"{coordinator.data[thermostat_id].name}-{self._attr_device_class}"
        )

    @property
    def device_info(self):
        """Return information to link this entity."""
        return {
            "identifiers": {(DOMAIN, self._thermostat_id)},
        }

    @property
    def native_value(self) -> float:
        """Return the state of the sensor."""
        return self.coordinator.data[self._thermostat_id].kwh_charge
