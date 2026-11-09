"""Configuration lue depuis les variables d'environnement."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Server:
    key: str        # "emby" ou "jellyfin"
    label: str
    url: str
    api_key: str

    @property
    def api_base(self) -> str:
        # Emby sert son API sous /emby, Jellyfin à la racine.
        return f"{self.url}/emby" if self.key == "emby" else self.url

    @property
    def headers(self) -> dict:
        h = {"X-Emby-Token": self.api_key, "Accept": "application/json"}
        if self.key == "jellyfin":
            h["Authorization"] = f'MediaBrowser Token="{self.api_key}"'
        return h


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def load_servers() -> list[Server]:
    servers = []
    for key, label in (("emby", "Emby"), ("jellyfin", "Jellyfin")):
        url = _env(f"{key.upper()}_URL").rstrip("/")
        api_key = _env(f"{key.upper()}_API_KEY")
        if url and api_key:
            servers.append(Server(key, label, url, api_key))
    return servers


DATA_DIR = _env("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "playback-hub.db")
SYNC_INTERVAL_MINUTES = max(1, int(_env("SYNC_INTERVAL_MINUTES", "15") or 15))
# Les lectures plus courtes que ce seuil (en secondes) sont ignorées dans les stats.
MIN_PLAY_SECONDS = max(0, int(_env("MIN_PLAY_SECONDS", "0") or 0))
# Les lignes plus récentes que ce délai sont relues à chaque synchro,
# car le plugin met à jour la durée pendant la lecture.
RESYNC_WINDOW_HOURS = 48
# Fréquence de relevé des sessions actives, pour connaître l'adresse IP des lectures en cours.
SESSION_POLL_SECONDS = max(15, int(_env("SESSION_POLL_SECONDS", "60") or 60))
AUTH_USER = _env("AUTH_USER")
AUTH_PASSWORD = _env("AUTH_PASSWORD")
VERIFY_TLS = _env("VERIFY_TLS", "true").lower() not in ("0", "false", "no")
VERSION = (_env("APP_VERSION") or "dev")[:7]
