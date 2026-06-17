"""Config flow pour l'intégration Eau du Grand Lyon."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback

from .api import EGLAuthError, EGLClient
from .const import (
    CONF_PASSWORD,
    CONF_UPDATE_HOUR_1,
    CONF_UPDATE_HOUR_2,
    CONF_UPDATE_MINUTE_1,
    CONF_UPDATE_MINUTE_2,
    CONF_USERNAME,
    DEFAULT_UPDATE_HOUR_1,
    DEFAULT_UPDATE_HOUR_2,
    DEFAULT_UPDATE_MINUTE_1,
    DEFAULT_UPDATE_MINUTE_2,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema({
    vol.Required(CONF_USERNAME): str,
    vol.Required(CONF_PASSWORD): str,
})


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
    """Gère le flux de configuration via l'interface HA."""

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
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EGLOptionsFlow(config_entry)


class EGLOptionsFlow(OptionsFlow):
    """Permet de modifier les horaires de téléchargement."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        opts = self._config_entry.options

        if user_input is not None:
            # Validation : les deux horaires doivent être différents
            t1 = (user_input[CONF_UPDATE_HOUR_1], user_input[CONF_UPDATE_MINUTE_1])
            t2 = (user_input[CONF_UPDATE_HOUR_2], user_input[CONF_UPDATE_MINUTE_2])
            if t1 == t2:
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._build_schema(user_input),
                    errors={"base": "same_times"},
                )
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=self._build_schema(opts),
        )

    def _build_schema(self, values: dict) -> vol.Schema:
        return vol.Schema({
            vol.Required(
                CONF_UPDATE_HOUR_1,
                default=values.get(CONF_UPDATE_HOUR_1, DEFAULT_UPDATE_HOUR_1),
            ): vol.All(int, vol.Range(min=0, max=23)),
            vol.Required(
                CONF_UPDATE_MINUTE_1,
                default=values.get(CONF_UPDATE_MINUTE_1, DEFAULT_UPDATE_MINUTE_1),
            ): vol.All(int, vol.Range(min=0, max=59)),
            vol.Required(
                CONF_UPDATE_HOUR_2,
                default=values.get(CONF_UPDATE_HOUR_2, DEFAULT_UPDATE_HOUR_2),
            ): vol.All(int, vol.Range(min=0, max=23)),
            vol.Required(
                CONF_UPDATE_MINUTE_2,
                default=values.get(CONF_UPDATE_MINUTE_2, DEFAULT_UPDATE_MINUTE_2),
            ): vol.All(int, vol.Range(min=0, max=59)),
        })
