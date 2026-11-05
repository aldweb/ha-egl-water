"""Intégration Eau du Grand Lyon pour Home Assistant."""
from __future__ import annotations

import logging

import aiohttp
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import EGLApiError, EGLAuthError, EGLClient
from .const import (
    CONF_CONTRACT_TOKEN,
    CONF_COST_IMPORTED,
    CONF_HISTORY_IMPORTED,
    PLATFORMS,
)
from .coordinator import EGLConfigEntry, EGLDataCoordinator
from .history_import import async_import_history

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: EGLConfigEntry) -> bool:
    """Initialise l'intégration depuis une config entry."""
    # Le login EGL repose sur un cookie de session : session dédiée avec son cookie jar
    # (la session partagée de Home Assistant n'en conserve pas).
    session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
    client = EGLClient(session, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    entry.async_on_unload(client.close)

    coordinator = EGLDataCoordinator(hass, entry, client, entry.data[CONF_CONTRACT_TOKEN])

    # ConfigEntryAuthFailed (identifiants refusés) lance la réauth ;
    # ConfigEntryNotReady (API indisponible) programme un nouvel essai automatique.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    coordinator.async_start_schedule()
    entry.async_on_unload(coordinator.async_stop_schedule)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Import (ou ré-import) de l'historique en tâche de fond. Le flag
    # cost_imported est dans les OPTIONS : l'options flow le remet à False quand le
    # tarif change, et l'entrée est alors rechargée (OptionsFlowWithReload).
    if not entry.data.get(CONF_HISTORY_IMPORTED) or not entry.options.get(CONF_COST_IMPORTED):
        entry.async_create_background_task(
            hass,
            _async_run_import(hass, entry, coordinator),
            name="egl_water_history_import",
        )

    return True


async def _async_run_import(
    hass: HomeAssistant, entry: EGLConfigEntry, coordinator: EGLDataCoordinator
) -> None:
    """Importe 2 ans de volume + coût, puis pose les flags (uniquement en cas de succès)."""
    _LOGGER.info("EGL: lancement de l'import historique (volume + coût)")
    try:
        count, _last_date = await async_import_history(
            hass,
            coordinator.client,
            coordinator.contract_token,
            coordinator.statistics_slug,
            price_per_m3=coordinator.price_per_m3,
        )
    except EGLAuthError:
        _LOGGER.error("EGL: identifiants refusés pendant l'import, ré-authentification requise")
        entry.async_start_reauth(hass)
        return
    except EGLApiError as err:
        _LOGGER.warning("EGL: import historique impossible pour le moment : %s", err)
        return
    except Exception:  # noqa: BLE001  (tâche de fond : ne rien laisser échapper silencieusement)
        _LOGGER.exception("EGL: échec inattendu de l'import historique")
        return

    if count == 0:
        # Aucun jour récupéré (API indisponible ?) : on ne pose pas les flags,
        # l'import sera retenté au prochain démarrage.
        _LOGGER.warning("EGL: aucune donnée importée, nouvel essai au prochain démarrage")
        return

    # Aucun update listener n'est enregistré : poser ces flags ne recharge pas l'entrée.
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_HISTORY_IMPORTED: True},
        options={**entry.options, CONF_COST_IMPORTED: True},
    )
    _LOGGER.info("EGL: import terminé (%d jours), flags posés", count)


async def async_unload_entry(hass: HomeAssistant, entry: EGLConfigEntry) -> bool:
    """Décharge une config entry (timers et session fermés via async_on_unload)."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
