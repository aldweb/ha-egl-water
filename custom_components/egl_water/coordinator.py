"""Coordinator pour la mise à jour des données EGL.

Scheduling :
  Deux déclencheurs fixes par jour (UPDATE_TIMES_UTC), pas d'intervalle dérivant.

Stratégie de fetch :
  À chaque refresh on interroge systématiquement l'API sur une fenêtre glissante
  fixe de REFRESH_WINDOW_DAYS jours (aujourd'hui - 90 j → aujourd'hui), quelle
  que soit la dernière date déjà connue. Toute la fenêtre est ensuite réécrite
  dans recorder via async_overwrite_recent_entries, y compris les jours à 0
  litres. Ce choix, plus simple qu'un fetch incrémental avec fenêtre d'overlap
  calibrée, absorbe sans logique particulière le comportement d'EGL : jours
  laissés à 0 puis remplis, puis corrigés rétroactivement (y compris repassés
  à 0). Le prix à payer est un appel API légèrement plus gros à chaque refresh,
  ce qui est négligeable comparé au risque de perdre silencieusement des
  corrections tardives.

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
    CHUNK_DAYS,
    CONF_LAST_KNOWN_DATE,
    CONF_PRICE_PER_M3,
    DEFAULT_PRICE_PER_M3,
    DOMAIN,
    REFRESH_WINDOW_DAYS,
    get_update_times,
)
from .history_import import async_overwrite_recent_entries

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
        # Prend la partie locale de l'email (avant @) comme slug, ou le username entier si pas un email
        raw = entry.data["username"].lower()
        local_part = raw.split("@")[0] if "@" in raw else raw
        username_slug = local_part.replace(".", "_").replace("-", "_")
        self._sensor_unique_id = f"{username_slug}_daily"
        self._unsub_timers: list[Any] = []

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def async_start_schedule(self) -> None:
        for hour, minute in get_update_times(self.hass, self._entry.options):
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

    async def _fetch_range_chunked(
        self, start: datetime, end: datetime
    ) -> list[dict]:
        """Récupère [start, end] en tranches de CHUNK_DAYS.

        L'appel unique sur toute la plage n'est fiable que sur de courtes
        durées (l'import historique utilise déjà des tranches de 90 jours
        pour la même raison). La fenêtre de refresh vaut désormais elle-même
        REFRESH_WINDOW_DAYS (90 j), donc ce découpage en tranches reste la
        garantie de fiabilité de l'appel API, même si la fenêtre demandée ne
        varie plus dans le temps.
        """
        all_entries: list[dict] = []
        chunk_start = start
        last_error: EGLApiError | None = None
        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), end)
            try:
                entries = await self._client.fetch_daily_consumption(
                    self._contract_token, chunk_start, chunk_end
                )
            except EGLAuthError:
                # Pas de sens à continuer si l'authentification échoue.
                raise
            except EGLApiError as err:
                # On garde la trace de l'erreur mais on continue les autres
                # tranches : un raté ponctuel ne doit pas faire perdre tout
                # le rattrapage déjà récupéré.
                _LOGGER.warning(
                    "EGL: erreur tranche %s→%s : %s",
                    chunk_start.strftime("%Y-%m-%d"),
                    chunk_end.strftime("%Y-%m-%d"),
                    err,
                )
                last_error = err
                chunk_start = chunk_end
                continue
            all_entries.extend(entries)
            _LOGGER.debug(
                "EGL: tranche %s→%s : %d jours",
                chunk_start.strftime("%Y-%m-%d"),
                chunk_end.strftime("%Y-%m-%d"),
                len(entries),
            )
            chunk_start = chunk_end

        if not all_entries and last_error is not None:
            # Aucune tranche n'a réussi : on remonte l'erreur pour que le
            # coordinator la traite comme un échec de refresh classique.
            raise last_error

        # Dédoublonnage (les bornes de tranches peuvent se chevaucher d'un jour) + tri
        seen: set[str] = set()
        unique: list[dict] = []
        for e in sorted(all_entries, key=lambda x: x["date"]):
            if e["date"] not in seen:
                seen.add(e["date"])
                unique.append(e)
        return unique

    async def _async_update_data(self) -> dict:
        now = datetime.now(timezone.utc)

        # Fenêtre de fetch : systématiquement les REFRESH_WINDOW_DAYS derniers
        # jours, indépendamment de toute date connue précédemment. On accepte
        # de re-télécharger et de réécraser une fenêtre en grande partie déjà
        # connue à chaque refresh : c'est le prix de la simplicité et de la
        # robustesse face aux corrections rétroactives d'EGL.
        fetch_start = now - timedelta(days=REFRESH_WINDOW_DAYS)

        _LOGGER.debug(
            "EGL: fetch %s → %s (fenêtre glissante fixe de %d j)",
            fetch_start.strftime("%Y-%m-%d"),
            now.strftime("%Y-%m-%d"),
            REFRESH_WINDOW_DAYS,
        )

        try:
            entries = await self._fetch_range_chunked(fetch_start, now)
        except EGLAuthError as err:
            raise UpdateFailed(f"Authentification EGL échouée : {err}") from err
        except EGLApiError as err:
            raise UpdateFailed(f"Erreur API EGL : {err}") from err

        if not entries:
            _LOGGER.warning("EGL: aucune donnée reçue, conservation de l'état précédent")
            return self.data or {}

        # --- Écrasement systématique de la fenêtre dans recorder ---
        price_per_m3 = self._entry.options.get(CONF_PRICE_PER_M3, DEFAULT_PRICE_PER_M3)
        new_last_date = await async_overwrite_recent_entries(
            self.hass,
            entries,
            self._sensor_unique_id,
            price_per_m3=price_per_m3,
        )

        # Persister la dernière date vue, à titre purement informatif/diagnostic
        # (n'influence plus le calcul de la fenêtre de fetch, qui est fixe).
        last_known_date: str | None = self._entry.data.get(CONF_LAST_KNOWN_DATE)
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

        # --- Cumuls (on a toujours REFRESH_WINDOW_DAYS jours de données, ≥ 30) ---
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
