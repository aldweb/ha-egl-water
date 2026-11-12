"""Coordinator pour la mise à jour des données EGL.

Scheduling :
  Deux déclencheurs fixes par jour, en heure locale Home Assistant, pas d'intervalle dérivant.

Stratégie de fetch :
  À chaque refresh on interroge systématiquement l'API sur une fenêtre glissante
  fixe de REFRESH_WINDOW_DAYS jours (aujourd'hui - 90 j → aujourd'hui), quelle
  que soit la dernière date déjà connue. Toute la fenêtre est ensuite réécrite
  dans recorder via async_overwrite_recent_entries, y compris les jours à 0
  litres. Ce choix absorbe sans logique particulière le comportement d'EGL :
  jours laissés à 0 puis remplis, puis corrigés rétroactivement.

Cumuls exposés aux capteurs (calculés en heure locale de Home Assistant) :
  - daily_liters / daily_date   : dernier jour disponible (> 0 litres)
  - daily_lag_days              : écart entre ce jour et aujourd'hui
  - monthly_liters              : cumul du mois calendaire en cours
  - rolling_30d_liters          : fenêtre glissante de 30 jours (aujourd'hui inclus)

Erreurs :
  - identifiants refusés  → ConfigEntryAuthFailed (déclenche le flux de réauth)
  - autre erreur d'API    → UpdateFailed (nouvel essai au prochain horaire)
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EGLApiError, EGLAuthError, EGLClient
from .const import (
    CHUNK_DAYS,
    CONF_COST_IMPORTED,
    CONF_HISTORY_IMPORTED,
    CONF_PRICE_PER_M3,
    DEFAULT_PRICE_PER_M3,
    DOMAIN,
    REFRESH_WINDOW_DAYS,
    get_update_times,
    statistic_slug,
)
from .history_import import async_overwrite_recent_entries

_LOGGER = logging.getLogger(__name__)

type EGLConfigEntry = ConfigEntry[EGLDataCoordinator]


class EGLDataCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Récupère et met en cache les données de consommation EGL."""

    config_entry: EGLConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EGLConfigEntry,
        client: EGLClient,
        contract_token: str,
    ) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=None
        )
        self.client = client
        self.contract_token = contract_token
        # Identifiant des statistiques externes, dérivé de l'identifiant EGL.
        self.statistics_slug = statistic_slug(entry.data[CONF_USERNAME])
        self._unsub_timers: list[Callable[[], None]] = []

    @property
    def price_per_m3(self) -> float:
        """Tarif TTC en €/m³ (options de l'entrée)."""
        return self.config_entry.options.get(CONF_PRICE_PER_M3, DEFAULT_PRICE_PER_M3)

    @property
    def statistics_ready(self) -> bool:
        """True quand l'import historique (volume + coût) est terminé.

        Tant qu'il ne l'est pas, le refresh n'écrit PAS dans recorder : l'import
        réécrit tout l'historique avec des cumuls repartant de zéro, et un
        refresh concurrent pourrait ancrer ses propres cumuls sur un état partiel.
        """
        return bool(
            self.config_entry.data.get(CONF_HISTORY_IMPORTED)
            and self.config_entry.options.get(CONF_COST_IMPORTED)
        )

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def async_start_schedule(self) -> None:
        """Planifie les deux refresh quotidiens (heure locale)."""
        for hour, minute in get_update_times(self.config_entry.options):
            unsub = async_track_time_change(
                self.hass, self._async_scheduled_refresh,
                hour=hour, minute=minute, second=0,
            )
            self._unsub_timers.append(unsub)
            _LOGGER.debug("EGL: refresh planifié à %02d:%02d (heure locale)", hour, minute)

    def async_stop_schedule(self) -> None:
        """Annule les refresh planifiés."""
        for unsub in self._unsub_timers:
            unsub()
        self._unsub_timers.clear()

    async def _async_scheduled_refresh(self, now: datetime) -> None:
        _LOGGER.debug("EGL: déclenchement planifié à %s", now.strftime("%H:%M"))
        await self.async_refresh()

    # ------------------------------------------------------------------
    # Récupération des données
    # ------------------------------------------------------------------

    async def _fetch_range_chunked(
        self, start: datetime, end: datetime
    ) -> list[dict]:
        """Récupère [start, end] en tranches de CHUNK_DAYS (appel API fiable sur courte durée)."""
        all_entries: list[dict] = []
        chunk_start = start
        last_error: EGLApiError | None = None
        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), end)
            try:
                entries = await self.client.fetch_daily_consumption(
                    self.contract_token, chunk_start, chunk_end
                )
            except EGLAuthError:
                # Pas de sens à continuer si l'authentification échoue.
                raise
            except EGLApiError as err:
                # Un raté ponctuel ne doit pas faire perdre les autres tranches.
                _LOGGER.warning(
                    "EGL: erreur tranche %s→%s : %s",
                    chunk_start.strftime("%Y-%m-%d"),
                    chunk_end.strftime("%Y-%m-%d"),
                    err,
                )
                last_error = err
            else:
                all_entries.extend(entries)
            chunk_start = chunk_end

        if not all_entries and last_error is not None:
            # Aucune tranche n'a réussi : échec de refresh classique.
            raise last_error

        # Dédoublonnage (les bornes de tranches peuvent se chevaucher d'un jour) + tri
        by_date = {e["date"]: e for e in all_entries}
        return [by_date[d] for d in sorted(by_date)]

    async def _async_update_data(self) -> dict[str, Any]:
        now_utc = dt_util.utcnow()
        # Les cumuls (mois, 30 jours, retard) se calculent en heure LOCALE de
        # Home Assistant : entre 00:00 et 02:00 en France, la date UTC est encore
        # celle de la veille.
        today = dt_util.now().date()

        fetch_start = now_utc - timedelta(days=REFRESH_WINDOW_DAYS)
        _LOGGER.debug(
            "EGL: fetch %s → %s (fenêtre glissante fixe de %d j)",
            fetch_start.strftime("%Y-%m-%d"),
            now_utc.strftime("%Y-%m-%d"),
            REFRESH_WINDOW_DAYS,
        )

        try:
            entries = await self._fetch_range_chunked(fetch_start, now_utc)
        except EGLAuthError as err:
            # Identifiants refusés : Home Assistant propose la ré-authentification.
            raise ConfigEntryAuthFailed(f"Identifiants EGL refusés : {err}") from err
        except EGLApiError as err:
            raise UpdateFailed(f"Erreur API EGL : {err}") from err

        if not entries:
            _LOGGER.warning(
                "EGL: aucune donnée reçue, conservation de l'état précédent. "
                "Si le contrat suivi est cessé, choisissez le contrat actif via "
                "« Reconfigurer » dans Appareils et services."
            )
            return self.data or {}

        # --- Écrasement systématique de la fenêtre dans recorder ---
        if self.statistics_ready:
            await async_overwrite_recent_entries(
                self.hass,
                entries,
                self.statistics_slug,
                price_per_m3=self.price_per_m3,
            )
        else:
            _LOGGER.debug("EGL: import historique en attente, statistiques non écrites")

        # --- Dernier jour avec consommation > 0 (retard de publication variable) ---
        last_published = next(
            (e for e in reversed(entries) if e["liters"] > 0),
            entries[-1],
        )
        lag_days = (today - datetime.strptime(last_published["date"], "%Y-%m-%d").date()).days
        if lag_days > 0:
            _LOGGER.debug(
                "EGL: dernière donnée publiée = %s (retard %d j)",
                last_published["date"], lag_days,
            )

        # --- Cumuls ---
        current_month = today.strftime("%Y-%m")
        monthly_total = sum(e["liters"] for e in entries if e["date"].startswith(current_month))

        # 30 jours glissants, aujourd'hui inclus : de (aujourd'hui - 29 j) à aujourd'hui.
        cutoff_30d = (today - timedelta(days=29)).isoformat()
        rolling_30d = sum(e["liters"] for e in entries if e["date"] >= cutoff_30d)

        return {
            "daily_liters": last_published["liters"],
            "daily_date": last_published["date"],
            "daily_lag_days": lag_days,
            "monthly_liters": monthly_total,
            "rolling_30d_liters": rolling_30d,
            "history": entries,
            "last_update": now_utc.isoformat(),
        }
