"""Faux serveurs Emby et Jellyfin exposant l'API du plugin Playback Reporting.

Usage : python -m tests.mock_servers  (port 9900)
  Emby     -> http://localhost:9900/e   (clé : emby-key)
  Jellyfin -> http://localhost:9900/j   (clé : jf-key)
"""
import os
import random
import sqlite3
import tempfile
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request

EMBY_KEY, JF_KEY = "emby-key", "jf-key"
USERS = {
    "emby": [("a1" * 16, "Yonel"), ("a2" * 16, "Marie"), ("a3" * 16, "Lucas")],
    # Jellyfin : mêmes personnes, mais « yonel » en minuscules et Lucas s'appelle « Luke ».
    "jellyfin": [("b1" * 16, "yonel"), ("b2" * 16, "Marie"), ("b3" * 16, "Luke"), ("b4" * 16, "Invité")],
}
MOVIES = ["Dune", "Inception", "Le Fabuleux Destin d'Amélie Poulain", "Interstellar", "Parasite", "Heat"]
SERIES = ["Severance", "The Bear", "Le Bureau des légendes", "Dark"]
CLIENTS = [("Emby for Android TV", "Shield Salon"), ("Emby Web", "Chrome"), ("Jellyfin Web", "Firefox"),
           ("Jellyfin Android", "Pixel 9"), ("Infuse", "Apple TV")]
METHODS = ["DirectPlay", "DirectStream", "Transcode (v:h264 a:aac)"]


def build_db(kind: str, path: str, seed: int, days: int = 120, n: int = 400) -> None:
    rnd = random.Random(seed)
    con = sqlite3.connect(path)
    cols = ("DateCreated DATETIME NOT NULL, UserId TEXT, ItemId TEXT, ItemType TEXT, ItemName TEXT, "
            "PlaybackMethod TEXT, ClientName TEXT, DeviceName TEXT, PlayDuration INT")
    if kind == "emby":  # le plugin Emby a des colonnes en plus
        cols += ", PauseDuration INT, RemoteAddress TEXT"
    con.execute(f"CREATE TABLE PlaybackActivity ({cols})")
    now = datetime.now()
    for _ in range(n):
        when = now - timedelta(days=rnd.random() * days)
        when = when.replace(hour=rnd.choice([8, 13, 18, 20, 20, 21, 21, 22, 23]), minute=rnd.randrange(60))
        if when > now:
            when -= timedelta(days=1)
        uid = rnd.choice(USERS[kind])[0]
        if kind == "jellyfin":  # Jellyfin peut stocker l'identifiant avec des tirets
            uid = f"{uid[:8]}-{uid[8:12]}-{uid[12:16]}-{uid[16:20]}-{uid[20:]}"
        if rnd.random() < 0.4:
            item_type, name = "Movie", rnd.choice(MOVIES)
            duration = rnd.randrange(600, 9000)
        else:
            item_type = "Episode"
            name = f"{rnd.choice(SERIES)} - s{rnd.randrange(1, 4):02d}e{rnd.randrange(1, 11):02d} - Épisode"
            duration = rnd.randrange(300, 3600)
        client, device = rnd.choice(CLIENTS)
        row = [when.strftime("%Y-%m-%d %H:%M:%S.%f") + "0", uid, str(rnd.randrange(1000, 9999)), item_type, name,
               rnd.choice(METHODS), client, device, duration]
        if kind == "emby":
            row += [rnd.randrange(0, 120), "192.168.1.20"]
        con.execute(f"INSERT INTO PlaybackActivity VALUES ({','.join('?' * len(row))})", row)
    con.commit()
    con.close()


def create_app(emby_db: str, jf_db: str) -> FastAPI:
    app = FastAPI()

    def check(kind: str, request: Request) -> None:
        if kind == "emby":
            ok = request.headers.get("x-emby-token") == EMBY_KEY
        else:
            ok = f'Token="{JF_KEY}"' in request.headers.get("authorization", "")
        if not ok:
            raise HTTPException(401)

    def run_query(path: str, sql: str) -> dict:
        con = sqlite3.connect(path)
        try:
            cur = con.execute(sql)
            columns = [c[0] for c in cur.description]
            # Comme le vrai plugin : toutes les valeurs sont renvoyées en chaînes.
            results = [[None if v is None else str(v) for v in row] for row in cur.fetchall()]
            if not results:  # comportement du vrai plugin quand il n'y a aucune ligne
                return {"colums": [], "results": [],
                        "message": "Query executed, no data returned.</br>Number of rows effected : 0"}
            return {"colums": columns, "results": results, "message": ""}
        except sqlite3.Error as e:
            return {"colums": [], "results": [], "message": f"Error Running Query</br>{e}"}
        finally:
            con.close()

    for kind, prefix, path in (("emby", "/e/emby", emby_db), ("jellyfin", "/j", jf_db)):
        def make(kind=kind, path=path):
            async def custom_query(request: Request):
                check(kind, request)
                body = await request.json()
                return run_query(path, body["CustomQueryString"])

            async def users(request: Request):
                check(kind, request)
                return [{"Id": uid, "Name": name} for uid, name in USERS[kind]]
            return custom_query, users
        cq, us = make()
        app.post(f"{prefix}/user_usage_stats/submit_custom_query")(cq)
        app.get(f"{prefix}/Users")(us)
    return app


if __name__ == "__main__":
    import uvicorn
    tmp = tempfile.mkdtemp()
    e, j = os.path.join(tmp, "emby.db"), os.path.join(tmp, "jf.db")
    build_db("emby", e, seed=1)
    build_db("jellyfin", j, seed=2)
    uvicorn.run(create_app(e, j), host="0.0.0.0", port=9900, log_level="warning")
