"""Capteurs Home Assistant pour Eau du Grand Lyon."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EGLDataCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Configure les capteurs."""
    coordinator: EGLDataCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        EGLDailySensor(coordinator, entry),
        EGLMonthlySensor(coordinator, entry),
        EGLRolling30dSensor(coordinator, entry),
    ])


class _EGLBaseSensor(CoordinatorEntity[EGLDataCoordinator], SensorEntity):
    """Capteur de base EGL."""

    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_has_entity_name = True

    def __init__(self, coordinator: EGLDataCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Eau du Grand Lyon",
            "manufacturer": "Eau du Grand Lyon",
            "model": "Compteur Téléo",
        }


class EGLDailySensor(_EGLBaseSensor):
    """Consommation du jour précédent (en litres)."""

    _attr_translation_key = "daily"
    _attr_icon = "mdi:water"

    def __init__(self, coordinator: EGLDataCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_daily"
        self._attr_name = "Consommation journalière"

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if not data:
            return None
        return data.get("daily_liters")

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "date": data.get("daily_date"),
            "last_update": data.get("last_update"),
        }


class EGLMonthlySensor(_EGLBaseSensor):
    """Cumul du mois en cours (en litres)."""

    _attr_icon = "mdi:water-outline"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: EGLDataCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_monthly"
        self._attr_name = "Consommation mensuelle"

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if not data:
            return None
        return data.get("monthly_liters")


class EGLRolling30dSensor(_EGLBaseSensor):
    """Consommation glissante sur 30 jours (en litres)."""

    _attr_icon = "mdi:calendar-month"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: EGLDataCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_rolling_30d"
        self._attr_name = "Consommation 30 derniers jours"

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if not data:
            return None
        return data.get("rolling_30d_liters")
