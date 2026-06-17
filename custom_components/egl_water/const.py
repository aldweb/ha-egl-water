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

# Scheduling — deux passages fixes par jour (heures UTC)
# 06:00 UTC = 08:00 CEST  /  14:00 UTC = 16:00 CEST
UPDATE_TIMES_UTC = [(6, 0), (14, 0)]

# Import historique
HISTORY_YEARS = 2       # profondeur à importer au premier démarrage
CHUNK_DAYS = 90         # taille des tranches d'appel API

# Fenêtre de fetch incrémental : on remonte depuis (last_known_date - FETCH_OVERLAP_DAYS)
# pour absorber les publications groupées (vendredi+samedi publiés le mardi, etc.)
FETCH_OVERLAP_DAYS = 10

# Pour les cumuls mensuels on remonte 35 jours (couvre le mois entier + marge retard)
FETCH_MONTHLY_DAYS = 35
