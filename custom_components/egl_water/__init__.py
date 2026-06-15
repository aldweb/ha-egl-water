"""Intégration Eau du Grand Lyon pour Home Assistant."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import EGLClient
from .const import CONF_PASSWORD, CONF_USERNAME, CONF_CONTRACT_TOKEN, DOMAIN, PLATFORMS
from .coordinator import EGLDataCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Initialise l'intégration depuis une config entry."""
    client = EGLClient(
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )
    contract_token = entry.data[CONF_CONTRACT_TOKEN]
    coordinator = EGLDataCoordinator(hass, client, contract_token)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Décharge une config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: EGLDataCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator._client.close()
    return unloaded
