"""Import et mise à jour des statistiques de consommation dans recorder HA.

Deux usages :
  1. `async_import_history`  — import initial complet (2 ans), avec purge préalable.
  2. `async_push_new_entries` — push incrémental, appelé à chaque refresh du coordinator.
     Reçoit la liste brute des entrées fetchées et insère tous les jours nouveaux
     avec une consommation > 0 (0 = non encore publié par EGL).

Modèle de données recorder :
  - `state`  = consommation du jour (litres)
  - `sum`    = compteur cumulatif croissant depuis le début de l'historique
               (obligatoire pour que le tableau de bord Énergie calcule les deltas)
  - timestamp = minuit UTC du jour concerné
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    StatisticMeanType,
    async_add_external_statistics,
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant

from .api import EGLApiError, EGLClient
from .const import CHUNK_DAYS, DOMAIN, HISTORY_YEARS

_LOGGER = logging.getLogger(__name__)


def _build_metadata(statistic_id: str) -> StatisticMetaData:
    return StatisticMetaData(
        has_mean=False,
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
        name="Consommation eau",
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement=UnitOfVolume.LITERS,
    )


def _entries_to_stats(entries: list[dict], initial_sum: float = 0.0) -> tuple[list[StatisticData], float]:
    """Convertit une liste triée d'entrées en StatisticData avec sum cumulatif.

    Retourne (stats, cumulative_final) pour éviter d'accéder aux internals de StatisticData.
    """
    stats: list[StatisticData] = []
    cumulative = initial_sum
    for entry in entries:
        liters = entry["liters"]
        cumulative += liters
        dt = datetime.strptime(entry["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        stats.append(StatisticData(start=dt, state=liters, sum=cumulative))
    return stats, cumulative


def _statistic_id(sensor_unique_id: str) -> str:
    """Construit un statistic_id valide pour HA.

    Format imposé : ``domain:object_id``
    Règles HA : chaque partie ne contient que [a-z0-9_] ET commence par [a-z0-9].
    """
    import re
    slug = sensor_unique_id.lower()
    # Remplacer tout caractère non alphanumérique par underscore
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    # Supprimer les underscores en début/fin et les doublons
    slug = re.sub(r"_+", "_", slug).strip("_")
    # S'assurer que le slug commence par [a-z0-9] (pas _)
    if not slug or not slug[0].isalnum():
        slug = f"u{slug}".strip("_")
    return f"{DOMAIN}:{slug}"


# ---------------------------------------------------------------------------
# Purge des statistiques existantes
# ---------------------------------------------------------------------------

async def async_clear_history(hass: HomeAssistant, sensor_unique_id: str) -> None:
    """Supprime toutes les statistiques recorder pour ce capteur."""
    statistic_id = _statistic_id(sensor_unique_id)

    # clear_statistics n'est pas appelable depuis le main thread (elle exige le thread
    # interne du recorder). On utilise à la place async_import_statistics avec une liste
    # vide de données : HA supprime alors toutes les statistiques existantes pour cet ID
    # et repart de zéro, ce qui est exactement l'effet voulu.
    from homeassistant.components.recorder.statistics import async_import_statistics
    metadata = _build_metadata(statistic_id)
    async_import_statistics(hass, metadata, [])
    _LOGGER.info("EGL: statistiques purgées pour %s (via async_import_statistics vide)", statistic_id)


# ---------------------------------------------------------------------------
# Import initial (avec purge préalable)
# ---------------------------------------------------------------------------

async def async_import_history(
    hass: HomeAssistant,
    client: EGLClient,
    contract_token: str,
    sensor_unique_id: str,
) -> tuple[int, str | None]:
    """Purge les stats existantes puis importe 2 ans d'historique dans recorder.

    Retourne (nombre_de_jours_importés, dernière_date_importée | None).
    """
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = now - timedelta(days=HISTORY_YEARS * 365)

    _LOGGER.info(
        "EGL: début import historique — période %s → %s",
        start.strftime("%Y-%m-%d"),
        now.strftime("%Y-%m-%d"),
    )

    # Purge préalable pour repartir d'une ardoise propre
    await async_clear_history(hass, sensor_unique_id)

    # Téléchargement par tranches de CHUNK_DAYS jours
    all_entries: list[dict] = []
    chunk_start = start
    chunk_num = 0
    while chunk_start < now:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), now)
        chunk_num += 1
        try:
            entries = await client.fetch_daily_consumption(contract_token, chunk_start, chunk_end)
            # Ne conserver que les jours avec consommation publiée (> 0)
            published = [e for e in entries if e["liters"] > 0]
            all_entries.extend(published)
            _LOGGER.info(
                "EGL: tranche %d — %s → %s : %d jour(s) reçu(s), %d publié(s)",
                chunk_num,
                chunk_start.strftime("%Y-%m-%d"),
                chunk_end.strftime("%Y-%m-%d"),
                len(entries),
                len(published),
            )
        except EGLApiError as err:
            _LOGGER.warning(
                "EGL: erreur tranche %d (%s → %s) : %s",
                chunk_num,
                chunk_start.strftime("%Y-%m-%d"),
                chunk_end.strftime("%Y-%m-%d"),
                err,
            )
        chunk_start = chunk_end

    if not all_entries:
        _LOGGER.warning("EGL: import historique — aucune donnée récupérée sur 2 ans")
        return 0, None

    # Dédoublonnage + tri chronologique
    seen: set[str] = set()
    unique: list[dict] = []
    for e in all_entries:
        if e["date"] not in seen:
            seen.add(e["date"])
            unique.append(e)
    unique.sort(key=lambda x: x["date"])

    statistic_id = _statistic_id(sensor_unique_id)
    metadata = _build_metadata(statistic_id)
    stats, final_sum = _entries_to_stats(unique)

    async_add_external_statistics(hass, metadata, stats)

    last_date = unique[-1]["date"] if unique else None
    _LOGGER.info(
        "EGL: import historique terminé — %d jour(s) importé(s), premier : %s, dernier : %s, sum final : %.0f L",
        len(stats),
        unique[0]["date"],
        last_date,
        final_sum,
    )
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
    """Pousse dans recorder tous les jours nouveaux avec consommation publiée (> 0).

    - `entries`         : liste triée de {"date": "YYYY-MM-DD", "liters": float}
    - `last_known_date` : dernière date déjà en base (None = première fois)
    - Retourne la nouvelle dernière date importée (ou last_known_date si rien de neuf).

    Un jour à 0 litre signifie "non encore publié par EGL" et est ignoré :
    le pousser créerait une discontinuité de sum quand la vraie valeur arrive ensuite.
    """
    if not entries:
        return last_known_date

    # Filtrer : strictement après la dernière date connue ET consommation publiée
    new_entries = [
        e for e in entries
        if (last_known_date is None or e["date"] > last_known_date)
        and e["liters"] > 0
    ]

    if not new_entries:
        _LOGGER.debug(
            "EGL: refresh — aucun nouveau jour publié (dernier connu : %s)",
            last_known_date,
        )
        return last_known_date

    _LOGGER.info(
        "EGL: refresh — %d nouveau(x) jour(s) publié(s) : %s → %s",
        len(new_entries),
        new_entries[0]["date"],
        new_entries[-1]["date"],
    )

    statistic_id = _statistic_id(sensor_unique_id)

    # Récupérer le sum cumulatif du dernier jour AVANT la première nouvelle entrée.
    # Fenêtre de 40 jours en arrière pour être robuste aux trous de publication EGL.
    # La borne de fin est first_new_dt (exclusive en mode "day") : on n'inclut pas
    # le jour qu'on est en train d'écrire, même s'il existait déjà en base.
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
            "EGL: sum cumulatif de référence = %.0f L (avant le %s)",
            current_sum,
            new_entries[0]["date"],
        )
    else:
        _LOGGER.warning(
            "EGL: aucune statistique antérieure trouvée avant le %s — "
            "le sum repart de 0 (import historique manquant ?)",
            new_entries[0]["date"],
        )

    metadata = _build_metadata(statistic_id)
    stats, final_sum = _entries_to_stats(new_entries, initial_sum=current_sum)
    async_add_external_statistics(hass, metadata, stats)

    new_last_date = new_entries[-1]["date"]
    _LOGGER.info(
        "EGL: push incrémental OK — nouvelle dernière date : %s (sum cumulatif : %.0f L)",
        new_last_date,
        final_sum,
    )
    return new_last_date
