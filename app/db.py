"""Base SQLite unifiée."""
import os
import sqlite3
import unicodedata
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
    server       TEXT NOT NULL,
    src_rowid    INTEGER NOT NULL,
    date_created TEXT NOT NULL,      -- heure locale du serveur, 'YYYY-MM-DD HH:MM:SS'
    user_id      TEXT NOT NULL,
    item_id      TEXT,
    item_type    TEXT,
    item_name    TEXT,
    series       TEXT,               -- nom de la série pour les épisodes
    method       TEXT,               -- Direct Play / Direct Stream / Transcodage
    method_raw   TEXT,
    client       TEXT,
    device       TEXT,
    duration     INTEGER NOT NULL DEFAULT 0,   -- secondes, pauses déduites
    ip           TEXT,               -- adresse IP du lecteur
    ip_scope     TEXT,               -- 'local' ou 'remote'
    PRIMARY KEY (server, src_rowid)
);
CREATE INDEX IF NOT EXISTS idx_plays_date ON plays(date_created);
CREATE INDEX IF NOT EXISTS idx_plays_user ON plays(server, user_id);

CREATE TABLE IF NOT EXISTS users (
    server  TEXT NOT NULL,
    user_id TEXT NOT NULL,
    name    TEXT,
    alias   TEXT,                    -- nom commun choisi pour fusionner deux comptes
    PRIMARY KEY (server, user_id)
);

CREATE TABLE IF NOT EXISTS sync_state (
    server     TEXT PRIMARY KEY,
    last_try   TEXT,
    last_ok    TEXT,
    last_error TEXT,
    fetched    INTEGER DEFAULT 0
);

-- Adresses vues dans les sessions actives (pour les serveurs dont le plugin ne les enregistre pas).
CREATE TABLE IF NOT EXISTS session_ips (
    server     TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    ip         TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    PRIMARY KEY (server, user_id, item_id, ip)
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

VIEW = """
DROP VIEW IF EXISTS v_plays;
CREATE VIEW v_plays AS
SELECT p.*,
       COALESCE(NULLIF(TRIM(u.alias), ''), NULLIF(TRIM(u.name), ''),
                'Inconnu (' || substr(p.user_id, 1, 6) || ')') AS person
FROM plays p
LEFT JOIN users u ON u.server = p.server AND u.user_id = p.user_id;
"""


def fold(text) -> str:
    """Minuscules sans accents, pour une recherche tolérante (« episode » trouve « Épisode »)."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _migrate(con) -> None:
    columns = {r[1] for r in con.execute("PRAGMA table_info(plays)")}
    if "ip" not in columns:
        con.execute("ALTER TABLE plays ADD COLUMN ip TEXT")
        con.execute("ALTER TABLE plays ADD COLUMN ip_scope TEXT")
        # Les lectures déjà copiées n'ont pas d'adresse : on relit tout l'historique une fois.
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('full_resync:emby', '1'), "
                    "('full_resync:jellyfin', '1')")


def init() -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with connect() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        _migrate(con)
        con.executescript(VIEW)
        con.execute("CREATE INDEX IF NOT EXISTS idx_plays_ip ON plays(ip)")


@contextmanager
def connect():
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.create_function("fold", 1, fold, deterministic=True)
    try:
        yield con
        con.commit()
    finally:
        con.close()
