"""Constantes pour l'intégration Eau du Grand Lyon."""

DOMAIN = "egl_water"
PLATFORMS = ["sensor"]

# API
BASE_URL = "https://agence.eaudugrandlyon.com/application"
ENTREPRISE_HEADER = "EPGL"
CLIENT_ID = "kwnOk0B_aqlOI6p_GVxrbf6"

# Endpoints
AUTH_URL = f"{BASE_URL}/auth/externe/authentification"
ROLES_URL = f"{BASE_URL}/auth/externe/utilisateur/roles"
AUTHORIZE_URL = f"{BASE_URL}/auth/authorize-internet"
TOKEN_URL = f"{BASE_URL}/auth/tokenUtilisateurInternet"
REDIRECT_URI = "https://agence.eaudugrandlyon.com/autorisation-callback.html"

# Config entry keys
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_CONTRACT_TOKEN = "contract_token"
CONF_HISTORY_IMPORTED = "history_imported"   # flag import initial effectué
CONF_LAST_KNOWN_DATE = "last_known_date"     # dernière date importée (YYYY-MM-DD)

# Options flow keys — heure locale au format "HH:MM"
CONF_UPDATE_TIME_1 = "update_time_1"
CONF_UPDATE_TIME_2 = "update_time_2"

# Valeurs par défaut en heure locale
# 08:00 CEST = 06:00 UTC  /  16:00 CEST = 14:00 UTC
DEFAULT_UPDATE_TIME_1 = "08:00"
DEFAULT_UPDATE_TIME_2 = "16:00"


def get_update_times(hass, options: dict) -> list[tuple[int, int]]:
    """Convertit les horaires locaux (HH:MM) en tuples (heure, minute) UTC.

    Utilise le fuseau horaire configuré dans Home Assistant.
    """
    import re
    from datetime import datetime, timezone

    tz = hass.config.time_zone  # ex: "Europe/Paris"
    try:
        import zoneinfo
        local_tz = zoneinfo.ZoneInfo(tz)
    except Exception:
        # Fallback : on renvoie les heures telles quelles (supposées UTC)
        local_tz = timezone.utc

    result = []
    for key, default in (
        (CONF_UPDATE_TIME_1, DEFAULT_UPDATE_TIME_1),
        (CONF_UPDATE_TIME_2, DEFAULT_UPDATE_TIME_2),
    ):
        raw = options.get(key, default)
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw.strip())
        if not m:
            raw = default
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
        local_h, local_min = int(m.group(1)), int(m.group(2))

        # Construire un datetime local fictif (date fixe) pour extraire l'offset
        ref = datetime(2000, 1, 1, local_h, local_min, tzinfo=local_tz)
        utc_ref = ref.astimezone(timezone.utc)
        result.append((utc_ref.hour, utc_ref.minute))

    return result


# Import historique
HISTORY_YEARS = 2       # profondeur à importer au premier démarrage
CHUNK_DAYS = 90         # taille des tranches d'appel API

# Fenêtre de fetch incrémental : on remonte depuis (last_known_date - FETCH_OVERLAP_DAYS)
# pour absorber les publications groupées (vendredi+samedi publiés le mardi, etc.)
FETCH_OVERLAP_DAYS = 10

# Pour les cumuls mensuels on remonte 35 jours (couvre le mois entier + marge retard)
FETCH_MONTHLY_DAYS = 35

# Tarif TTC tout compris en €/m³
CONF_PRICE_PER_M3 = "price_per_m3"
DEFAULT_PRICE_PER_M3 = 3.56  # à titre indicatif, à ajuster selon facture
CONF_COST_IMPORTED = "cost_imported"         # flag import coût historique effectué
