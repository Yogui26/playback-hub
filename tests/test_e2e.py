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
    server = uvicorn.Server(uvicorn.Config(mock_servers.create_app(emby_db, jf_db), port=PORT, log_level="error"))
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
    yield {"emby": emby_db, "jellyfin": jf_db}
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
