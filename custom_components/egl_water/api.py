"""Client API pour Eau du Grand Lyon (agence.eaudugrandlyon.com).

Flux d'authentification OAuth2 PKCE maison :
  1. POST /auth/externe/authentification  → session cookie
  2. GET  /auth/externe/utilisateur/roles  → récupère les rôles
  3. PUT  /auth/externe/utilisateur/roles  → active le rôle "client"
  4. GET  /auth/externe/utilisateur/roles  → confirme
  5. GET  /auth/authorize-internet?...code_challenge... → reçoit un code OAuth2
  6. POST /auth/tokenUtilisateurInternet   → échange le code contre un Bearer JWT
  7. GET  /produits/contrats/{token}/consommationsJournalieres → données

Convention d'erreurs (importante pour la ré-authentification) :
  - EGLAuthError  : UNIQUEMENT quand le serveur rejette les identifiants
                    (HTTP 400/401/403 sur le login ou l'échange de token).
  - EGLApiError   : tout le reste (réseau, timeout, 5xx, réponse inattendue).
Ainsi une panne d'EGL ne déclenche jamais une demande de nouveau mot de passe.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

from .const import (
    AUTH_URL,
    AUTHORIZE_URL,
    BASE_URL,
    CLIENT_ID,
    ENTREPRISE_HEADER,
    REDIRECT_URI,
    REQUEST_TIMEOUT,
    ROLES_URL,
    TOKEN_URL,
)

_LOGGER = logging.getLogger(__name__)

COMMON_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "fr, fr-FR;q=0.9",
    "content-encoding": "gzip, identity",
    "entreprise": ENTREPRISE_HEADER,
}

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
# Statuts HTTP par lesquels le serveur signale des identifiants refusés.
_CREDENTIAL_REJECTED = (400, 401, 403)


def _generate_pkce() -> tuple[str, str]:
    """Génère un code_verifier et son code_challenge S256."""
    verifier_bytes = os.urandom(32)
    code_verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode()
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


class EGLApiError(Exception):
    """Erreur générique de l'API EGL (réseau, timeout, 5xx, réponse invalide)."""


class EGLAuthError(EGLApiError):
    """Identifiants refusés par EGL."""


def _auth_or_api_error(status: int, message: str) -> EGLApiError:
    """EGLAuthError si le statut signifie « identifiants refusés », sinon EGLApiError."""
    if status in _CREDENTIAL_REJECTED:
        return EGLAuthError(f"{message} ({status})")
    return EGLApiError(f"{message} ({status})")


class EGLClient:
    """Client asynchrone pour l'API Eau du Grand Lyon.

    `session` doit disposer d'un cookie jar (le login repose sur un cookie de
    session) ; voir homeassistant.helpers.aiohttp_client.async_create_clientsession.
    """

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._bearer_token: str | None = None
        self._token_expiry: datetime | None = None
        self._contract_token: str | None = None
        self._auth_lock = asyncio.Lock()

    async def close(self) -> None:
        """Ferme la session HTTP."""
        if not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Requêtes
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _request(
        self, method: str, url: str, **kwargs: Any
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        """Requête HTTP avec timeout ; toute erreur réseau devient EGLApiError."""
        headers = {**COMMON_HEADERS, **kwargs.pop("headers", {})}
        try:
            async with self._session.request(
                method, url, headers=headers, timeout=_REQUEST_TIMEOUT, **kwargs
            ) as resp:
                yield resp
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EGLApiError(f"Erreur réseau : {err}") from err

    @staticmethod
    async def _json(resp: aiohttp.ClientResponse) -> Any:
        try:
            return await resp.json(content_type=None)
        except ValueError as err:
            raise EGLApiError("Réponse JSON invalide") from err

    # ------------------------------------------------------------------
    # Authentification complète
    # ------------------------------------------------------------------

    async def authenticate(self) -> str:
        """Effectue le flux complet et retourne le Bearer token."""
        async with self._auth_lock:
            return await self._authenticate()

    async def _authenticate(self) -> str:
        # Étape 1 : login → cookie de session
        _LOGGER.debug("EGL auth step 1: login")
        login_data = urlencode({
            "username": self._username,
            "password": self._password,
            "client_id": CLIENT_ID,
        })
        async with self._request(
            "POST",
            AUTH_URL,
            data=login_data,
            headers={"content-type": "application/x-www-form-urlencoded"},
        ) as resp:
            if resp.status not in (200, 204):
                # Ne jamais journaliser le corps de la réponse (peut contenir des données de compte).
                raise _auth_or_api_error(resp.status, "Login échoué")

        # Étape 2 : GET roles
        _LOGGER.debug("EGL auth step 2: get roles")
        async with self._request("GET", ROLES_URL) as resp:
            if resp.status != 200:
                raise _auth_or_api_error(resp.status, "GET roles échoué")

        # Étape 3 : PUT roles → activer "client"
        _LOGGER.debug("EGL auth step 3: set role client")
        async with self._request(
            "PUT",
            ROLES_URL,
            json=[{"id": "client", "libelle": "client", "actif": False}],
            headers={"content-type": "application/json"},
        ) as resp:
            if resp.status not in (200, 204):
                raise _auth_or_api_error(resp.status, "PUT roles échoué")

        # Étape 4 : GET roles (confirmation) — le contenu n'est pas nécessaire
        _LOGGER.debug("EGL auth step 4: confirm roles")
        async with self._request("GET", ROLES_URL):
            pass

        # Étape 5 : authorize → code OAuth2
        _LOGGER.debug("EGL auth step 5: authorize")
        code_verifier, code_challenge = _generate_pkce()
        auth_params = {
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "client_id": CLIENT_ID,
        }
        oauth_code: str | None = None
        last_status = 0

        # On suit manuellement la redirection pour extraire le code
        async with self._request(
            "GET", AUTHORIZE_URL, params=auth_params, allow_redirects=False
        ) as resp:
            last_status = resp.status
            # Le serveur peut répondre 302 vers redirect_uri?code=...
            # ou 200 avec le code dans le body / header Location
            location = resp.headers.get("Location", "")
            if location:
                oauth_code = parse_qs(urlparse(location).query).get("code", [None])[0]
            if not oauth_code and resp.status == 200:
                body = await resp.text()
                m = re.search(r'"code"\s*:\s*"([^"]+)"', body) or re.search(
                    r"[?&]code=([a-f0-9]+)", body
                )
                if m:
                    oauth_code = m.group(1)

        if not oauth_code:
            # Certains serveurs retournent directement 200 avec le code
            # en suivant la redirection (callback.html?code=…)
            async with self._request(
                "GET", AUTHORIZE_URL, params=auth_params, allow_redirects=True
            ) as resp:
                last_status = resp.status
                oauth_code = parse_qs(urlparse(str(resp.url)).query).get("code", [None])[0]
                if not oauth_code:
                    m = re.search(r"[?&]code=([a-f0-9]+)", await resp.text())
                    if m:
                        oauth_code = m.group(1)

        if not oauth_code:
            if last_status >= 500:
                raise EGLApiError(f"Serveur d'autorisation indisponible ({last_status})")
            raise EGLAuthError("Impossible d'obtenir le code OAuth2")

        # Étape 6 : échange code → Bearer token
        _LOGGER.debug("EGL auth step 6: exchange code for token")
        token_data = urlencode({
            "client_id": CLIENT_ID,
            "code": oauth_code,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        })
        async with self._request(
            "POST",
            TOKEN_URL,
            data=token_data,
            headers={"content-type": "application/x-www-form-urlencoded"},
        ) as resp:
            if resp.status != 200:
                raise _auth_or_api_error(resp.status, "Échange de token échoué")
            payload = await self._json(resp)

        token = None
        expires_in = 3600
        if isinstance(payload, dict):
            token = payload.get("access_token") or payload.get("token")
            try:
                expires_in = int(payload.get("expires_in", 3600))
            except (TypeError, ValueError):
                pass
        if not token:
            # Ne pas inclure `payload` dans le message : il peut contenir des secrets.
            raise EGLApiError("Pas de token dans la réponse d'EGL")

        self._bearer_token = token
        self._token_expiry = datetime.now(UTC) + timedelta(seconds=max(expires_in - 60, 0))
        _LOGGER.debug("EGL: token obtenu, expire dans %ds", expires_in)
        return token

    async def _get_valid_token(self) -> str:
        """Retourne un token valide, ré-authentifie si nécessaire."""
        async with self._auth_lock:
            if (
                self._bearer_token is None
                or self._token_expiry is None
                or datetime.now(UTC) >= self._token_expiry
            ):
                await self._authenticate()
            return self._bearer_token  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Récupération des données
    # ------------------------------------------------------------------

    async def get_contract_token(self) -> str:
        """Récupère et met en cache le token de contrat depuis la liste des contrats."""
        if self._contract_token:
            return self._contract_token

        token = await self._get_valid_token()
        url = f"{BASE_URL}/rest/produits/contrats"
        async with self._request(
            "GET", url, headers={"authorization": f"Bearer {token}"}
        ) as resp:
            if resp.status != 200:
                raise EGLApiError(f"Impossible de récupérer les contrats ({resp.status})")
            data = await self._json(resp)

        # La réponse est une liste de contrats ; on prend le premier
        contracts = data if isinstance(data, list) else (data or {}).get("contrats", [])
        if not contracts:
            raise EGLApiError("Aucun contrat trouvé pour ce compte")

        # Le token de contrat est dans le champ "id", "token" ou "refContrat"
        first = contracts[0]
        contract_token = first.get("id") or first.get("token") or first.get("refContrat")
        if not contract_token:
            raise EGLApiError("Identifiant de contrat introuvable dans la réponse")

        self._contract_token = str(contract_token)
        return self._contract_token

    async def fetch_daily_consumption(
        self,
        contract_token: str,
        start: datetime,
        end: datetime,
    ) -> list[dict]:
        """Retourne les consommations journalières entre start et end.

        Chaque entrée : {"date": "YYYY-MM-DD", "liters": float}
        """
        date_fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        url = (
            f"{BASE_URL}/rest/produits/contrats/{contract_token}"
            f"/consommationsJournalieres"
        )
        params = {
            "dateDebut": start.strftime(date_fmt),
            "dateFin": end.strftime(date_fmt),
        }

        for attempt in range(2):
            token = await self._get_valid_token()
            async with self._request(
                "GET", url, params=params, headers={"authorization": f"Bearer {token}"}
            ) as resp:
                if resp.status == 401 and attempt == 0:
                    # Token expiré/révoqué côté serveur : on ré-authentifie une fois.
                    _LOGGER.debug("EGL: token refusé, ré-authentification")
                    self._bearer_token = None
                    continue
                if resp.status != 200:
                    # Une fois le login réussi, un échec ici n'est pas un problème
                    # d'identifiants : EGLApiError (pas de demande de réauth).
                    raise EGLApiError(f"Lecture des consommations échouée ({resp.status})")
                data = await self._json(resp)
            return _parse_consumption(data)

        raise EGLApiError("Lecture des consommations échouée")  # pragma: no cover


def _parse_consumption(data: Any) -> list[dict]:
    """Transforme la réponse API en liste normalisée.

    Format observé dans le konnector EGL :
      { "postes": [ { "data": [ { "consommation": N, "annee": Y, "mois": M (0-based), "jour": D } ] } ],
        "unites": { "consommation": "m3" | "l" } }
    Les lignes malformées sont ignorées plutôt que de faire échouer tout le refresh.
    """
    if isinstance(data, list):
        raw_list = data
        unit = "l"
    elif isinstance(data, dict):
        postes = data.get("postes") or []
        if not postes:
            return []
        raw_list = postes[0].get("data") or []
        unit = str((data.get("unites") or {}).get("consommation", "l")).lower()
    else:
        return []

    results = []
    for entry in raw_list:
        try:
            conso = float(entry.get("consommation", 0) or 0)
            liters = conso * 1000 if unit == "m3" else conso
            mois = int(entry.get("mois", 0))
            # Mois 0-based dans l'API (comportement du konnector EGL : mois + 1).
            month = mois + 1 if mois < 12 else mois
            day = date(int(entry.get("annee", 0)), month, int(entry.get("jour", 1)))
        except (AttributeError, TypeError, ValueError):
            _LOGGER.debug("EGL: ligne de consommation ignorée (format inattendu)")
            continue
        results.append({"date": day.isoformat(), "liters": liters})

    return sorted(results, key=lambda x: x["date"])
