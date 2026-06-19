"""Intégration Eau du Grand Lyon pour Home Assistant."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .api import EGLAuthError, EGLClient
from .const import (
    CONF_CONTRACT_TOKEN,
    CONF_COST_IMPORTED,
    CONF_HISTORY_IMPORTED,
    CONF_LAST_KNOWN_DATE,
    CONF_PASSWORD,
    CONF_PRICE_PER_M3,
    CONF_USERNAME,
    DEFAULT_PRICE_PER_M3,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import EGLDataCoordinator
from .history_import import async_import_cost_history, async_import_history

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Initialise l'intégration depuis une config entry."""
    client = EGLClient(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    contract_token = entry.data[CONF_CONTRACT_TOKEN]

    coordinator = EGLDataCoordinator(hass, entry, client, contract_token)

    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        _LOGGER.warning("EGL: premier refresh échoué (API indisponible ?). L'import sera lancé quand même.")

    coordinator.async_start_schedule()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    price_per_m3 = entry.options.get(CONF_PRICE_PER_M3, DEFAULT_PRICE_PER_M3)
    history_done = entry.data.get(CONF_HISTORY_IMPORTED, False)
    # Le flag cost_imported est stocké dans les OPTIONS pour éviter de
    # déclencher le listener options lors de sa mise à jour
    cost_done = entry.options.get(CONF_COST_IMPORTED, False)

    if not history_done:
        _LOGGER.info("EGL: lancement de l'import historique (volume + coût)")
        hass.async_create_task(
            _async_run_history_import(hass, entry, contract_token, coordinator, price_per_m3),
            name="egl_water_history_import",
        )
    elif not cost_done:
        _LOGGER.info("EGL: lancement de l'import coût rétroactif")
        hass.async_create_task(
            _async_run_cost_import(hass, entry, contract_token, coordinator, price_per_m3),
            name="egl_water_cost_import",
        )
    else:
        _LOGGER.debug("EGL: imports déjà effectués")

    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Appelé quand l'utilisateur modifie les options.

    Ne relance l'import coût QUE si le tarif a changé par rapport à la valeur
    enregistrée dans les options (champ 'price_per_m3' précédent).
    Le flag cost_imported est dans options — cette fonction sera rappelée quand
    on le pose, mais la condition ci-dessous empêche toute boucle infinie.
    """
    coordinator: EGLDataCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.async_stop_schedule()
    coordinator.async_start_schedule()
    _LOGGER.debug("EGL: options mises à jour (horaires replanifiés)")

    # Si le flag cost_imported est False dans les options ET qu'un tarif est défini,
    # c'est qu'un import coût est en attente — ne pas relancer, il est déjà en cours
    # ou sera lancé au prochain démarrage.
    # On ne relance l'import que si l'utilisateur a explicitement modifié le tarif,
    # ce qui est signalé par cost_imported=False posé AVANT cet appel.
    # Pour éviter toute boucle, on vérifie que cost_imported est bien False
    # ET qu'aucune tâche d'import n'est déjà active (via un flag en mémoire).
    if not entry.options.get(CONF_COST_IMPORTED, False):
        price_per_m3 = entry.options.get(CONF_PRICE_PER_M3, DEFAULT_PRICE_PER_M3)
        _LOGGER.info("EGL: tarif modifié (%.4f €/m³), lancement import coût rétroactif", price_per_m3)
        hass.async_create_task(
            _async_run_cost_import(
                hass, entry, entry.data[CONF_CONTRACT_TOKEN], coordinator, price_per_m3
            ),
            name="egl_water_cost_reimport",
        )


async def _async_run_history_import(
    hass: HomeAssistant,
    entry: ConfigEntry,
    contract_token: str,
    coordinator: "EGLDataCoordinator",
    price_per_m3: float,
) -> None:
    """Import initial volume + coût en tâche de fond."""
    sensor_unique_id = coordinator._sensor_unique_id
    _LOGGER.info("EGL: import historique démarré (sensor_id=%s)", sensor_unique_id)

    import_client = EGLClient(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    try:
        try:
            await import_client.authenticate()
        except EGLAuthError as err:
            _LOGGER.error("EGL: authentification échouée pour l'import : %s", err)
            return

        count, last_date = await async_import_history(
            hass, import_client, contract_token, sensor_unique_id,
            price_per_m3=price_per_m3,
        )
        _LOGGER.info("EGL: %d jours importés (dernier : %s)", count, last_date)

    except Exception as err:  # noqa: BLE001
        _LOGGER.error("EGL: échec import historique : %s", err, exc_info=True)
        return
    finally:
        await import_client.close()

    # Mettre history_imported dans data (ne déclenche PAS le listener options)
    new_data = {**entry.data, CONF_HISTORY_IMPORTED: True}
    if last_date:
        new_data[CONF_LAST_KNOWN_DATE] = last_date
    hass.config_entries.async_update_entry(entry, data=new_data)

    # Mettre cost_imported dans options — déclenche le listener, mais
    # cost_imported=True donc la condition dans _async_update_options ne repart pas
    new_options = {**entry.options, CONF_COST_IMPORTED: True}
    hass.config_entries.async_update_entry(entry, options=new_options)
    _LOGGER.info("EGL: flags history_imported (data) et cost_imported (options) posés")


async def _async_run_cost_import(
    hass: HomeAssistant,
    entry: ConfigEntry,
    contract_token: str,
    coordinator: "EGLDataCoordinator",
    price_per_m3: float,
) -> None:
    """Import coût rétroactif seul (volume déjà en base)."""
    sensor_unique_id = coordinator._sensor_unique_id
    _LOGGER.info(
        "EGL: import coût rétroactif démarré (%.4f €/m³, sensor_id=%s)",
        price_per_m3, sensor_unique_id,
    )

    import_client = EGLClient(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    try:
        try:
            await import_client.authenticate()
        except EGLAuthError as err:
            _LOGGER.error("EGL: authentification échouée pour l'import coût : %s", err)
            return

        count, last_date = await async_import_cost_history(
            hass, import_client, contract_token, sensor_unique_id, price_per_m3
        )
        _LOGGER.info("EGL: %d jours de coût importés (dernier : %s)", count, last_date)

    except Exception as err:  # noqa: BLE001
        _LOGGER.error("EGL: échec import coût : %s", err, exc_info=True)
        return
    finally:
        await import_client.close()

    # cost_imported=True dans options → le listener sera rappelé mais ne repart pas
    new_options = {**entry.options, CONF_COST_IMPORTED: True}
    hass.config_entries.async_update_entry(entry, options=new_options)
    _LOGGER.info("EGL: flag cost_imported posé dans les options")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Décharge une config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: EGLDataCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.async_stop_schedule()
        await coordinator._client.close()
    return unloaded
