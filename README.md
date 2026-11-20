# EGL (Eau du Grand Lyon) — Intégration Home Assistant

[![Works with Home Assistant](https://img.shields.io/badge/Works%20with-Home%20Assistant-41BDF5?logo=homeassistant&logoColor=white)](https://www.home-assistant.io/)
[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg?logo=homeassistantcommunitystore&logoColor=white)](https://github.com/hacs/integration)
[![Latest Version](https://img.shields.io/github/v/tag/aldweb/ha-egl-water?label=Latest%20Version&logo=github&color=blue)](https://github.com/aldweb/ha-egl-water/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> **Note for English speakers:** This documentation is intentionally written in French. This integration is specific to the Lyon metropolitan area (France) and is only relevant to residents served by the *Eau du Grand Lyon* water utility. There is no English-speaking audience for this project.

Intégration custom pour récupérer votre consommation d'eau quotidienne depuis l'espace client **agence.eaudugrandlyon.com** (compteur Téléo). Elle publie l'historique complet (volume + coût estimé) sous forme de **statistiques externes** que vous sélectionnez dans le **tableau de bord Énergie** de Home Assistant, dès la première installation.

---

## Fonctionnalités

- **5 capteurs** : consommation journalière, mensuelle, glissante 30 jours + coûts estimés associés
- **Import historique automatique** sur 2 ans au premier démarrage (volume et coût)
- **Mise à jour incrémentale** à deux horaires configurables (par défaut 08 h 00 et 16 h 00, heure locale)
- **Recalcul rétroactif du coût** si vous modifiez le tarif dans les options
- **Statistiques externes** (volume + coût) à ajouter dans la section Eau du **tableau de bord Énergie** — voir [Tableau de bord Énergie](#tableau-de-bord-énergie)
- Authentification **OAuth2 PKCE**, identique au navigateur — aucune dépendance externe

---

## Capteurs créés

Les capteurs sont regroupés dans l'appareil **Eau du Grand Lyon** ; leurs identifiants sont donc préfixés par `sensor.eau_du_grand_lyon_`.

| Entité | Description | Unité |
|--------|-------------|-------|
| `sensor.eau_du_grand_lyon_consommation_journaliere` | Dernier jour publié par EGL | L |
| `sensor.eau_du_grand_lyon_consommation_mensuelle` | Cumul du mois calendaire en cours | L |
| `sensor.eau_du_grand_lyon_consommation_30_derniers_jours` | Fenêtre glissante 30 jours | L |
| `sensor.eau_du_grand_lyon_cout_journalier` | Coût estimé TTC du dernier jour publié | € |
| `sensor.eau_du_grand_lyon_cout_mensuel` | Coût estimé TTC du mois en cours | € |

> Les identifiants dépendent de la **langue du serveur Home Assistant** (**Paramètres** → **Système** → **Général**), et non de la langue d'affichage de votre profil : ils sont fixés à la création des entités. Ceux du tableau correspondent à un serveur en français ; avec une autre langue, ils sont construits à partir des noms anglais (par exemple `sensor.eau_du_grand_lyon_daily_consumption`). Vérifiez les vôtres dans **Paramètres** → **Appareils et services** → **Entités**.

> **Note sur le retard de publication** : EGL publie les données avec un délai variable (souvent 1 à 3 jours, parfois le vendredi et samedi d'un seul coup le mardi suivant). L'intégration gère ce décalage automatiquement — le capteur journalier porte toujours la date du dernier relevé réellement disponible.

L'attribut `retard_publication_jours` du capteur journalier indique le nombre de jours de décalage entre le dernier relevé publié et aujourd'hui.

> **Ces capteurs sont purement informatifs** : ils n'ont pas de `state_class` et ne peuvent donc **pas** être sélectionnés dans le tableau de bord Énergie. Pour celui-ci, utilisez les statistiques externes décrites [plus bas](#tableau-de-bord-énergie).

---

## Prérequis

- Home Assistant ≥ 2026.3.0
- Un compte actif sur [agence.eaudugrandlyon.com](https://agence.eaudugrandlyon.com) (compteur Téléo compatible)

---

## Installation

### Via le Marketplace ou HACS (recommandé)

Depuis Home Assistant 2026.11, HACS est intégré à Home Assistant sous le nom de **Marketplace** (même catalogue, mêmes dépôts). Jusqu'à Home Assistant 2026.10, on utilise HACS.

**Home Assistant 2026.11 ou plus récent : Marketplace**

1. Ouvrez **Paramètres** → **Marketplace**
2. Recherchez **Eau du Grand Lyon** dans le catalogue. S'il n'y figure pas encore, choisissez **Ajouter depuis un lien** (*Add from link*) et saisissez `https://github.com/aldweb/ha-egl-water` (une connexion GitHub est demandée pour ajouter un dépôt personnalisé)
3. Installez **Eau du Grand Lyon** : la page de configuration s'ouvre ensuite ; si Home Assistant le demande, redémarrez-le d'abord

**Home Assistant 2026.10 ou plus ancien : HACS**

1. Dans HACS → ⋮ → **Dépôts personnalisés**
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

Si votre compte comporte **plusieurs contrats**, seuls les contrats **actifs** sont proposés :
- un seul contrat actif → il est sélectionné automatiquement, sans question ;
- plusieurs contrats actifs → vous choisissez celui à suivre ;
- aucun contrat actif → la configuration est refusée (les contrats cessés ne renvoient aucune consommation).

L'intégration se connecte, vérifie les identifiants, puis lance en arrière-plan l'**import historique** (2 ans de données). Ce processus prend quelques minutes ; les capteurs sont immédiatement disponibles avec les données du jour.

### Options (modifiables après installation)

Accédez aux options via **Paramètres** → **Appareils et services** → **Eau du Grand Lyon** → **Configurer** :

| Option | Par défaut | Description |
|--------|------------|-------------|
| Heure de mise à jour 1 | `08:00` | Premier refresh quotidien (heure locale, format HH:MM) |
| Heure de mise à jour 2 | `16:00` | Second refresh quotidien (heure locale, format HH:MM) |
| Tarif €/m³ TTC | `3,56` | Tarif tout compris pour le calcul du coût estimé |

> Si vous modifiez le tarif, l'historique des coûts est **recalculé automatiquement** sur l'ensemble de l'historique importé.

### Changer de contrat

Contrat cessé, déménagement, nouveau contrat : **Paramètres** → **Appareils et services** → **Eau du Grand Lyon** → menu **⋮** → **Reconfigurer**, puis choisissez le contrat actif. Aucune suppression de l'intégration n'est nécessaire ; en cas de changement de contrat, l'historique est ré-importé.

---

## Tableau de bord Énergie

Le tableau de bord Énergie s'appuie sur deux **statistiques externes** créées par l'intégration (et non sur les capteurs `sensor.*`, dont le nom ne contient pas `<compte>`) :

| Statistique | Contenu | Unité |
|-------------|---------|-------|
| `egl_water:<compte>_daily` | Volume d'eau consommé (cumul) | L |
| `egl_water:<compte>_daily_cost` | Coût estimé TTC (cumul) | EUR |

### Retrouver votre `<compte>`

`<compte>` (appelé *slug* dans le code, voir `statistic_slug` dans `const.py`) est dérivé de l'e-mail de votre compte EGL : on prend la partie **avant le `@`**, on la passe en **minuscules**, puis chaque suite de caractères autres que `a-z` et `0-9` est remplacée par un seul `_` (les `_` en début et fin sont supprimés ; si le résultat est vide, `<compte>` vaut `account`).

| E-mail | `<compte>` | Statistique de volume |
|--------|----------|-----------------------|
| `jean.dupont@example.com` | `jean_dupont` | `egl_water:jean_dupont_daily` |
| `Jean+Eau@example.com` | `jean_eau` | `egl_water:jean_eau_daily` |
| `marie-claire_01@example.com` | `marie_claire_01` | `egl_water:marie_claire_01_daily` |

> Vous pouvez aussi retrouver les identifiants exacts dans **Outils de développement** → **Statistiques** (recherchez `egl_water`).

### Ajouter les statistiques au tableau de bord

1. **Paramètres** → **Tableaux de bord** → **Énergie**
2. Dans la section **Eau**, cliquez sur **Ajouter une source d'eau**
3. Sélectionnez la statistique de volume `egl_water:<compte>_daily` (nom affiché : *Consommation journalière eau*)
4. Pour le coût, choisissez l'option « entité qui suit le coût total » et sélectionnez `egl_water:<compte>_daily_cost` (nom affiché : *Coût journalier eau*)

Les statistiques contiennent l'historique importé (2 ans) : les graphiques du tableau de bord Énergie sont donc renseignés rétroactivement dès l'ajout de la source.

---

## Fonctionnement technique

### Authentification

L'intégration utilise le flux **OAuth2 PKCE** de l'espace client EGL (identique à ce que fait votre navigateur). Le token Bearer est valable 1 heure et renouvelé automatiquement. Aucune bibliothèque externe n'est requise.

### Mise à jour des données

Les données sont rafraîchies à deux horaires fixes configurables (pas d'intervalle dérivant). À chaque refresh, l'API est interrogée sur une **fenêtre glissante fixe des 90 derniers jours jusqu'à aujourd'hui**, quelle que soit la dernière date déjà connue, et toute la fenêtre est réécrite dans le recorder (y compris les jours à 0 L). Cela permet d'absorber les publications groupées ou rétroactives d'EGL, ainsi que les corrections de valeurs déjà publiées, sans logique de détection de nouveauté.

Les nouvelles entrées sont poussées directement dans le **recorder** de Home Assistant via les statistiques externes — le tableau de bord Énergie les voit immédiatement sans redémarrage.

### Import historique

Au premier démarrage, l'intégration importe jusqu'à **2 ans** de données en tranches de 90 jours. Cet import s'effectue en tâche de fond sans bloquer Home Assistant. Les données de volume et le coût estimé (selon le tarif configuré) sont importés simultanément.

---

## Dépannage

| Symptôme | Cause probable | Solution |
|----------|---------------|----------|
| Erreur `invalid_auth` au démarrage | Identifiants incorrects | Vérifiez e-mail et mot de passe sur agence.eaudugrandlyon.com |
| Erreur `cannot_connect` | API EGL indisponible | Réessayez ultérieurement ; l'intégration se rétablira automatiquement |
| Capteurs à `unknown` et log « aucune donnée reçue » | Le contrat suivi est cessé (compte à plusieurs contrats) | **Reconfigurer** l'intégration et choisir le contrat actif |
| Capteurs bloqués à `unavailable` | Problème réseau ou changement d'API | Consultez les logs HA |
| Import historique très long | Normal pour 2 ans de données | Patienter quelques minutes ; les capteurs fonctionnent pendant l'import |
| Coût non recalculé après changement de tarif | Recalcul en cours en arrière-plan | Patienter quelques minutes |

**Consulter les logs :**
**Paramètres** → **Journaux** → filtrez sur `egl_water`

Si l'API EGL évolue (changement d'endpoint ou de format), ouvrez une issue sur [github.com/aldweb/ha-egl-water/issues](https://github.com/aldweb/ha-egl-water/issues).

---

## Licence

MIT — voir [LICENSE](LICENSE)
