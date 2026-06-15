"""Intégration Eau du Grand Lyon pour Home Assistant."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import EGLClient
from .const import (
    CONF_CONTRACT_TOKEN,
    CONF_HISTORY_IMPORTED,
    CONF_LAST_KNOWN_DATE,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import EGLDataCoordinator
from .history_import import async_import_history

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Initialise l'intégration depuis une config entry."""
    client = EGLClient(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    contract_token = entry.data[CONF_CONTRACT_TOKEN]

    # On passe entry au coordinator pour qu'il puisse persister last_known_date
    coordinator = EGLDataCoordinator(hass, entry, client, contract_token)
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_start_schedule()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Import historique initial : une seule fois, en tâche de fond
    if not entry.data.get(CONF_HISTORY_IMPORTED, False):
        hass.async_create_task(
            _async_run_history_import(hass, entry, client, contract_token)
        )

    return True


async def _async_run_history_import(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: EGLClient,
    contract_token: str,
) -> None:
    """Import initial en tâche de fond. Pose le flag et mémorise la dernière date."""
    sensor_unique_id = f"{entry.entry_id}_daily"
    try:
        count, last_date = await async_import_history(
            hass, client, contract_token, sensor_unique_id
        )
        _LOGGER.info("EGL: %d jours d'historique importés (dernier : %s)", count, last_date)
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("EGL: échec de l'import historique : %s", err)
        return  # flag non posé → nouvel essai au prochain démarrage

    # Marquer l'import comme effectué et mémoriser la dernière date connue
    new_data = {
        **entry.data,
        CONF_HISTORY_IMPORTED: True,
    }
    if last_date:
        new_data[CONF_LAST_KNOWN_DATE] = last_date

    hass.config_entries.async_update_entry(entry, data=new_data)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Décharge une config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: EGLDataCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.async_stop_schedule()
        await coordinator._client.close()
    return unloaded
