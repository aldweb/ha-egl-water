# Eau du Grand Lyon — Intégration Home Assistant

Intégration custom pour récupérer votre consommation d'eau quotidienne depuis l'espace client **agence.eaudugrandlyon.com** (compteur Téléo).

## Capteurs créés

| Entité | Description | Unité |
|--------|-------------|-------|
| `sensor.eau_du_grand_lyon_consommation_journaliere` | Consommation du dernier jour relevé | Litres |
| `sensor.eau_du_grand_lyon_consommation_mensuelle` | Cumul du mois en cours | Litres |
| `sensor.eau_du_grand_lyon_consommation_30_derniers_jours` | Fenêtre glissante 30 j | Litres |

Les trois capteurs sont compatibles avec le **tableau de bord Énergie** de Home Assistant (catégorie Eau).

## Installation

### Via HACS (recommandé)

1. Dans HACS → Intégrations → ⋮ → Dépôts personnalisés
2. Ajoutez `https://github.com/aldweb/ha-egl-water`, catégorie **Intégration**
3. Installez « Eau du Grand Lyon »
4. Redémarrez Home Assistant

### Manuelle

Copiez le dossier `egl_water/` dans votre dossier `config/custom_components/` :

```
config/
  custom_components/
    egl_water/
      __init__.py
      api.py
      config_flow.py
      const.py
      coordinator.py
      manifest.json
      sensor.py
      translations/
        fr.json
```

Redémarrez Home Assistant.

## Configuration

1. **Paramètres** → **Appareils et services** → **+ Ajouter une intégration**
2. Recherchez « Eau du Grand Lyon »
3. Saisissez l'e-mail et le mot de passe de votre espace client agence.eaudugrandlyon.com

L'intégration se connecte automatiquement et crée les 3 capteurs. Les données sont rafraîchies toutes les **6 heures**.

## Tableau de bord Énergie

1. **Paramètres** → **Tableau de bord Énergie** → section **Eau**
2. Ajoutez `sensor.eau_du_grand_lyon_consommation_journaliere`

## Notes techniques

- L'authentification utilise le flux **OAuth2 PKCE** de l'espace client (identique à ce que fait votre navigateur).
- Le token Bearer est valable **1 heure** ; l'intégration le renouvelle automatiquement.
- La fréquence de mise à jour est limitée à 6h car les données côté serveur ne sont mises à jour qu'une fois par jour.

## Dépannage

| Erreur | Cause probable |
|--------|---------------|
| `invalid_auth` | E-mail ou mot de passe incorrect |
| `cannot_connect` | Serveur indisponible ou changement d'API |
| Capteurs à `unavailable` | Vérifiez les logs HA (`Paramètres → Journaux → Filtrer "egl_water"`) |

Si l'API évolue (changement d'endpoint, de format de réponse), ouvrez une issue sur https://github.com/aldweb/ha-egl-water/issues
