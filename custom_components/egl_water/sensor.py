"""Capteurs Home Assistant pour Eau du Grand Lyon.

Ces capteurs sont purement INFORMATIFS : aucun n'est un compteur cumulatif
(TOTAL / TOTAL_INCREASING), donc aucun n'a de state_class et aucun ne génère de
statistiques à long terme. Les données destinées au tableau de bord Énergie sont
les statistiques externes `egl_water:<slug>_daily` et `egl_water:<slug>_daily_cost`
(voir history_import.py).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import CURRENCY_EURO, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EGLConfigEntry, EGLDataCoordinator


def _cost(liters: float | None, price_per_m3: float) -> float | None:
    """Convertit des litres en euros (arrondi à 4 décimales)."""
    if liters is None:
        return None
    return round(liters / 1000 * price_per_m3, 4)


@dataclass(frozen=True, kw_only=True)
class EGLSensorDescription(SensorEntityDescription):
    """Description d'un capteur EGL."""

    value_fn: Callable[[dict[str, Any], float], float | None]
    attrs_fn: Callable[[dict[str, Any], float], dict[str, Any]] | None = None


_WATER = {
    "device_class": SensorDeviceClass.WATER,
    "native_unit_of_measurement": UnitOfVolume.LITERS,
    "suggested_display_precision": 0,
}
_COST = {
    "device_class": SensorDeviceClass.MONETARY,
    "native_unit_of_measurement": CURRENCY_EURO,
    "suggested_display_precision": 2,
    "icon": "mdi:currency-eur",
}

# Les `key` forment la fin des unique_id (`<entry_id>_<key>`) : ne pas les renommer.
SENSORS: tuple[EGLSensorDescription, ...] = (
    EGLSensorDescription(
        key="daily",
        translation_key="daily",
        icon="mdi:water",
        value_fn=lambda d, _p: d.get("daily_liters"),
        attrs_fn=lambda d, _p: {
            "date": d.get("daily_date"),
            "retard_publication_jours": d.get("daily_lag_days"),
            "last_update": d.get("last_update"),
        },
        **_WATER,
    ),
    EGLSensorDescription(
        key="monthly",
        translation_key="monthly",
        icon="mdi:water-outline",
        value_fn=lambda d, _p: d.get("monthly_liters"),
        **_WATER,
    ),
    EGLSensorDescription(
        key="rolling_30d",
        translation_key="rolling_30d",
        icon="mdi:calendar-month",
        value_fn=lambda d, _p: d.get("rolling_30d_liters"),
        **_WATER,
    ),
    EGLSensorDescription(
        key="daily_cost",
        translation_key="daily_cost",
        value_fn=lambda d, p: _cost(d.get("daily_liters"), p),
        attrs_fn=lambda d, p: {"tarif_euro_par_m3": p, "date": d.get("daily_date")},
        **_COST,
    ),
    EGLSensorDescription(
        key="monthly_cost",
        translation_key="monthly_cost",
        value_fn=lambda d, p: _cost(d.get("monthly_liters"), p),
        attrs_fn=lambda d, p: {"tarif_euro_par_m3": p},
        **_COST,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EGLConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Configure les capteurs."""
    coordinator = entry.runtime_data
    async_add_entities(EGLSensor(coordinator, entry, desc) for desc in SENSORS)


class EGLSensor(CoordinatorEntity[EGLDataCoordinator], SensorEntity):
    """Capteur EGL piloté par une description."""

    entity_description: EGLSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EGLDataCoordinator,
        entry: EGLConfigEntry,
        description: EGLSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Eau du Grand Lyon",
            manufacturer="Eau du Grand Lyon",
            model="Compteur Téléo",
        )

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if not data:
            return None
        return self.entity_description.value_fn(data, self.coordinator.price_per_m3)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        if fn is None:
            return None
        return fn(self.coordinator.data or {}, self.coordinator.price_per_m3)
