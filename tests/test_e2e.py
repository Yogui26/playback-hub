"""Test de bout en bout : faux serveurs -> collecteur -> statistiques."""
import asyncio
import os
import sqlite3
import threading
import time

import pytest
import uvicorn

from tests import mock_servers

PORT = 9911


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("hub")
    emby_db, jf_db = str(tmp / "emby.db"), str(tmp / "jf.db")
    mock_servers.build_db("emby", emby_db, seed=1)
    mock_servers.build_db("jellyfin", jf_db, seed=2)
    mock = mock_servers.create_app(emby_db, jf_db)
    server = uvicorn.Server(uvicorn.Config(mock, port=PORT, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    os.environ.update({
        "DATA_DIR": str(tmp / "data"),
        "EMBY_URL": f"http://127.0.0.1:{PORT}/e/", "EMBY_API_KEY": mock_servers.EMBY_KEY,
        "JELLYFIN_URL": f"http://127.0.0.1:{PORT}/j", "JELLYFIN_API_KEY": mock_servers.JF_KEY,
    })
    from app import collector, db
    collector.PAGE = 150  # force la pagination
    db.init()
    asyncio.run(collector.sync_all())
    yield {"emby": emby_db, "jellyfin": jf_db, "mock": mock}
    server.should_exit = True


def _src(path, sql):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


def test_totals_match_sources(env):
    from app import stats
    d = stats.dashboard(0, "all", "")
    emby = _src(env["emby"], "SELECT SUM(PlayDuration - PauseDuration) FROM PlaybackActivity")
    jf = _src(env["jellyfin"], "SELECT SUM(PlayDuration) FROM PlaybackActivity")
    assert d["totals"]["emby"] == emby
    assert d["totals"]["jellyfin"] == jf
    assert d["totals"]["seconds"] == emby + jf
    assert d["totals"]["plays"] == 800
    assert stats.status()["servers"][0]["last_error"] is None


def test_same_name_users_are_merged(env):
    from app import stats
    names = [p["name"].lower() for p in stats.dashboard(0, "all", "")["people"]]
    assert names.count("yonel") == 1 and names.count("marie") == 1
    assert "lucas" in names and "luke" in names
    yonel = next(p for p in stats.dashboard(0, "all", "")["people"] if p["name"].lower() == "yonel")
    assert yonel["emby"] > 0 and yonel["jellyfin"] > 0


def test_alias_merges_accounts(env):
    from app import stats
    before = stats.dashboard(0, "all", "")["totals"]["people"]
    luke = next(u for u in stats.list_users() if u["name"] == "Luke")
    assert stats.set_alias(luke["server"], luke["user_id"], "Lucas")
    d = stats.dashboard(0, "all", "")
    assert d["totals"]["people"] == before - 1
    lucas = next(p for p in d["people"] if p["name"] == "Lucas")
    assert lucas["emby"] > 0 and lucas["jellyfin"] > 0


def test_filters_and_series(env):
    from app import stats
    only = stats.dashboard(0, "jellyfin", "")
    assert only["totals"]["emby"] == 0 and only["totals"]["plays"] == 400
    assert {s["name"] for s in only["series"]} <= set(mock_servers.SERIES)
    week = stats.dashboard(7, "all", "")
    assert len(week["timeline"]["points"]) == 7
    assert week["totals"]["plays"] < 800
    assert sum(map(sum, stats.dashboard(0, "all", "")["heatmap"])) == stats.dashboard(0, "all", "")["totals"]["seconds"]


def test_resync_is_idempotent_and_updates_running_plays(env):
    from app import collector, stats
    con = sqlite3.connect(env["jellyfin"])
    rid = con.execute("SELECT rowid FROM PlaybackActivity ORDER BY DateCreated DESC LIMIT 1").fetchone()[0]
    con.execute("UPDATE PlaybackActivity SET PlayDuration = PlayDuration + 1000 WHERE rowid = ?", (rid,))
    con.commit()
    con.close()
    before = stats.dashboard(0, "all", "")["totals"]
    asyncio.run(collector.sync_all())
    after = stats.dashboard(0, "all", "")["totals"]
    assert after["plays"] == before["plays"]
    assert after["jellyfin"] == before["jellyfin"] + 1000


def test_bad_key_is_reported(env):
    from app import collector, config, stats
    bad = config.Server("emby", "Emby", f"http://127.0.0.1:{PORT}/e", "mauvaise-cle")
    asyncio.run(collector.sync_server(bad))
    emby = next(s for s in stats.status()["servers"] if s["key"] == "emby")
    assert "clé API refusée" in emby["last_error"]
    assert emby["last_ok"] is not None  # la dernière synchro réussie reste connue


def test_empty_table_is_not_an_error(env):
    from app import collector, stats
    con = sqlite3.connect(env["jellyfin"])
    con.execute("DELETE FROM PlaybackActivity")
    con.commit()
    con.close()
    asyncio.run(collector.sync_all())
    jf = next(s for s in stats.status()["servers"] if s["key"] == "jellyfin")
    assert jf["last_error"] is None


def test_sql_error_is_reported(env):
    from app import collector, config
    import httpx

    async def run():
        async with httpx.AsyncClient() as client:
            await collector._custom_query(client, config.load_servers()[1], "SELECT * FROM TableAbsente")
    with pytest.raises(RuntimeError, match="requête refusée"):
        asyncio.run(run())


def test_emby_ips_are_normalized_and_classified(env):
    from app import stats
    d = stats.dashboard(0, "emby", "")
    ips = {i["ip"]: i for i in d["ips"]}
    # « 192.168.1.20 », « ::ffff:192.168.1.20 » et « 192.168.1.21:8096 » sont nettoyées.
    assert set(ips) == {"192.168.1.20", "192.168.1.21", "82.64.10.5", "2a01:e0a:1:2::10"}
    assert ips["192.168.1.20"]["scope"] == "local" and ips["82.64.10.5"]["scope"] == "remote"
    assert sum(i["seconds"] for i in d["ips"]) == d["totals"]["emby"]
    assert sum(i["plays"] for i in d["ips"]) == 400
    assert d["totals"]["ips"] == 4
    assert {n["name"] for n in d["networks"]} == {"Réseau local", "Distant"}
    assert sum(n["seconds"] for n in d["networks"]) == d["totals"]["seconds"]
    assert all(p["ips"] == p["local"] + p["remote"] for p in d["people_ips"])
    assert all(len(i["people"]) == len({x.lower() for x in i["people"]}) for i in d["ips"])


def test_jellyfin_ip_comes_from_active_session(env):
    from datetime import datetime
    from app import collector, stats
    user = mock_servers.USERS["jellyfin"][0][0]
    dashed = f"{user[:8]}-{user[8:12]}-{user[12:16]}-{user[16:20]}-{user[20:]}"
    item = "c0ffee00c0ffee00c0ffee00c0ffee00"
    con = sqlite3.connect(env["jellyfin"])
    con.execute("INSERT INTO PlaybackActivity VALUES (?,?,?,?,?,?,?,?,?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S.0000000"), dashed, item, "Movie", "Dune",
                 "DirectPlay", "Jellyfin Web", "Firefox", 600))
    con.commit()
    con.close()
    env["mock"].state.sessions["jellyfin"] = [
        {"UserId": user, "RemoteEndPoint": "90.12.34.56", "NowPlayingItem": {"Id": item}},
        {"UserId": user, "RemoteEndPoint": "10.0.0.9"},  # session sans lecture : ignorée
    ]
    asyncio.run(collector.poll_sessions())
    asyncio.run(collector.sync_all())
    d = stats.dashboard(1, "jellyfin", "")
    assert d["recent"][0]["ip"] == "90.12.34.56" and d["recent"][0]["ip_scope"] == "remote"
    assert [i["ip"] for i in d["ips"]] == ["90.12.34.56"]


def test_migration_backfills_ips(env):
    from app import collector, config, db, stats
    with db.connect() as con:
        con.execute("DROP VIEW v_plays")
        con.execute("ALTER TABLE plays DROP COLUMN ip_scope")
        con.execute("DROP INDEX idx_plays_ip")
        con.execute("ALTER TABLE plays DROP COLUMN ip")
    db.init()
    assert stats.dashboard(0, "emby", "")["totals"]["ips"] == 0
    asyncio.run(collector.sync_all())
    assert stats.dashboard(0, "emby", "")["totals"]["ips"] == 4
    assert not collector._full_resync(config.load_servers()[0])


def test_plays_filters_sort_and_pagination(env):
    from app import stats
    everything = stats.plays(0, "all", "")
    assert everything["total"] == stats.dashboard(0, "all", "")["totals"]["plays"]
    assert everything["pages"] == -(-everything["total"] // 25) and len(everything["rows"]) == 25

    # Pagination : les pages se suivent sans doublon ni trou.
    seen = []
    for page in range(1, 4):
        seen += stats.plays(0, "emby", "", page=page, per_page=150)["rows"]
    assert len(seen) == 400
    assert stats.plays(0, "emby", "", page=99, per_page=150)["page"] == 3

    # Liste de valeurs d'une colonne : les comptes couvrent toutes les lignes.
    titles = stats.column_values(0, "emby", "", {}, "title")
    assert sum(v["count"] for v in titles) == 400
    assert {v["label"] for v in titles} == set(mock_servers.MOVIES) | set(mock_servers.SERIES)

    # Inclure (valeurs cochées) et exclure (valeurs décochées) se complètent.
    inc = stats.plays(0, "emby", "", {"title": {"mode": "in", "values": ["dune", "dark"]}}, per_page=500)
    out = stats.plays(0, "emby", "", {"title": {"mode": "out", "values": ["dune", "dark"]}}, per_page=500)
    assert inc["total"] > 0 and inc["total"] + out["total"] == 400
    assert all(r["item_name"] == "Dune" or r["item_name"].startswith("Dark - ") for r in inc["rows"])
    assert not any(r["item_name"] == "Dune" or r["item_name"].startswith("Dark - ") for r in out["rows"])
    assert stats.plays(0, "emby", "", {"title": {"mode": "in", "values": []}})["total"] == 0

    # Plusieurs colonnes se combinent ; la liste d'une colonne tient compte des autres filtres.
    filters = {"person": {"mode": "in", "values": ["yonel"]}, "type": {"mode": "in", "values": ["movie"]},
               "scope": {"mode": "in", "values": ["remote"]}, "duration": {"mode": "out", "values": ["1"]}}
    combo = stats.plays(0, "emby", "", filters, per_page=500)
    assert combo["total"] > 0
    assert all(r["person"] == "Yonel" and r["item_type"] == "Movie" and r["ip_scope"] == "remote"
               and r["duration"] >= 300 for r in combo["rows"])
    assert combo["seconds"] == sum(r["duration"] for r in combo["rows"])
    ips = stats.column_values(0, "emby", "", filters, "ip")
    assert {v["value"] for v in ips} <= {"82.64.10.5", "2a01:e0a:1:2::10"}
    assert sum(v["count"] for v in ips) == combo["total"]
    # ... mais pas de son propre filtre, pour pouvoir recocher une valeur.
    assert len(stats.column_values(0, "emby", "", filters, "person")) == 3

    # Valeurs vides (Jellyfin sans adresse) proposées comme dans Excel.
    assert "" in {v["value"] for v in stats.column_values(0, "all", "", {}, "ip")}

    # Tri.
    asc = [r["duration"] for r in stats.plays(0, "emby", "", sort="duration", order="asc", per_page=500)["rows"]]
    assert asc == sorted(asc)
    desc = [r["date_created"] for r in stats.plays(0, "emby", "", per_page=500)["rows"]]
    assert desc == sorted(desc, reverse=True)
    assert stats.plays(0, "emby", "", sort="n'importe quoi; DROP TABLE plays")["total"] == 400
    assert stats.plays(0, "emby", "", {"inconnue; DROP": {"mode": "in", "values": ["x"]}})["total"] == 400


def test_http_api_and_cache_busting(env):
    from fastapi.testclient import TestClient
    from app import main
    with TestClient(main.app) as client:
        html = client.get("/")
        assert f"/static/app.js?v={main.ASSETS}" in html.text and f"/static/style.css?v={main.ASSETS}" in html.text
        assert html.headers["cache-control"] == "no-store"
        assert client.get(f"/static/app.js?v={main.ASSETS}").headers["cache-control"] == "no-cache"
        r = client.post("/api/plays", json={"days": 0, "source": "emby", "per_page": 10,
                                            "filters": {"method": {"mode": "in", "values": ["Transcodage"]}}})
        assert r.status_code == 200 and len(r.json()["rows"]) == 10
        assert all(x["method"] == "Transcodage" for x in r.json()["rows"])
        v = client.post("/api/plays/values", json={"days": 0, "source": "emby", "column": "method"})
        assert {x["value"] for x in v.json()} == {"Direct Play", "Direct Stream", "Transcodage"}
