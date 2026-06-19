"""Config flow pour l'intégration Eau du Grand Lyon."""
from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback

from .api import EGLAuthError, EGLClient
from .const import (
    CONF_PASSWORD,
    CONF_PRICE_PER_M3,
    CONF_UPDATE_TIME_1,
    CONF_UPDATE_TIME_2,
    CONF_USERNAME,
    DEFAULT_PRICE_PER_M3,
    DEFAULT_UPDATE_TIME_1,
    DEFAULT_UPDATE_TIME_2,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_RE_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _validate_hhmm(value: str) -> str:
    value = value.strip()
    if not _RE_HHMM.match(value):
        raise vol.Invalid("Format invalide, attendu HH:MM (ex : 08:00)")
    h, m = value.split(":")
    return f"{int(h):02d}:{m}"


STEP_USER_SCHEMA = vol.Schema({
    vol.Required(CONF_USERNAME): str,
    vol.Required(CONF_PASSWORD): str,
})


async def _validate_credentials(hass: HomeAssistant, data: dict) -> dict:
    client = EGLClient(data[CONF_USERNAME], data[CONF_PASSWORD])
    try:
        await client.authenticate()
        contract_token = await client.get_contract_token()
        return {"contract_token": contract_token}
    finally:
        await client.close()


class EGLConfigFlow(ConfigFlow, domain=DOMAIN):
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
    """Permet de modifier les horaires et le tarif."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        opts = self._config_entry.options
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                t1 = _validate_hhmm(user_input[CONF_UPDATE_TIME_1])
                t2 = _validate_hhmm(user_input[CONF_UPDATE_TIME_2])
            except vol.Invalid:
                errors["base"] = "invalid_time_format"
            else:
                if t1 == t2:
                    errors["base"] = "same_times"
                else:
                    opts = self._config_entry.options
                    old_price = opts.get(CONF_PRICE_PER_M3, DEFAULT_PRICE_PER_M3)
                    new_price = user_input[CONF_PRICE_PER_M3]
                    # Si le tarif a changé, on réinitialise cost_imported pour
                    # déclencher un recalcul rétroactif via _async_update_options
                    from .const import CONF_COST_IMPORTED
                    cost_imported = opts.get(CONF_COST_IMPORTED, False)
                    if abs(new_price - old_price) > 0.0001:
                        cost_imported = False
                    return self.async_create_entry(
                        title="",
                        data={
                            CONF_UPDATE_TIME_1: t1,
                            CONF_UPDATE_TIME_2: t2,
                            CONF_PRICE_PER_M3: new_price,
                            CONF_COST_IMPORTED: cost_imported,
                        },
                    )

        schema = vol.Schema({
            vol.Required(
                CONF_UPDATE_TIME_1,
                default=opts.get(CONF_UPDATE_TIME_1, DEFAULT_UPDATE_TIME_1),
            ): str,
            vol.Required(
                CONF_UPDATE_TIME_2,
                default=opts.get(CONF_UPDATE_TIME_2, DEFAULT_UPDATE_TIME_2),
            ): str,
            vol.Required(
                CONF_PRICE_PER_M3,
                default=opts.get(CONF_PRICE_PER_M3, DEFAULT_PRICE_PER_M3),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.01, max=50.0)),
        })

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )
