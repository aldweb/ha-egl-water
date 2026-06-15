"""Import et mise à jour des statistiques de consommation dans recorder HA.

Deux usages :
  1. `async_import_history`  — import initial complet (2 ans), appelé une seule fois.
  2. `async_push_new_entries` — push incrémental, appelé à chaque refresh du coordinator.
     Reçoit la liste brute des entrées fetchées et insère TOUS les jours nouveaux ou
     mis à jour, sans hypothèse sur leur nombre ni leur régularité de publication.

Modèle de données recorder :
  - `state`  = consommation du jour (litres)
  - `sum`    = compteur cumulatif croissant depuis le début de l'historique
               (obligatoire pour que le tableau de bord Énergie calcule les deltas)
  - timestamp = minuit UTC du jour concerné
  - L'import est idempotent : recorder écrase les valeurs existantes sur les mêmes
    timestamps → pas de doublons même si on repasse sur des jours déjà importés.
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


def _build_metadata(statistic_id: str) -> StatisticMetaData:
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


def _entries_to_stats(entries: list[dict], initial_sum: float = 0.0) -> list[StatisticData]:
    """Convertit une liste triée d'entrées en StatisticData avec sum cumulatif."""
    stats: list[StatisticData] = []
    cumulative = initial_sum
    for entry in entries:
        liters = entry["liters"]
        cumulative += liters
        dt = datetime.strptime(entry["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        stats.append(StatisticData(start=dt, state=liters, sum=cumulative))
    return stats


# ---------------------------------------------------------------------------
# Import initial (appelé une seule fois au premier démarrage)
# ---------------------------------------------------------------------------

async def async_import_history(
    hass: HomeAssistant,
    client: EGLClient,
    contract_token: str,
    sensor_unique_id: str,
) -> tuple[int, str | None]:
    """Importe 2 ans d'historique dans recorder.

    Retourne (nombre_de_jours_importés, dernière_date_importée | None).
    """
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = now - timedelta(days=HISTORY_YEARS * 365)

    _LOGGER.info(
        "EGL: import initial du %s au %s",
        start.strftime("%Y-%m-%d"),
        now.strftime("%Y-%m-%d"),
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

    statistic_id = f"{DOMAIN}:{sensor_unique_id.lower().replace('-', '_')}"
    metadata = _build_metadata(statistic_id)
    stats = _entries_to_stats(unique)

    async_add_external_statistics(hass, metadata, stats)

    last_date = unique[-1]["date"] if unique else None
    _LOGGER.info("EGL: import initial terminé — %d jours, dernier : %s", len(stats), last_date)
    return len(stats), last_date


# ---------------------------------------------------------------------------
# Push incrémental (appelé à chaque refresh du coordinator)
# ---------------------------------------------------------------------------

async def async_push_new_entries(
    hass: HomeAssistant,
    entries: list[dict],
    sensor_unique_id: str,
    last_known_date: str | None,
) -> str | None:
    """Pousse dans recorder tous les jours plus récents que last_known_date.

    - `entries` : liste triée de {"date": "YYYY-MM-DD", "liters": float}
    - `last_known_date` : dernière date déjà en base (None = première fois)
    - Retourne la nouvelle dernière date importée (ou last_known_date si rien de neuf).

    Gère les publications groupées : si EGL publie vendredi+samedi le mardi,
    tous ces jours sont insérés en un seul appel.
    """
    if not entries:
        return last_known_date

    # Filtrer les jours strictement après la dernière date connue,
    # ET avec une consommation > 0 : un 0 signifie "non encore publié par EGL",
    # pas une vraie consommation nulle. Pousser ces jours créerait des
    # discontinuités de sum quand les vraies valeurs arrivent ensuite.
    new_entries = [
        e for e in entries
        if (last_known_date is None or e["date"] > last_known_date)
        and e["liters"] > 0
    ]

    if not new_entries:
        _LOGGER.debug("EGL: aucun nouveau jour à pousser (dernier connu : %s)", last_known_date)
        return last_known_date

    _LOGGER.info(
        "EGL: %d nouveau(x) jour(s) à importer (%s → %s)",
        len(new_entries),
        new_entries[0]["date"],
        new_entries[-1]["date"],
    )

    statistic_id = f"{DOMAIN}:{sensor_unique_id.lower().replace('-', '_')}"

    # Récupérer le sum cumulatif du dernier jour strictement AVANT la première
    # nouvelle entrée. On remonte jusqu'à 40 jours pour être robuste aux trous
    # de publication EGL (plusieurs jours consécutifs non publiés).
    # La borne de fin est first_new_dt : en mode "day", statistics_during_period
    # retourne les stats dont start >= borne_debut ET start < borne_fin,
    # ce qui exclut bien le jour first_new_dt lui-même.
    instance = get_instance(hass)
    first_new_dt = datetime.strptime(new_entries[0]["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    prior_stats = await instance.async_add_executor_job(
        statistics_during_period,
        hass,
        first_new_dt - timedelta(days=40),
        first_new_dt,
        {statistic_id},
        "day",
        None,
        {"sum"},
    )
    current_sum = 0.0
    if prior_stats and statistic_id in prior_stats and prior_stats[statistic_id]:
        current_sum = prior_stats[statistic_id][-1].get("sum") or 0.0
        _LOGGER.debug(
            "EGL: sum cumulatif de référence = %.0f L (avant %s)",
            current_sum,
            new_entries[0]["date"],
        )

    metadata = _build_metadata(statistic_id)
    stats = _entries_to_stats(new_entries, initial_sum=current_sum)
    async_add_external_statistics(hass, metadata, stats)

    new_last_date = new_entries[-1]["date"]
    _LOGGER.debug("EGL: push incrémental OK, nouvelle dernière date : %s", new_last_date)
    return new_last_date
