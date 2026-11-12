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
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import EGLApiError, EGLAuthError, EGLClient, EGLContract
from .const import (
    CONF_CONTRACT_TOKEN,
    CONF_COST_IMPORTED,
    CONF_HISTORY_IMPORTED,
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


class NoActiveContractError(Exception):
    """Le compte EGL ne possède aucun contrat actif."""


async def _check_credentials(hass: HomeAssistant, username: str, password: str) -> None:
    """Vérifie uniquement les identifiants.

    Lève EGLAuthError (identifiants refusés) ou EGLApiError (EGL injoignable).
    """
    session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
    client = EGLClient(session, username, password)
    try:
        await client.authenticate()
    finally:
        await client.close()


async def _fetch_active_contracts(
    hass: HomeAssistant, username: str, password: str
) -> list[EGLContract]:
    """Vérifie les identifiants et retourne les contrats ACTIFS du compte.

    Lève EGLAuthError, EGLApiError ou NoActiveContractError (compte sans contrat
    actif, par exemple tous cessés). Les contrats cessés sont écartés : EGL
    n'y renvoie aucune consommation, et l'ordre de la liste n'est pas fiable.
    """
    session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
    client = EGLClient(session, username, password)
    try:
        await client.authenticate()
        contracts = await client.get_contracts()
    finally:
        await client.close()
    active = [c for c in contracts if c.active]
    if not active:
        raise NoActiveContractError
    return active


def _format_date(iso_date: str) -> str:
    """AAAA-MM-JJ -> JJ/MM/AAAA."""
    year, month, day = iso_date.split("-")
    return f"{day}/{month}/{year}"


def _contract_options(contracts: list[EGLContract]) -> list[SelectOptionDict]:
    """Libellés lisibles, sans donnée personnelle (le token reste la valeur interne)."""
    options: list[SelectOptionDict] = []
    for number, contract in enumerate(contracts, start=1):
        label = f"Contrat n°{number}"
        if contract.start_date:
            label += f" – depuis le {_format_date(contract.start_date)}"
        options.append(SelectOptionDict(value=contract.token, label=label))
    return options


def _contract_schema(
    contracts: list[EGLContract], default: str | None = None
) -> vol.Schema:
    """Formulaire de choix du contrat parmi les contrats actifs."""
    key = (
        vol.Required(CONF_CONTRACT_TOKEN, default=default)
        if default
        else vol.Required(CONF_CONTRACT_TOKEN)
    )
    return vol.Schema({
        key: SelectSelector(
            SelectSelectorConfig(
                options=_contract_options(contracts),
                mode=SelectSelectorMode.LIST,
            )
        )
    })


class EGLConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configuration de l'intégration."""

    VERSION = 1

    def __init__(self) -> None:
        self._user_input: dict[str, Any] = {}
        self._contracts: list[EGLContract] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                contracts = await _fetch_active_contracts(
                    self.hass, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except EGLAuthError:
                errors["base"] = "invalid_auth"
            except NoActiveContractError:
                errors["base"] = "no_active_contract"
            except EGLApiError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Erreur inattendue lors de la validation")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
                self._abort_if_unique_id_configured()
                self._user_input = user_input
                if len(contracts) == 1:
                    # Un seul contrat actif : rien à choisir.
                    return self._create_entry(contracts[0].token)
                self._contracts = contracts
                return await self.async_step_contract()
        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_contract(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Plusieurs contrats actifs : l'utilisateur choisit celui à suivre."""
        if user_input is not None:
            return self._create_entry(user_input[CONF_CONTRACT_TOKEN])
        return self.async_show_form(
            step_id="contract",
            data_schema=_contract_schema(self._contracts),
        )

    def _create_entry(self, contract_token: str) -> ConfigFlowResult:
        return self.async_create_entry(
            title=f"EGL – {self._user_input[CONF_USERNAME]}",
            data={**self._user_input, CONF_CONTRACT_TOKEN: contract_token},
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
        """Demande le nouveau mot de passe et le valide.

        Le contrat choisi n'est pas modifié ici (voir « Reconfigurer »).
        """
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        username = reauth_entry.data[CONF_USERNAME]

        if user_input is not None:
            try:
                await _check_credentials(self.hass, username, user_input[CONF_PASSWORD])
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
                    data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={"username": username},
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Reconfiguration : changer de contrat sans supprimer l'intégration
    # (contrat cessé, déménagement, nouveau contrat...)
    # ------------------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choix du contrat parmi les contrats actifs du compte."""
        entry = self._get_reconfigure_entry()

        if not self._contracts:
            try:
                self._contracts = await _fetch_active_contracts(
                    self.hass, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
                )
            except EGLAuthError:
                return self.async_abort(reason="invalid_auth")
            except NoActiveContractError:
                return self.async_abort(reason="no_active_contract")
            except EGLApiError:
                return self.async_abort(reason="cannot_connect")
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Erreur inattendue lors de la reconfiguration")
                return self.async_abort(reason="unknown")

        if user_input is not None:
            new_token = user_input[CONF_CONTRACT_TOKEN]
            data_updates: dict[str, Any] = {CONF_CONTRACT_TOKEN: new_token}
            if new_token != entry.data.get(CONF_CONTRACT_TOKEN):
                # Autre contrat : l'historique (volume + coût) doit être ré-importé.
                data_updates[CONF_HISTORY_IMPORTED] = False
                self.hass.config_entries.async_update_entry(
                    entry,
                    options={**entry.options, CONF_COST_IMPORTED: False},
                )
            return self.async_update_reload_and_abort(entry, data_updates=data_updates)

        current = entry.data.get(CONF_CONTRACT_TOKEN)
        tokens = [c.token for c in self._contracts]
        default = current if current in tokens else (tokens[0] if len(tokens) == 1 else None)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_contract_schema(self._contracts, default),
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
