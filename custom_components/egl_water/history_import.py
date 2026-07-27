"""Import et mise à jour des statistiques de consommation dans recorder HA.

Deux usages :
  1. `async_import_history`  — import initial complet (2 ans), appelé une seule fois.
  2. `async_overwrite_recent_entries` — écrase la fenêtre glissante (REFRESH_WINDOW_DAYS
     jours), appelé à chaque refresh du coordinator. Contrairement à un push
     incrémental, TOUS les jours de la fenêtre sont réécrits (y compris ceux à 0 L) :
     EGL peut publier un jour à 0 L, puis le remplir, puis corriger rétroactivement
     des valeurs déjà publiées. Recorder écrase les points existants aux mêmes
     timestamps, donc rejouer systématiquement toute la fenêtre absorbe ces
     corrections sans avoir besoin de détecter ce qui a changé.

Statistiques recorder créées par entrée :
  - `<domain>:<slug>_daily`       volume en litres (sum cumulatif)
  - `<domain>:<slug>_daily_cost`  coût en € TTC (sum cumulatif)

Le modèle _cost utilise le même timestamp que le volume, et le même sum cumulatif
croissant — ce qui permet à la page Énergie d'afficher le coût associé au volume.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    StatisticMeanType,
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import VolumeConverter

from .api import EGLApiError, EGLClient
from .const import CHUNK_DAYS, DOMAIN, HISTORY_YEARS

_LOGGER = logging.getLogger(__name__)

# Date plancher utilisée pour retrouver le cumul précédent lors d'un push
# incrémental, quel que soit l'ancienneté du dernier point connu.
SENTINEL_START = datetime(2000, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _build_volume_metadata(statistic_id: str) -> StatisticMetaData:
    return StatisticMetaData(
        has_mean=False,
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
        name="Consommation journalière eau",
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_class=VolumeConverter.UNIT_CLASS,
        unit_of_measurement=UnitOfVolume.LITERS,
    )


def _build_cost_metadata(statistic_id: str) -> StatisticMetaData:
    return StatisticMetaData(
        has_mean=False,
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
        name="Coût journalier eau",
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_class=None,
        unit_of_measurement="EUR",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entries_to_stats(
    entries: list[dict],
    initial_volume_sum: float = 0.0,
    price_per_m3: float | None = None,
    initial_cost_sum: float = 0.0,
) -> tuple[list[StatisticData], list[StatisticData]]:
    """Convertit des entrées triées en (stats_volume, stats_cost).

    Si price_per_m3 est None, stats_cost sera une liste vide.
    """
    vol_stats: list[StatisticData] = []
    cost_stats: list[StatisticData] = []
    cum_vol = initial_volume_sum
    cum_cost = initial_cost_sum

    for entry in entries:
        liters = entry["liters"]
        cum_vol += liters
        dt = datetime.strptime(entry["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        vol_stats.append(StatisticData(start=dt, state=liters, sum=cum_vol))

        if price_per_m3 is not None:
            cost = round(liters / 1000 * price_per_m3, 4)
            cum_cost += cost
            cost_stats.append(StatisticData(start=dt, state=cost, sum=cum_cost))

    return vol_stats, cost_stats


def _statistic_ids(sensor_unique_id: str) -> tuple[str, str]:
    """Retourne (volume_id, cost_id)."""
    slug = sensor_unique_id.lower().replace("-", "_")
    # sensor_unique_id vaut déjà "<slug>_daily", on remplace le suffixe
    base = slug.removesuffix("_daily") if slug.endswith("_daily") else slug
    return f"{DOMAIN}:{base}_daily", f"{DOMAIN}:{base}_daily_cost"


# ---------------------------------------------------------------------------
# Import initial
# ---------------------------------------------------------------------------

async def async_import_history(
    hass: HomeAssistant,
    client: EGLClient,
    contract_token: str,
    sensor_unique_id: str,
    price_per_m3: float | None = None,
) -> tuple[int, str | None]:
    """Importe 2 ans d'historique volume (et coût si price_per_m3 fourni).

    Retourne (nombre_de_jours_importés, dernière_date | None).
    """
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = now - timedelta(days=HISTORY_YEARS * 365)

    _LOGGER.info(
        "EGL: import initial du %s au %s (coût : %s)",
        start.strftime("%Y-%m-%d"),
        now.strftime("%Y-%m-%d"),
        f"{price_per_m3} €/m³" if price_per_m3 else "non",
    )

    all_entries: list[dict] = []
    chunk_start = start
    while chunk_start < now:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), now)
        try:
            entries = await client.fetch_daily_consumption(contract_token, chunk_start, chunk_end)
            all_entries.extend(entries)
            _LOGGER.debug(
                "EGL: tranche %s→%s : %d jours",
                chunk_start.strftime("%Y-%m-%d"),
                chunk_end.strftime("%Y-%m-%d"),
                len(entries),
            )
        except EGLApiError as err:
            _LOGGER.warning("EGL: erreur tranche %s : %s", chunk_start.strftime("%Y-%m-%d"), err)
        chunk_start = chunk_end

    if not all_entries:
        _LOGGER.warning("EGL: aucune donnée historique récupérée")
        return 0, None

    # Dédoublonnage + tri
    seen: set[str] = set()
    unique: list[dict] = []
    for e in all_entries:
        if e["date"] not in seen:
            seen.add(e["date"])
            unique.append(e)
    unique.sort(key=lambda x: x["date"])

    vol_id, cost_id = _statistic_ids(sensor_unique_id)
    vol_stats, cost_stats = _entries_to_stats(unique, price_per_m3=price_per_m3)

    async_add_external_statistics(hass, _build_volume_metadata(vol_id), vol_stats)
    if cost_stats:
        async_add_external_statistics(hass, _build_cost_metadata(cost_id), cost_stats)

    last_date = unique[-1]["date"] if unique else None
    _LOGGER.info("EGL: import initial terminé — %d jours, dernier : %s", len(vol_stats), last_date)
    return len(vol_stats), last_date


# ---------------------------------------------------------------------------
# Import coût rétroactif (appelé si le tarif vient d'être configuré)
# ---------------------------------------------------------------------------

async def async_import_cost_history(
    hass: HomeAssistant,
    client: EGLClient,
    contract_token: str,
    sensor_unique_id: str,
    price_per_m3: float,
) -> tuple[int, str | None]:
    """Réécrit toutes les statistiques de coût sur 2 ans.

    Idempotent : recorder écrase les valeurs existantes sur les mêmes timestamps.
    """
    _LOGGER.info("EGL: import coût rétroactif à %.4f €/m³", price_per_m3)
    return await async_import_history(
        hass, client, contract_token, sensor_unique_id, price_per_m3=price_per_m3
    )


# ---------------------------------------------------------------------------
# Écrasement de la fenêtre glissante
# ---------------------------------------------------------------------------

async def async_overwrite_recent_entries(
    hass: HomeAssistant,
    entries: list[dict],
    sensor_unique_id: str,
    price_per_m3: float | None = None,
) -> str | None:
    """Écrase dans recorder TOUTE la fenêtre d'entrées fournie par le coordinator.

    Gère volume et coût en un seul appel. Ne filtre ni sur une date connue, ni
    sur les jours à 0 litres : ces derniers sont volontairement écrits (comme
    valeur 0) pour que recorder ait un point à chaque date de la fenêtre, et
    pour qu'une correction ultérieure d'EGL sur ce même jour soit reprise au
    prochain refresh, puisque toute la fenêtre est rejouée systématiquement.

    `async_add_external_statistics` écrase les points existants aux mêmes
    timestamps (comportement idempotent) : rejouer une fenêtre déjà connue
    ne duplique rien, elle est simplement recalculée à l'identique ou corrigée.

    Retourne la dernière date de la fenêtre (ou None si `entries` est vide).
    """
    if not entries:
        return None

    sorted_entries = sorted(entries, key=lambda x: x["date"])

    _LOGGER.info(
        "EGL: écrasement de la fenêtre glissante (%s → %s, %d jours)",
        sorted_entries[0]["date"],
        sorted_entries[-1]["date"],
        len(sorted_entries),
    )

    vol_id, cost_id = _statistic_ids(sensor_unique_id)
    instance = get_instance(hass)
    window_start_dt = datetime.strptime(sorted_entries[0]["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)

    async def _get_prior_sum(stat_id: str) -> float:
        # Cumul juste avant le début de la fenêtre, pour ancrer le sum
        # cumulatif recalculé sur la fenêtre. On ne borne pas le début de la
        # recherche : SENTINEL_START est antérieure à tout import historique
        # possible (HISTORY_YEARS ans max), donc couvre toujours le cas réel,
        # y compris si des refresh ont été manqués longtemps.
        prior = await instance.async_add_executor_job(
            statistics_during_period,
            hass,
            SENTINEL_START,
            window_start_dt,
            {stat_id},
            "day",
            None,
            {"sum"},
        )
        if prior and stat_id in prior and prior[stat_id]:
            return prior[stat_id][-1].get("sum") or 0.0
        return 0.0

    prior_vol_sum = await _get_prior_sum(vol_id)
    prior_cost_sum = await _get_prior_sum(cost_id) if price_per_m3 is not None else 0.0

    vol_stats, cost_stats = _entries_to_stats(
        sorted_entries,
        initial_volume_sum=prior_vol_sum,
        price_per_m3=price_per_m3,
        initial_cost_sum=prior_cost_sum,
    )

    async_add_external_statistics(hass, _build_volume_metadata(vol_id), vol_stats)
    if cost_stats:
        async_add_external_statistics(hass, _build_cost_metadata(cost_id), cost_stats)

    new_last_date = sorted_entries[-1]["date"]
    _LOGGER.debug("EGL: fenêtre écrasée avec succès, dernière date : %s", new_last_date)
    return new_last_date
