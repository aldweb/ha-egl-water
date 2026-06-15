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
    CONF_REIMPORT,
    CONF_USERNAME,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import EGLDataCoordinator
from .history_import import async_clear_history, async_import_history

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Initialise l'intégration depuis une config entry."""
    client = EGLClient(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    contract_token = entry.data[CONF_CONTRACT_TOKEN]

    coordinator = EGLDataCoordinator(hass, entry, client, contract_token)
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_start_schedule()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Réimport demandé depuis l'options flow : purge + réimport complet
    if entry.options.get(CONF_REIMPORT, False):
        _LOGGER.info("EGL: réimport historique demandé via les options")
        hass.async_create_task(
            _async_run_reimport(hass, entry, client, contract_token, coordinator)
        )
    # Import initial : une seule fois au premier démarrage
    elif not entry.data.get(CONF_HISTORY_IMPORTED, False):
        _LOGGER.info("EGL: premier démarrage — lancement de l'import historique initial")
        hass.async_create_task(
            _async_run_history_import(hass, entry, client, contract_token, coordinator)
        )

    # Replanifier les refreshs si les options ont changé
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Appelé quand les options changent : recharge l'entrée pour appliquer les nouveaux horaires."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_run_history_import(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: EGLClient,
    contract_token: str,
    coordinator: EGLDataCoordinator,
) -> None:
    """Import initial en tâche de fond. Pose le flag et mémorise la dernière date."""
    try:
        count, last_date = await async_import_history(
            hass, client, contract_token, coordinator._sensor_unique_id
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("EGL: échec de l'import historique initial : %s", err)
        return  # flag non posé → nouvel essai au prochain démarrage

    new_data = {
        **entry.data,
        CONF_HISTORY_IMPORTED: True,
    }
    if last_date:
        new_data[CONF_LAST_KNOWN_DATE] = last_date

    hass.config_entries.async_update_entry(entry, data=new_data)
    _LOGGER.info(
        "EGL: import historique initial terminé — %d jour(s), dernier : %s",
        count,
        last_date,
    )


async def _async_run_reimport(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: EGLClient,
    contract_token: str,
    coordinator: EGLDataCoordinator,
) -> None:
    """Réimport complet demandé depuis l'options flow : purge + téléchargement."""
    try:
        count, last_date = await async_import_history(
            hass, client, contract_token, coordinator._sensor_unique_id
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("EGL: échec du réimport historique : %s", err)
        # On retire le flag reimport même en cas d'échec pour ne pas reboucler
        _clear_reimport_flag(hass, entry)
        return

    # Mettre à jour data + retirer le flag reimport des options
    new_data = {
        **entry.data,
        CONF_HISTORY_IMPORTED: True,
    }
    if last_date:
        new_data[CONF_LAST_KNOWN_DATE] = last_date
    hass.config_entries.async_update_entry(entry, data=new_data)

    _clear_reimport_flag(hass, entry)
    _LOGGER.info(
        "EGL: réimport historique terminé — %d jour(s), dernier : %s",
        count,
        last_date,
    )


def _clear_reimport_flag(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Retire le flag CONF_REIMPORT des options pour éviter une boucle au prochain démarrage."""
    new_options = {k: v for k, v in entry.options.items() if k != CONF_REIMPORT}
    hass.config_entries.async_update_entry(entry, options=new_options)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Décharge une config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: EGLDataCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.async_stop_schedule()
        await coordinator._client.close()
    return unloaded
