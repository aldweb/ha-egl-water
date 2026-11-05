"""Constantes pour l'intégration Eau du Grand Lyon."""
from __future__ import annotations

import re

from homeassistant.const import Platform

DOMAIN = "egl_water"
PLATFORMS = [Platform.SENSOR]

# API
BASE_URL = "https://agence.eaudugrandlyon.com/application"
ENTREPRISE_HEADER = "EPGL"
CLIENT_ID = "kwnOk0B_aqlOI6p_GVxrbf6"
REQUEST_TIMEOUT = 30  # secondes, par requête HTTP

# Endpoints
AUTH_URL = f"{BASE_URL}/auth/externe/authentification"
ROLES_URL = f"{BASE_URL}/auth/externe/utilisateur/roles"
AUTHORIZE_URL = f"{BASE_URL}/auth/authorize-internet"
TOKEN_URL = f"{BASE_URL}/auth/tokenUtilisateurInternet"
REDIRECT_URI = "https://agence.eaudugrandlyon.com/autorisation-callback.html"

# Config entry keys (CONF_USERNAME / CONF_PASSWORD viennent de homeassistant.const)
CONF_CONTRACT_TOKEN = "contract_token"
CONF_HISTORY_IMPORTED = "history_imported"   # flag import initial effectué (data)

# Options flow keys — heure locale au format "HH:MM"
CONF_UPDATE_TIME_1 = "update_time_1"
CONF_UPDATE_TIME_2 = "update_time_2"

# Valeurs par défaut en heure locale (le fuseau est celui de Home Assistant,
# géré par async_track_time_change : aucune conversion à faire ici)
DEFAULT_UPDATE_TIME_1 = "08:00"
DEFAULT_UPDATE_TIME_2 = "16:00"

_RE_HHMM = re.compile(r"(\d{1,2}):(\d{2})")


def get_update_times(options: dict) -> list[tuple[int, int]]:
    """Retourne les horaires configurés en tuples (heure, minute) **locaux**.

    Les valeurs sont destinées à async_track_time_change, qui compare à
    l'heure locale de Home Assistant (heure d'été/hiver incluse).
    Une valeur invalide retombe sur la valeur par défaut.
    """
    result: list[tuple[int, int]] = []
    for key, default in (
        (CONF_UPDATE_TIME_1, DEFAULT_UPDATE_TIME_1),
        (CONF_UPDATE_TIME_2, DEFAULT_UPDATE_TIME_2),
    ):
        raw = options.get(key, default)
        m = _RE_HHMM.fullmatch(str(raw).strip())
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            m = _RE_HHMM.fullmatch(default)
        result.append((int(m.group(1)), int(m.group(2))))
    return result


# Statistiques externes ------------------------------------------------------

def statistic_slug(username: str) -> str:
    """Dérive un identifiant de statistique valide depuis l'identifiant EGL.

    Le recorder n'accepte que [a-z0-9_] (sans "__", ni "_" en début/fin) dans
    la partie après "egl_water:". On prend la partie locale d'un e-mail et on
    remplace tout caractère hors [a-z0-9] par "_" (ex. "jean+eau" -> "jean_eau").
    Pour les identifiants déjà valides, le résultat est identique à l'ancien
    calcul : l'historique existant reste rattaché aux mêmes statistiques.
    """
    local = username.strip().lower().split("@")[0]
    slug = re.sub(r"[^a-z0-9]+", "_", local).strip("_")
    return slug or "account"


def statistic_ids(slug: str) -> tuple[str, str]:
    """Retourne (statistic_id volume, statistic_id coût)."""
    return f"{DOMAIN}:{slug}_daily", f"{DOMAIN}:{slug}_daily_cost"


# Import historique
HISTORY_YEARS = 2       # profondeur à importer au premier démarrage
CHUNK_DAYS = 90         # taille des tranches d'appel API

# Fenêtre de rafraîchissement glissante : à CHAQUE refresh on relit
# systématiquement les REFRESH_WINDOW_DAYS derniers jours et on écrase les
# statistiques recorder correspondantes (voir history_import.async_overwrite_recent_entries).
#
# EGL peut publier un jour à 0 L, puis le remplir plus tard, puis corriger
# rétroactivement des valeurs déjà publiées (y compris repasser un jour à 0).
# Une fenêtre glissante fixe, entièrement réécrite à chaque fois, absorbe tous
# ces cas sans logique de détection de "nouveauté".
REFRESH_WINDOW_DAYS = 90

# Tarif TTC tout compris en €/m³
CONF_PRICE_PER_M3 = "price_per_m3"
DEFAULT_PRICE_PER_M3 = 3.56  # à titre indicatif, à ajuster selon facture
CONF_COST_IMPORTED = "cost_imported"         # flag import coût historique effectué (options)
