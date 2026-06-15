"""Client API pour Eau du Grand Lyon (agence.eaudugrandlyon.com).

Flux d'authentification OAuth2 PKCE maison :
  1. POST /auth/externe/authentification  → session cookie
  2. GET  /auth/externe/utilisateur/roles  → récupère les rôles
  3. PUT  /auth/externe/utilisateur/roles  → active le rôle "client"
  4. GET  /auth/externe/utilisateur/roles  → confirme
  5. GET  /auth/authorize-internet?...code_challenge... → reçoit un code OAuth2
  6. POST /auth/tokenUtilisateurInternet   → échange le code contre un Bearer JWT
  7. GET  /produits/contrats/{token}/consommationsJournalieres → données

Note sur la gestion de session :
  Chaque authentification crée une SESSION FRAÎCHE (nouveau CookieJar) puis la
  ferme dès que le Bearer token est obtenu. On ne conserve entre les refreshs
  QUE le Bearer token ; les cookies de session EGL ne sont jamais réutilisés.
  Ceci évite le rejet "invalid_grant" (code OAuth2 déjà consommé) et le
  "nombre maximum de connexion atteint" (sessions zombies côté serveur).
  La session de données (appels fetch) est également recréée à chaque appel
  pour éviter les "Unclosed client session" de runner.py.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import base64
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse, parse_qs

import aiohttp

from .const import (
    AUTH_URL,
    AUTHORIZE_URL,
    CLIENT_ID,
    ENTREPRISE_HEADER,
    REDIRECT_URI,
    ROLES_URL,
    TOKEN_URL,
    BASE_URL,
)

_LOGGER = logging.getLogger(__name__)

COMMON_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "fr, fr-FR;q=0.9",
    "content-encoding": "gzip, identity",
    "entreprise": ENTREPRISE_HEADER,
}


def _generate_pkce() -> tuple[str, str]:
    """Génère un code_verifier et son code_challenge S256."""
    verifier_bytes = os.urandom(32)
    code_verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode()
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


class EGLApiError(Exception):
    """Erreur générique de l'API EGL."""


class EGLAuthError(EGLApiError):
    """Erreur d'authentification."""


class EGLClient:
    """Client asynchrone pour l'API Eau du Grand Lyon."""

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        self._bearer_token: str | None = None
        self._token_expiry: datetime | None = None
        self._contract_token: str | None = None

    # ------------------------------------------------------------------
    # Session helpers — une session éphémère par opération
    # ------------------------------------------------------------------

    def _new_session(self) -> aiohttp.ClientSession:
        """Crée une session aiohttp avec CookieJar frais."""
        return aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(),
            headers=COMMON_HEADERS,
        )

    async def close(self) -> None:
        """Pas de session persistante à fermer — méthode conservée pour compatibilité."""

    # ------------------------------------------------------------------
    # Authentification complète
    # ------------------------------------------------------------------

    async def authenticate(self) -> str:
        """Effectue le flux complet et retourne le Bearer token.

        La session HTTP est créée, utilisée, puis fermée dans cette méthode.
        Aucun cookie ne persiste après l'obtention du token.
        """
        session = self._new_session()
        try:
            token, expires_in = await self._do_auth_flow(session)
        finally:
            # On ferme TOUJOURS la session, même en cas d'exception,
            # pour ne laisser aucune connexion ouverte côté serveur EGL.
            await session.close()

        self._bearer_token = token
        self._token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)
        _LOGGER.info("EGL: authentification OK, token valide %ds", expires_in)
        return token

    async def _do_auth_flow(self, session: aiohttp.ClientSession) -> tuple[str, int]:
        """Effectue les 6 étapes OAuth2 dans la session fournie.

        Retourne (access_token, expires_in).
        """
        # Étape 1 : login → cookie de session
        _LOGGER.debug("EGL auth step 1: login")
        login_data = urlencode({
            "username": self._username,
            "password": self._password,
            "client_id": CLIENT_ID,
        })
        async with session.post(
            AUTH_URL,
            data=login_data,
            headers={"content-type": "application/x-www-form-urlencoded"},
        ) as resp:
            if resp.status not in (200, 204):
                text = await resp.text()
                _LOGGER.error("Login failed %s: %s", resp.status, text)
                raise EGLAuthError(f"Login échoué ({resp.status})")

        # Étape 2 : GET roles
        _LOGGER.debug("EGL auth step 2: get roles")
        async with session.get(ROLES_URL) as resp:
            if resp.status != 200:
                raise EGLAuthError(f"GET roles échoué ({resp.status})")

        # Étape 3 : PUT roles → activer "client"
        _LOGGER.debug("EGL auth step 3: set role client")
        async with session.put(
            ROLES_URL,
            json=[{"id": "client", "libelle": "client", "actif": False}],
            headers={"content-type": "application/json"},
        ) as resp:
            if resp.status not in (200, 204):
                raise EGLAuthError(f"PUT roles échoué ({resp.status})")

        # Étape 4 : GET roles (confirmation)
        _LOGGER.debug("EGL auth step 4: confirm roles")
        async with session.get(ROLES_URL) as resp:
            pass  # contenu non utilisé

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

        oauth_code = None

        # Tentative 1 : sans suivi de redirection (302 → Location: callback?code=...)
        async with session.get(
            AUTHORIZE_URL,
            params=auth_params,
            allow_redirects=False,
        ) as resp:
            location = resp.headers.get("Location", "")
            if location:
                parsed = urlparse(location)
                oauth_code = parse_qs(parsed.query).get("code", [None])[0]
            if not oauth_code and resp.status == 200:
                body = await resp.text()
                m = re.search(r'"code"\s*:\s*"([^"]+)"', body)
                if m:
                    oauth_code = m.group(1)
                else:
                    m = re.search(r'[?&]code=([a-f0-9]+)', body)
                    if m:
                        oauth_code = m.group(1)

        # Tentative 2 : avec suivi de redirection
        if not oauth_code:
            async with session.get(
                AUTHORIZE_URL,
                params=auth_params,
                allow_redirects=True,
            ) as resp:
                final_url = str(resp.url)
                parsed = urlparse(final_url)
                oauth_code = parse_qs(parsed.query).get("code", [None])[0]
                if not oauth_code:
                    body = await resp.text()
                    m = re.search(r'[?&]code=([a-f0-9]+)', body)
                    if m:
                        oauth_code = m.group(1)

        if not oauth_code:
            raise EGLAuthError("Impossible d'obtenir le code OAuth2")

        _LOGGER.debug("EGL auth step 5: code obtenu (%s…)", oauth_code[:8])

        # Étape 6 : échange code → Bearer token
        _LOGGER.debug("EGL auth step 6: exchange code for token")
        token_data = urlencode({
            "client_id": CLIENT_ID,
            "code": oauth_code,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        })
        async with session.post(
            TOKEN_URL,
            data=token_data,
            headers={"content-type": "application/x-www-form-urlencoded"},
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                _LOGGER.error("Token exchange failed %s: %s", resp.status, text)
                raise EGLAuthError(f"Échange de token échoué ({resp.status})")
            payload = await resp.json()

        token = payload.get("access_token") or payload.get("token")
        if not token:
            raise EGLAuthError(f"Pas de token dans la réponse : {payload}")

        expires_in = payload.get("expires_in", 3600)
        return token, expires_in

    async def _get_valid_token(self) -> str:
        """Retourne un token valide, ré-authentifie si nécessaire."""
        if (
            self._bearer_token is None
            or self._token_expiry is None
            or datetime.now(timezone.utc) >= self._token_expiry
        ):
            await self.authenticate()
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

        session = self._new_session()
        try:
            async with session.get(
                url,
                headers={**COMMON_HEADERS, "authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status != 200:
                    raise EGLApiError(f"Impossible de récupérer les contrats ({resp.status})")
                data = await resp.json()
        finally:
            await session.close()

        contracts = data if isinstance(data, list) else data.get("contrats", [])
        if not contracts:
            raise EGLApiError("Aucun contrat trouvé pour ce compte")

        first = contracts[0]
        contract_token = (
            first.get("id")
            or first.get("token")
            or first.get("refContrat")
        )
        if not contract_token:
            raise EGLApiError(f"Champ 'id' introuvable dans le contrat : {first}")

        _LOGGER.debug("EGL: contract token = %s…", str(contract_token)[:12])
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
        token = await self._get_valid_token()

        date_fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        url = (
            f"{BASE_URL}/rest/produits/contrats/{contract_token}"
            f"/consommationsJournalieres"
        )
        params = {
            "dateDebut": start.strftime(date_fmt),
            "dateFin": end.strftime(date_fmt),
        }

        session = self._new_session()
        try:
            async with session.get(
                url,
                params=params,
                headers={**COMMON_HEADERS, "authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status == 401:
                    # Token expiré entre-temps : invalider et ré-authentifier une fois
                    _LOGGER.warning("EGL: token expiré pendant le fetch, ré-authentification")
                    self._bearer_token = None
                    self._token_expiry = None

                if resp.status == 401:
                    # On doit rouvrir une nouvelle requête avec le nouveau token
                    token = await self._get_valid_token()
                else:
                    data = await resp.json()

            if resp.status == 401:
                async with session.get(
                    url,
                    params=params,
                    headers={**COMMON_HEADERS, "authorization": f"Bearer {token}"},
                ) as resp2:
                    if resp2.status != 200:
                        raise EGLApiError(f"Fetch consommations échoué ({resp2.status})")
                    data = await resp2.json()
        finally:
            await session.close()

        _LOGGER.debug(
            "EGL: fetch %s → %s OK",
            start.strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d"),
        )
        return _parse_consumption(data)


def _parse_consumption(data: dict | list) -> list[dict]:
    """Transforme la réponse API en liste normalisée.

    Format observé dans le konnector EGL :
      { "postes": [ { "data": [ { "consommation": N, "annee": Y, "mois": M (0-based), "jour": D } ] } ],
        "unites": { "consommation": "m3" | "l" } }
    """
    if isinstance(data, list):
        raw_list = data
        unit = "l"
    else:
        postes = data.get("postes", [])
        if not postes:
            return []
        raw_list = postes[0].get("data", [])
        unit = (data.get("unites") or {}).get("consommation", "l").lower()

    results = []
    for entry in raw_list:
        conso = entry.get("consommation", 0) or 0
        liters = float(conso) * 1000 if unit == "m3" else float(conso)
        annee = entry.get("annee", 0)
        mois = entry.get("mois", 0)
        jour = entry.get("jour", 1)
        month = mois + 1 if mois < 12 else mois
        date_str = f"{annee:04d}-{month:02d}-{jour:02d}"
        results.append({"date": date_str, "liters": liters})

    return sorted(results, key=lambda x: x["date"])
