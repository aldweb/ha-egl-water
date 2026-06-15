"""Coordinator pour la mise à jour des données EGL.

Scheduling :
  Deux déclencheurs fixes par jour (UPDATE_TIMES_UTC), pas d'intervalle dérivant.

Stratégie de fetch incrémental :
  À chaque refresh on interroge l'API depuis (last_known_date - FETCH_OVERLAP_DAYS)
  jusqu'à aujourd'hui. L'overlap de 10 jours absorbe les publications groupées
  irrégulières d'EGL (ex: vendredi+samedi publiés le mardi suivant).
  Tous les jours nouvellement publiés — quel que soit leur nombre — sont poussés
  dans recorder via async_push_new_entries, et last_known_date est mis à jour.

Cumuls exposés aux capteurs :
  - daily_liters / daily_date   : dernier jour disponible (> 0 litres)
  - daily_lag_days              : écart entre ce jour et aujourd'hui
  - monthly_liters              : cumul du mois calendaire en cours
  - rolling_30d_liters          : fenêtre glissante 30 j
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EGLApiError, EGLAuthError, EGLClient
from .const import (
    CONF_LAST_KNOWN_DATE,
    DOMAIN,
    FETCH_MONTHLY_DAYS,
    FETCH_OVERLAP_DAYS,
    UPDATE_TIMES_UTC,
)
from .history_import import async_push_new_entries

_LOGGER = logging.getLogger(__name__)


class EGLDataCoordinator(DataUpdateCoordinator):
    """Récupère et met en cache les données de consommation EGL."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EGLClient,
        contract_token: str,
    ) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=None)
        self._entry = entry
        self._client = client
        self._contract_token = contract_token
        self._sensor_unique_id = f"{entry.entry_id}_daily"
        self._unsub_timers: list[Any] = []

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def async_start_schedule(self) -> None:
        for hour, minute in UPDATE_TIMES_UTC:
            unsub = async_track_time_change(
                self.hass, self._async_scheduled_refresh,
                hour=hour, minute=minute, second=0,
            )
            self._unsub_timers.append(unsub)
            _LOGGER.debug("EGL: refresh planifié à %02d:%02d UTC", hour, minute)

    def async_stop_schedule(self) -> None:
        for unsub in self._unsub_timers:
            unsub()
        self._unsub_timers.clear()

    @callback
    def _async_scheduled_refresh(self, now: datetime) -> None:
        _LOGGER.debug("EGL: déclenchement planifié à %s UTC", now.strftime("%H:%M"))
        self.hass.async_create_task(self.async_refresh())

    # ------------------------------------------------------------------
    # Récupération des données
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict:
        now = datetime.now(timezone.utc)
        last_known_date: str | None = self._entry.data.get(CONF_LAST_KNOWN_DATE)

        # Fenêtre de fetch :
        # - Si on a une date connue : on recule de FETCH_OVERLAP_DAYS pour capturer
        #   les jours publiés rétroactivement (groupés, irréguliers).
        # - Sinon (premier refresh avant import historique) : on remonte FETCH_MONTHLY_DAYS
        #   pour avoir les cumuls mensuels.
        if last_known_date:
            anchor = datetime.strptime(last_known_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            fetch_start = anchor - timedelta(days=FETCH_OVERLAP_DAYS)
        else:
            fetch_start = now - timedelta(days=FETCH_MONTHLY_DAYS)

        _LOGGER.debug(
            "EGL: fetch %s → %s (last_known=%s)",
            fetch_start.strftime("%Y-%m-%d"),
            now.strftime("%Y-%m-%d"),
            last_known_date,
        )

        try:
            entries = await self._client.fetch_daily_consumption(
                self._contract_token, fetch_start, now
            )
        except EGLAuthError as err:
            raise UpdateFailed(f"Authentification EGL échouée : {err}") from err
        except EGLApiError as err:
            raise UpdateFailed(f"Erreur API EGL : {err}") from err

        if not entries:
            _LOGGER.warning("EGL: aucune donnée reçue, conservation de l'état précédent")
            return self.data or {}

        # --- Push incrémental dans recorder ---
        new_last_date = await async_push_new_entries(
            self.hass,
            entries,
            self._sensor_unique_id,
            last_known_date,
        )

        # Persister la nouvelle dernière date si elle a progressé
        if new_last_date and new_last_date != last_known_date:
            self.hass.config_entries.async_update_entry(
                self._entry,
                data={**self._entry.data, CONF_LAST_KNOWN_DATE: new_last_date},
            )

        # --- Dernier jour avec consommation > 0 (retard de publication variable) ---
        last_published = next(
            (e for e in reversed(entries) if e["liters"] > 0),
            entries[-1],
        )
        lag_days = (
            now.date()
            - datetime.strptime(last_published["date"], "%Y-%m-%d").date()
        ).days
        if lag_days > 0:
            _LOGGER.debug(
                "EGL: dernière donnée publiée = %s (retard %d j)",
                last_published["date"], lag_days,
            )

        # --- Cumuls (on a toujours au moins FETCH_MONTHLY_DAYS de données) ---
        current_month = now.strftime("%Y-%m")
        monthly_total = sum(e["liters"] for e in entries if e["date"].startswith(current_month))

        cutoff_30d = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        rolling_30d = sum(e["liters"] for e in entries if e["date"] >= cutoff_30d)

        return {
            "daily_liters": last_published["liters"],
            "daily_date": last_published["date"],
            "daily_lag_days": lag_days,
            "monthly_liters": monthly_total,
            "rolling_30d_liters": rolling_30d,
            "history": entries,
            "last_update": now.isoformat(),
        }
