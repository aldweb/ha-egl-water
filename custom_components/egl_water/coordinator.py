"""Coordinator pour la mise à jour des données EGL.

Scheduling :
  Deux déclencheurs fixes par jour configurables via l'options flow.
  Par défaut : 06:00 UTC et 14:00 UTC (08:00 et 16:00 CEST).

Stratégie de fetch incrémental :
  À chaque refresh on interroge l'API depuis (last_known_date - FETCH_OVERLAP_DAYS)
  jusqu'à aujourd'hui. L'overlap de 10 jours absorbe les publications groupées
  irrégulières d'EGL (ex: vendredi+samedi publiés le mardi suivant).
  Seuls les jours avec consommation > 0 sont poussés dans recorder.

Cumuls exposés aux capteurs :
  - daily_liters / daily_date   : dernier jour publié (> 0 litres)
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
    CONF_UPDATE_HOUR_1,
    CONF_UPDATE_HOUR_2,
    DEFAULT_UPDATE_HOUR_1,
    DEFAULT_UPDATE_HOUR_2,
    DOMAIN,
    FETCH_MONTHLY_DAYS,
    FETCH_OVERLAP_DAYS,
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
        # Slug dérivé du nom d'utilisateur — ne doit contenir que [a-z0-9_]
        # pour satisfaire la validation HA du statistic_id.
        import re
        raw = entry.data["username"].lower()
        local_part = raw.split("@")[0] if "@" in raw else raw
        slug = re.sub(r"[^a-z0-9]+", "_", local_part)
        slug = re.sub(r"_+", "_", slug).strip("_")
        # Fallback si le slug est vide (caractères non-ASCII uniquement)
        if not slug:
            slug = re.sub(r"[^a-z0-9]+", "_", entry.entry_id.lower()).strip("_")
        self._sensor_unique_id = f"{slug}_daily"
        self._unsub_timers: list[Any] = []

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def _update_hours(self) -> list[tuple[int, int]]:
        """Retourne les heures UTC de refresh depuis les options (ou les défauts)."""
        opts = self._entry.options
        h1 = int(opts.get(CONF_UPDATE_HOUR_1, DEFAULT_UPDATE_HOUR_1))
        h2 = int(opts.get(CONF_UPDATE_HOUR_2, DEFAULT_UPDATE_HOUR_2))
        return [(h1, 0), (h2, 0)]

    def async_start_schedule(self) -> None:
        hours = self._update_hours()
        for hour, minute in hours:
            unsub = async_track_time_change(
                self.hass, self._async_scheduled_refresh,
                hour=hour, minute=minute, second=0,
            )
            self._unsub_timers.append(unsub)
            _LOGGER.info(
                "EGL: refresh planifié à %02d:%02d UTC (%02d:%02d CEST)",
                hour, minute, (hour + 2) % 24, minute,
            )

    def async_stop_schedule(self) -> None:
        for unsub in self._unsub_timers:
            unsub()
        self._unsub_timers.clear()

    def async_restart_schedule(self) -> None:
        """Replanifie les refreshs (après changement d'options)."""
        self.async_stop_schedule()
        self.async_start_schedule()

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
        # - Si on a une date connue : recul de FETCH_OVERLAP_DAYS pour capturer
        #   les jours publiés rétroactivement.
        # - Sinon : FETCH_MONTHLY_DAYS pour avoir les cumuls mensuels d'emblée.
        if last_known_date:
            anchor = datetime.strptime(last_known_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            fetch_start = anchor - timedelta(days=FETCH_OVERLAP_DAYS)
        else:
            fetch_start = now - timedelta(days=FETCH_MONTHLY_DAYS)

        _LOGGER.debug(
            "EGL: fetch %s → %s (dernier connu : %s)",
            fetch_start.strftime("%Y-%m-%d"),
            now.strftime("%Y-%m-%d"),
            last_known_date or "aucun",
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
            _LOGGER.warning("EGL: aucune donnée reçue de l'API, conservation de l'état précédent")
            return self.data or {}

        published = [e for e in entries if e["liters"] > 0]
        _LOGGER.debug(
            "EGL: %d entrée(s) reçue(s) de l'API, %d avec consommation publiée",
            len(entries),
            len(published),
        )

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

        # --- Dernier jour avec consommation publiée ---
        last_published = next(
            (e for e in reversed(entries) if e["liters"] > 0),
            entries[-1],
        )
        lag_days = (
            now.date()
            - datetime.strptime(last_published["date"], "%Y-%m-%d").date()
        ).days
        if lag_days > 0:
            _LOGGER.info(
                "EGL: dernière valeur publiée = %s (%d litre(s)), retard %d jour(s)",
                last_published["date"],
                int(last_published["liters"]),
                lag_days,
            )

        # --- Cumuls ---
        current_month = now.strftime("%Y-%m")
        monthly_total = sum(e["liters"] for e in entries if e["date"].startswith(current_month))

        cutoff_30d = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        rolling_30d = sum(e["liters"] for e in entries if e["date"] >= cutoff_30d)

        _LOGGER.debug(
            "EGL: cumul mois en cours = %.0f L, fenêtre 30 j = %.0f L",
            monthly_total,
            rolling_30d,
        )

        return {
            "daily_liters": last_published["liters"],
            "daily_date": last_published["date"],
            "daily_lag_days": lag_days,
            "monthly_liters": monthly_total,
            "rolling_30d_liters": rolling_30d,
            "history": entries,
            "last_update": now.isoformat(),
        }
