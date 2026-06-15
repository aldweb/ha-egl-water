"""Config flow et options flow pour l'intégration Eau du Grand Lyon."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .api import EGLAuthError, EGLClient
from .const import (
    AVAILABLE_HOURS_UTC,
    CONF_PASSWORD,
    CONF_REIMPORT,
    CONF_UPDATE_HOUR_1,
    CONF_UPDATE_HOUR_2,
    CONF_USERNAME,
    DEFAULT_UPDATE_HOUR_1,
    DEFAULT_UPDATE_HOUR_2,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema({
    vol.Required(CONF_USERNAME): str,
    vol.Required(CONF_PASSWORD): str,
})


def _hour_selector() -> selector.SelectSelector:
    """Sélecteur d'heure UTC avec libellé CEST (+2h en été)."""
    options = [
        selector.SelectOptionDict(
            value=str(h),
            label=f"{h:02d}:00 UTC  →  {(h + 2) % 24:02d}:00 CEST",
        )
        for h in AVAILABLE_HOURS_UTC
    ]
    return selector.SelectSelector(
        selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.DROPDOWN)
    )


async def _validate_credentials(hass: HomeAssistant, data: dict) -> dict:
    """Tente une authentification et récupère le token de contrat."""
    client = EGLClient(data[CONF_USERNAME], data[CONF_PASSWORD])
    try:
        await client.authenticate()
        contract_token = await client.get_contract_token()
        return {"contract_token": contract_token}
    finally:
        await client.close()


class EGLConfigFlow(ConfigFlow, domain=DOMAIN):
    """Gère le flux de configuration initiale via l'interface HA."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                extra = await _validate_credentials(self.hass, user_input)
            except EGLAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Erreur inattendue lors de la validation")
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"EGL – {user_input[CONF_USERNAME]}",
                    data={**user_input, **extra},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "EGLOptionsFlow":
        return EGLOptionsFlow(config_entry)


class EGLOptionsFlow(OptionsFlow):
    """Options flow : horaires de téléchargement + réimport historique."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        current_opts = self._config_entry.options
        history_imported = self._config_entry.data.get("history_imported", False)

        if user_input is not None:
            opts: dict[str, Any] = {
                CONF_UPDATE_HOUR_1: int(user_input[CONF_UPDATE_HOUR_1]),
                CONF_UPDATE_HOUR_2: int(user_input[CONF_UPDATE_HOUR_2]),
            }
            # Le flag reimport n'est disponible que si l'historique a déjà été importé
            if history_imported and user_input.get(CONF_REIMPORT, False):
                opts[CONF_REIMPORT] = True
                _LOGGER.info(
                    "EGL: réimport de l'historique demandé — sera effectué au prochain démarrage"
                )

            return self.async_create_entry(title="", data=opts)

        h1 = current_opts.get(CONF_UPDATE_HOUR_1, DEFAULT_UPDATE_HOUR_1)
        h2 = current_opts.get(CONF_UPDATE_HOUR_2, DEFAULT_UPDATE_HOUR_2)

        fields: dict = {
            vol.Required(CONF_UPDATE_HOUR_1, default=str(h1)): _hour_selector(),
            vol.Required(CONF_UPDATE_HOUR_2, default=str(h2)): _hour_selector(),
        }
        # Proposer le réimport uniquement si l'historique a déjà été importé au moins une fois
        if history_imported:
            fields[vol.Optional(CONF_REIMPORT, default=False)] = selector.BooleanSelector()

        schema = vol.Schema(fields)

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
        )
