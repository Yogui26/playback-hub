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
# Formats variés, comme en vrai : IPv4, IPv4 avec port, IPv4 encapsulée dans IPv6, adresse publique.
EMBY_IPS = ["192.168.1.20", "192.168.1.21:8096", "::ffff:192.168.1.20", "82.64.10.5", "2a01:e0a:1:2::10"]
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
            row += [rnd.randrange(0, 120), rnd.choice(EMBY_IPS)]
        con.execute(f"INSERT INTO PlaybackActivity VALUES ({','.join('?' * len(row))})", row)
    con.commit()
    con.close()


def create_app(emby_db: str, jf_db: str) -> FastAPI:
    app = FastAPI()
    app.state.sessions = {"emby": [], "jellyfin": []}
    app.state.activity = {"emby": [], "jellyfin": []}     # entrées du journal d'activité
    app.state.items = {"emby": {}, "jellyfin": {}}        # médias connus, par identifiant
    app.state.jobs = []                                   # téléchargements hors ligne (Emby)

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
            async def sessions(request: Request):
                check(kind, request)
                return app.state.sessions[kind]
            async def activity(request: Request):
                check(kind, request)
                q = request.query_params
                entries = sorted(app.state.activity[kind], key=lambda e: e["Date"], reverse=True)
                if q.get("MinDate"):
                    entries = [e for e in entries if e["Date"] >= q["MinDate"]]
                start, limit = int(q.get("StartIndex", 0)), int(q.get("Limit", 100))
                return {"Items": entries[start:start + limit], "TotalRecordCount": len(entries)}

            async def items(request: Request):
                check(kind, request)
                ids = [i for i in request.query_params.get("Ids", "").split(",") if i]
                return {"Items": [app.state.items[kind][i] for i in ids if i in app.state.items[kind]]}
            return custom_query, users, sessions, activity, items
        cq, us, se, ac, it = make()
        app.get(f"{prefix}/Sessions")(se)
        app.get(f"{prefix}/System/ActivityLog/Entries")(ac)
        app.get(f"{prefix}/Items")(it)
        app.post(f"{prefix}/user_usage_stats/submit_custom_query")(cq)
        app.get(f"{prefix}/Users")(us)
    @app.get("/e/emby/Sync/Jobs")
    async def sync_jobs(request: Request):
        check("emby", request)
        return {"Items": app.state.jobs, "TotalRecordCount": len(app.state.jobs)}

    return app


def demo_live(app: FastAPI) -> None:
    """Remplit les faux serveurs avec des lectures en cours et des téléchargements, pour la démo."""
    from datetime import timezone
    now = datetime.now(timezone.utc)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    video = lambda w, h: [{"Type": "Video", "Width": w, "Height": h}]
    app.state.sessions["emby"] = [
        {"UserId": "a1" * 16, "UserName": "Yonel", "Client": "Emby for Android TV", "DeviceName": "Shield Salon",
         "RemoteEndPoint": "192.168.1.20", "PlayState": {"PlayMethod": "DirectPlay", "PositionTicks": 41 * 600000000},
         "NowPlayingItem": {"Id": "1001", "Name": "Dune", "Type": "Movie", "ProductionYear": 2021,
                            "RunTimeTicks": 155 * 600000000, "Bitrate": 18500000, "MediaStreams": video(3840, 1600)}},
        {"UserId": "a2" * 16, "UserName": "Marie", "Client": "Emby Web", "DeviceName": "Chrome",
         "RemoteEndPoint": "82.64.10.5", "PlayState": {"PlayMethod": "Transcode", "PositionTicks": 12 * 600000000},
         "TranscodingInfo": {"IsVideoDirect": False, "IsAudioDirect": True, "VideoCodec": "h264", "Bitrate": 4000000,
                             "TranscodeReasons": ["ContainerBitrateExceedsLimit"]},
         "NowPlayingItem": {"Id": "1002", "Name": "Chikhai Bardo", "Type": "Episode", "SeriesName": "Severance",
                            "ParentIndexNumber": 2, "IndexNumber": 7, "RunTimeTicks": 50 * 600000000,
                            "MediaStreams": video(1920, 1080)}},
        {"UserId": "a3" * 16, "UserName": "Lucas", "Client": "Emby Web", "DeviceName": "Firefox"},
    ]
    app.state.sessions["jellyfin"] = [
        {"UserId": "b3" * 16, "UserName": "Luke", "Client": "Jellyfin Android", "DeviceName": "Pixel 9",
         "RemoteEndPoint": "90.12.34.56",
         "PlayState": {"PlayMethod": "DirectStream", "PositionTicks": 20 * 600000000, "IsPaused": True},
         "NowPlayingItem": {"Id": "d" * 32, "Name": "Review", "Type": "Episode", "SeriesName": "The Bear",
                            "ParentIndexNumber": 1, "IndexNumber": 7, "RunTimeTicks": 21 * 600000000,
                            "Bitrate": 6200000, "MediaStreams": video(1920, 1080)}},
    ]
    gb = 1024 ** 3
    app.state.items["jellyfin"] = {
        "e" * 32: {"Id": "e" * 32, "Name": "Interstellar", "Type": "Movie", "MediaSources": [{"Size": int(14.2 * gb)}]},
        "f" * 32: {"Id": "f" * 32, "Name": "Forks", "Type": "Episode", "SeriesName": "The Bear",
                   "ParentIndexNumber": 2, "IndexNumber": 7, "MediaSources": [{"Size": int(2.1 * gb)}]},
        "9" * 32: {"Id": "9" * 32, "Name": "Parasite", "Type": "Movie", "MediaSources": [{"Size": int(8.4 * gb)}]},
    }
    app.state.items["emby"] = {
        "2001": {"Id": "2001", "Name": "Heat", "Type": "Movie", "MediaSources": [{"Size": int(11.6 * gb)}]},
    }

    def entry(i, kind, user, item, hours, overview):
        return {"Id": i, "Type": kind, "UserId": user, "ItemId": item, "Date": iso(now - timedelta(hours=hours)),
                "Name": "entrée du journal", "ShortOverview": overview}
    app.state.activity["jellyfin"] = [
        entry(1, "UserDownloadingContent", "b1" * 16, "e" * 32, 5, "Application : Jellyfin Android, Appareil : Pixel 9"),
        entry(2, "UserDownloadingContent", "b1" * 16, "e" * 32, 5.1, "Application : Jellyfin Android, Appareil : Pixel 9"),
        entry(3, "UserDownloadingContent", "b3" * 16, "f" * 32, 30, "Application : Jellyfin Web, Appareil : Firefox"),
        entry(4, "UserDownloadingContent", "b2" * 16, "9" * 32, 75, "Application : Jellyfin Web, Appareil : Chrome"),
        entry(5, "VideoPlayback", "b2" * 16, "9" * 32, 76, ""),
        entry(6, "SessionStarted", "b2" * 16, None, 77, ""),
        entry(7, "SubtitlesDownloaded", "b2" * 16, "9" * 32, 78, ""),
    ]
    app.state.activity["emby"] = [
        entry(1, "UserDownloadingContent", "a3" * 16, "2001", 50, "Emby Web, Chrome"),
        entry(2, "playback.start", "a1" * 16, "1001", 1, ""),
    ]
    app.state.jobs = [
        {"Id": 12, "Name": "Dark - Saison 1", "UserId": "a2" * 16, "TargetName": "iPad de Marie", "Status": "Completed",
         "ItemCount": 10, "Category": "Series", "RequestedItemIds": ["3001"], "DateCreated": iso(now - timedelta(days=4))},
    ]


if __name__ == "__main__":
    import uvicorn
    tmp = tempfile.mkdtemp()
    e, j = os.path.join(tmp, "emby.db"), os.path.join(tmp, "jf.db")
    build_db("emby", e, seed=1)
    build_db("jellyfin", j, seed=2)
    demo = create_app(e, j)
    demo_live(demo)
    uvicorn.run(demo, host="0.0.0.0", port=9900, log_level="warning")
