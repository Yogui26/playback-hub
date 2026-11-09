# Playback Hub

Un dashboard web unique pour les statistiques de lecture d'**Emby** et de **Jellyfin**, à partir du plugin
**Playback Reporting** installé sur chacun des deux serveurs.

- Temps de visionnage par jour ou par mois, avec la part de chaque serveur
- Classement des utilisateurs, des films et des séries
- Applications, appareils, méthode de lecture (Direct Play, Direct Stream, transcodage)
- Carte des heures de visionnage et dernières lectures
- Filtres par période, par serveur et par utilisateur
- Regroupement des comptes d'une même personne entre les deux serveurs

Les données sont copiées dans une base SQLite locale : l'historique reste disponible même si un serveur
est éteint ou réinstallé.

## Prérequis

1. Le plugin **Playback Reporting** installé sur Emby et/ou Jellyfin (catalogue de plugins de chaque serveur).
2. Une **clé API** par serveur :
   - Emby : *Paramètres → Avancé → Clés API*
   - Jellyfin : *Tableau de bord → Avancé → Clés API*

Un seul des deux serveurs suffit pour démarrer ; l'autre peut être ajouté plus tard.

## Installation

`docker-compose.yml` :

```yaml
services:
  playback-hub:
    image: ghcr.io/yogui26/playback-hub:latest
    container_name: playback-hub
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      TZ: Europe/Paris
      EMBY_URL: http://192.168.1.10:8096
      EMBY_API_KEY: remplace-moi
      JELLYFIN_URL: http://192.168.1.10:8097
      JELLYFIN_API_KEY: remplace-moi
    volumes:
      - ./data:/data
```

```bash
docker compose up -d
```

Le dashboard est ensuite disponible sur `http://<ton-serveur>:8080`.

Le conteneur tourne avec l'utilisateur 1000. Si le dossier `./data` appartient à un autre utilisateur :
`sudo chown 1000:1000 data`.

## Configuration

| Variable | Défaut | Rôle |
|---|---|---|
| `EMBY_URL`, `EMBY_API_KEY` | – | Adresse et clé API du serveur Emby |
| `JELLYFIN_URL`, `JELLYFIN_API_KEY` | – | Adresse et clé API du serveur Jellyfin |
| `TZ` | UTC | Fuseau horaire (le même que celui des serveurs) |
| `SYNC_INTERVAL_MINUTES` | `15` | Fréquence de synchronisation |
| `MIN_PLAY_SECONDS` | `0` | Ignore les lectures plus courtes que ce seuil |
| `AUTH_USER`, `AUTH_PASSWORD` | – | Protège le dashboard par un mot de passe (authentification HTTP Basic) |
| `VERIFY_TLS` | `true` | Mettre `false` pour un certificat auto-signé |

## Fonctionnement

- Le collecteur interroge l'API du plugin (`/user_usage_stats/submit_custom_query`) et ne lit que la table
  `PlaybackActivity`. Il n'écrit jamais sur les serveurs.
- Seules les nouvelles lignes sont récupérées. Celles des dernières 48 heures sont relues à chaque passage,
  car le plugin met à jour la durée pendant la lecture.
- Sur Emby, la durée de pause est déduite du temps de lecture.
- Deux comptes portant le même nom (sans tenir compte des majuscules) sont regroupés. Pour des noms
  différents, le bouton **Utilisateurs** permet de leur donner un nom commun.
- Films et séries sont rapprochés entre les deux serveurs par leur titre.

## Limites connues

- Les heures sont celles enregistrées par chaque serveur : Emby, Jellyfin et le conteneur doivent être sur le
  même fuseau horaire.
- Un même film intitulé différemment sur les deux serveurs apparaît deux fois.
- Sans mot de passe (`AUTH_USER` / `AUTH_PASSWORD`), le dashboard est lisible par toute personne qui y accède :
  à ne pas exposer tel quel sur Internet.

## Développement

```bash
pip install -r requirements.txt pytest
python -m pytest -q tests            # tests de bout en bout avec de faux serveurs

python -m tests.mock_servers &       # faux Emby et Jellyfin sur le port 9900
DATA_DIR=./data EMBY_URL=http://localhost:9900/e EMBY_API_KEY=emby-key \
  JELLYFIN_URL=http://localhost:9900/j JELLYFIN_API_KEY=jf-key \
  uvicorn app.main:app --port 8080
```

Les graphiques utilisent [Chart.js](https://www.chartjs.org) (licence MIT), embarqué dans l'image.
