# EGL (Eau du Grand Lyon) — Intégration Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
![Version](https://img.shields.io/badge/version-1.9.1-blue)
![HA min](https://img.shields.io/badge/Home%20Assistant-%3E%3D%202025.1-green)

> **Note for English speakers:** This documentation is intentionally written in French. This integration is specific to the Lyon metropolitan area (France) and is only relevant to residents served by the *Eau du Grand Lyon* water utility. There is no English-speaking audience for this project.

Intégration custom pour récupérer votre consommation d'eau quotidienne depuis l'espace client **agence.eaudugrandlyon.com** (compteur Téléo). Elle alimente le **tableau de bord Énergie** de Home Assistant avec l'historique complet (volume + coût estimé) dès la première installation.

---

## Fonctionnalités

- **5 capteurs** : consommation journalière, mensuelle, glissante 30 jours + coûts estimés associés
- **Import historique automatique** sur 2 ans au premier démarrage (volume et coût)
- **Mise à jour incrémentale** à deux horaires configurables (par défaut 08 h 00 et 16 h 00, heure locale)
- **Recalcul rétroactif du coût** si vous modifiez le tarif dans les options
- Intégration native avec le **tableau de bord Énergie** (catégorie Eau)
- Authentification **OAuth2 PKCE**, identique au navigateur — aucune dépendance externe

---

## Capteurs créés

| Entité | Description | Unité |
|--------|-------------|-------|
| `sensor.consommation_journaliere` | Dernier jour publié par EGL | L |
| `sensor.consommation_mensuelle` | Cumul du mois calendaire en cours | L |
| `sensor.consommation_30_derniers_jours` | Fenêtre glissante 30 jours | L |
| `sensor.cout_journalier` | Coût estimé TTC du dernier jour publié | € |
| `sensor.cout_mensuel` | Coût estimé TTC du mois en cours | € |

> **Note sur le retard de publication** : EGL publie les données avec un délai variable (souvent 1 à 3 jours, parfois le vendredi et samedi d'un seul coup le mardi suivant). L'intégration gère ce décalage automatiquement — le capteur journalier porte toujours la date du dernier relevé réellement disponible.

L'attribut `retard_publication_jours` du capteur journalier indique le nombre de jours de décalage entre le dernier relevé publié et aujourd'hui.

---

## Prérequis

- Home Assistant ≥ 2025.1.0
- Un compte actif sur [agence.eaudugrandlyon.com](https://agence.eaudugrandlyon.com) (compteur Téléo compatible)

---

## Installation

### Via HACS (recommandé)

1. Dans HACS → **Intégrations** → ⋮ → **Dépôts personnalisés**
2. Ajoutez `https://github.com/aldweb/ha-egl-water`, catégorie **Intégration**
3. Installez **Eau du Grand Lyon**
4. Redémarrez Home Assistant

### Installation manuelle

Copiez le dossier `custom_components/egl_water/` dans `config/custom_components/`, puis redémarrez Home Assistant.

---

## Configuration

1. **Paramètres** → **Appareils et services** → **+ Ajouter une intégration**
2. Recherchez **Eau du Grand Lyon**
3. Saisissez l'e-mail et le mot de passe de votre espace client agence.eaudugrandlyon.com

L'intégration se connecte, vérifie les identifiants, puis lance en arrière-plan l'**import historique** (2 ans de données). Ce processus prend quelques minutes ; les capteurs sont immédiatement disponibles avec les données du jour.

### Options (modifiables après installation)

Accédez aux options via **Paramètres** → **Appareils et services** → **Eau du Grand Lyon** → **Configurer** :

| Option | Par défaut | Description |
|--------|------------|-------------|
| Heure de mise à jour 1 | `08:00` | Premier refresh quotidien (heure locale, format HH:MM) |
| Heure de mise à jour 2 | `16:00` | Second refresh quotidien (heure locale, format HH:MM) |
| Tarif €/m³ TTC | `3,56` | Tarif tout compris pour le calcul du coût estimé |

> Si vous modifiez le tarif, l'historique des coûts est **recalculé automatiquement** sur l'ensemble de l'historique importé.

---

## Tableau de bord Énergie

1. **Paramètres** → **Tableau de bord Énergie** → section **Eau**
2. Ajoutez `sensor.consommation_journaliere`

Les statistiques de volume et de coût alimentent directement les graphiques du tableau de bord Énergie, y compris l'historique rétroactif.

---

## Fonctionnement technique

### Authentification

L'intégration utilise le flux **OAuth2 PKCE** de l'espace client EGL (identique à ce que fait votre navigateur). Le token Bearer est valable 1 heure et renouvelé automatiquement. Aucune bibliothèque externe n'est requise.

### Mise à jour des données

Les données sont rafraîchies à deux horaires fixes configurables (pas d'intervalle dérivant). À chaque refresh, l'API est interrogée sur une fenêtre remontant à **10 jours avant le dernier relevé connu**, ce qui permet de récupérer les publications groupées ou rétroactives d'EGL.

Les nouvelles entrées sont poussées directement dans le **recorder** de Home Assistant via les statistiques externes — le tableau de bord Énergie les voit immédiatement sans redémarrage.

### Import historique

Au premier démarrage, l'intégration importe jusqu'à **2 ans** de données en tranches de 90 jours. Cet import s'effectue en tâche de fond sans bloquer Home Assistant. Les données de volume et le coût estimé (selon le tarif configuré) sont importés simultanément.

---

## Dépannage

| Symptôme | Cause probable | Solution |
|----------|---------------|----------|
| Erreur `invalid_auth` au démarrage | Identifiants incorrects | Vérifiez e-mail et mot de passe sur agence.eaudugrandlyon.com |
| Erreur `cannot_connect` | API EGL indisponible | Réessayez ultérieurement ; l'intégration se rétablira automatiquement |
| Capteurs bloqués à `unavailable` | Problème réseau ou changement d'API | Consultez les logs HA |
| Import historique très long | Normal pour 2 ans de données | Patienter quelques minutes ; les capteurs fonctionnent pendant l'import |
| Coût non recalculé après changement de tarif | Recalcul en cours en arrière-plan | Patienter quelques minutes |

**Consulter les logs :**
**Paramètres** → **Journaux** → filtrez sur `egl_water`

Si l'API EGL évolue (changement d'endpoint ou de format), ouvrez une issue sur [github.com/aldweb/ha-egl-water/issues](https://github.com/aldweb/ha-egl-water/issues).

---

## Licence

MIT — voir [LICENSE](LICENSE)
