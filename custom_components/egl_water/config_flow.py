"""Config flow pour l'intégration Eau du Grand Lyon."""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import EGLApiError, EGLAuthError, EGLClient
from .const import (
    CONF_CONTRACT_TOKEN,
    CONF_COST_IMPORTED,
    CONF_PRICE_PER_M3,
    CONF_UPDATE_TIME_1,
    CONF_UPDATE_TIME_2,
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

STEP_REAUTH_SCHEMA = vol.Schema({
    vol.Required(CONF_PASSWORD): str,
})


async def _validate_credentials(hass: HomeAssistant, username: str, password: str) -> str:
    """Vérifie les identifiants et retourne le token de contrat.

    Lève EGLAuthError (identifiants refusés) ou EGLApiError (EGL injoignable).
    """
    session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
    client = EGLClient(session, username, password)
    try:
        await client.authenticate()
        return await client.get_contract_token()
    finally:
        await client.close()


class EGLConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configuration de l'intégration."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                contract_token = await _validate_credentials(
                    self.hass, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except EGLAuthError:
                errors["base"] = "invalid_auth"
            except EGLApiError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Erreur inattendue lors de la validation")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"EGL – {user_input[CONF_USERNAME]}",
                    data={**user_input, CONF_CONTRACT_TOKEN: contract_token},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Ré-authentification (mot de passe changé côté EGL)
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Point d'entrée déclenché par ConfigEntryAuthFailed."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Demande le nouveau mot de passe et le valide."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        username = reauth_entry.data[CONF_USERNAME]

        if user_input is not None:
            try:
                contract_token = await _validate_credentials(
                    self.hass, username, user_input[CONF_PASSWORD]
                )
            except EGLAuthError:
                errors["base"] = "invalid_auth"
            except EGLApiError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Erreur inattendue lors de la ré-authentification")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates={
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_CONTRACT_TOKEN: contract_token,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={"username": username},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EGLOptionsFlow:
        return EGLOptionsFlow()


class EGLOptionsFlow(OptionsFlowWithReload):
    """Permet de modifier les horaires et le tarif.

    OptionsFlowWithReload recharge l'entrée à la validation : les horaires sont
    replanifiés et, si le tarif a changé, le recalcul rétroactif du coût est relancé.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        opts = self.config_entry.options
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
                    old_price = opts.get(CONF_PRICE_PER_M3, DEFAULT_PRICE_PER_M3)
                    new_price = user_input[CONF_PRICE_PER_M3]
                    cost_imported = opts.get(CONF_COST_IMPORTED, False)
                    # Tarif modifié : on remet le flag à False, le rechargement de
                    # l'entrée relance alors le recalcul rétroactif du coût.
                    if abs(new_price - old_price) > 0.0001:
                        cost_imported = False
                    return self.async_create_entry(
                        title="",
                        data={
                            **opts,
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
