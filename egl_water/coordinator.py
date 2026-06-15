"""Coordinator pour la mise à jour des données EGL."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EGLApiError, EGLAuthError, EGLClient
from .const import DOMAIN, UPDATE_INTERVAL_HOURS

_LOGGER = logging.getLogger(__name__)


class EGLDataCoordinator(DataUpdateCoordinator):
    """Récupère et met en cache les données de consommation EGL."""

    def __init__(self, hass: HomeAssistant, client: EGLClient, contract_token: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=UPDATE_INTERVAL_HOURS),
        )
        self._client = client
        self._contract_token = contract_token

    async def _async_update_data(self) -> dict:
        """Appelé par HA pour rafraîchir les données."""
        now = datetime.now(timezone.utc)
        # On récupère les 32 derniers jours pour avoir le mois courant complet
        start = now - timedelta(days=32)

        try:
            entries = await self._client.fetch_daily_consumption(
                self._contract_token, start, now
            )
        except EGLAuthError as err:
            raise UpdateFailed(f"Authentification EGL échouée : {err}") from err
        except EGLApiError as err:
            raise UpdateFailed(f"Erreur API EGL : {err}") from err

        if not entries:
            # Pas de nouvelles données, on garde l'ancien état
            return self.data or {}

        # Dernier jour disponible
        latest = entries[-1]

        # Consommation du jour en cours (ou du dernier jour connu)
        today_str = now.strftime("%Y-%m-%d")
        yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        today_entry = next(
            (e for e in reversed(entries) if e["date"] in (today_str, yesterday_str)),
            latest,
        )

        # Cumul du mois courant
        current_month = now.strftime("%Y-%m")
        monthly_total = sum(
            e["liters"] for e in entries if e["date"].startswith(current_month)
        )

        # Cumul des 30 derniers jours
        cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        rolling_30d = sum(e["liters"] for e in entries if e["date"] >= cutoff)

        return {
            "daily_liters": today_entry["liters"],
            "daily_date": today_entry["date"],
            "monthly_liters": monthly_total,
            "rolling_30d_liters": rolling_30d,
            "history": entries,
            "last_update": now.isoformat(),
        }
