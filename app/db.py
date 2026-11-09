"""Base SQLite unifiée."""
import os
import sqlite3
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

DROP VIEW IF EXISTS v_plays;
CREATE VIEW v_plays AS
SELECT p.*,
       COALESCE(NULLIF(TRIM(u.alias), ''), NULLIF(TRIM(u.name), ''),
                'Inconnu (' || substr(p.user_id, 1, 6) || ')') AS person
FROM plays p
LEFT JOIN users u ON u.server = p.server AND u.user_id = p.user_id;
"""


def init() -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with connect() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)


@contextmanager
def connect():
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()
