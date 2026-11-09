"""Calcul des statistiques affichées par le dashboard."""
from datetime import date, datetime, timedelta

from . import config, db

SOURCES = ("emby", "jellyfin")
_SPLIT = (
    "SUM(duration) AS seconds, COUNT(*) AS plays, "
    "SUM(CASE WHEN server='emby' THEN duration ELSE 0 END) AS emby, "
    "SUM(CASE WHEN server='jellyfin' THEN duration ELSE 0 END) AS jellyfin"
)


def _where(days: int, source: str, person: str) -> tuple[str, list]:
    clauses, params = ["duration >= ?"], [config.MIN_PLAY_SECONDS]
    if days > 0:
        clauses.append("date_created >= ?")
        params.append((date.today() - timedelta(days=days - 1)).isoformat())
    if source in SOURCES:
        clauses.append("server = ?")
        params.append(source)
    if person:
        clauses.append("lower(person) = lower(?)")
        params.append(person)
    return " AND ".join(clauses), params


def _breakdown(con, where, params, expr, limit, extra=""):
    sql = (
        f"SELECT MIN({expr}) AS name, {_SPLIT} FROM v_plays WHERE {where} {extra} "
        f"AND {expr} IS NOT NULL AND {expr} <> '' GROUP BY lower({expr}) "
        f"ORDER BY seconds DESC LIMIT {int(limit)}"
    )
    return [dict(r) for r in con.execute(sql, params)]


def _timeline(con, where, params, days):
    monthly = days == 0 or days > 92
    bucket = "substr(date_created, 1, 7)" if monthly else "substr(date_created, 1, 10)"
    rows = {
        r["bucket"]: dict(r)
        for r in con.execute(
            f"SELECT {bucket} AS bucket, {_SPLIT} FROM v_plays WHERE {where} GROUP BY bucket ORDER BY bucket",
            params,
        )
    }
    if monthly:
        return {"unit": "month", "points": list(rows.values())}
    start = date.today() - timedelta(days=days - 1)
    points = []
    for i in range(days):
        key = (start + timedelta(days=i)).isoformat()
        points.append(rows.get(key) or {"bucket": key, "seconds": 0, "plays": 0, "emby": 0, "jellyfin": 0})
    return {"unit": "day", "points": points}


def _heatmap(con, where, params):
    grid = [[0] * 24 for _ in range(7)]  # lundi = 0
    for r in con.execute(
        "SELECT CAST(strftime('%w', date_created) AS INTEGER) AS dow, "
        "CAST(strftime('%H', date_created) AS INTEGER) AS hour, SUM(duration) AS seconds "
        f"FROM v_plays WHERE {where} GROUP BY dow, hour",
        params,
    ):
        if r["dow"] is not None and r["hour"] is not None:
            grid[(r["dow"] + 6) % 7][r["hour"]] = r["seconds"]
    return grid


def dashboard(days: int, source: str, person: str) -> dict:
    where, params = _where(days, source, person)
    with db.connect() as con:
        totals = dict(con.execute(
            f"SELECT COALESCE(SUM(duration),0) AS seconds, COUNT(*) AS plays, "
            f"COUNT(DISTINCT lower(person)) AS people, "
            f"COUNT(DISTINCT lower(COALESCE(series, item_name))) AS titles, "
            f"COALESCE(SUM(CASE WHEN server='emby' THEN duration ELSE 0 END),0) AS emby, "
            f"COALESCE(SUM(CASE WHEN server='jellyfin' THEN duration ELSE 0 END),0) AS jellyfin, "
            f"COALESCE(SUM(server='emby'),0) AS emby_plays, "
            f"COALESCE(SUM(server='jellyfin'),0) AS jellyfin_plays "
            f"FROM v_plays WHERE {where}", params).fetchone())
        recent = [dict(r) for r in con.execute(
            f"SELECT date_created, server, person, item_name, item_type, method, client, device, duration "
            f"FROM v_plays WHERE {where} ORDER BY date_created DESC LIMIT 40", params)]
        # La liste du filtre ne dépend pas de la personne sélectionnée.
        w_all, p_all = _where(0, "", "")
        people_filter = [r[0] for r in con.execute(
            f"SELECT MIN(person) FROM v_plays WHERE {w_all} GROUP BY lower(person) ORDER BY 1 COLLATE NOCASE", p_all)]
        return {
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "totals": totals,
            "timeline": _timeline(con, where, params, days),
            "people": _breakdown(con, where, params, "person", 15),
            "movies": _breakdown(con, where, params, "item_name", 10, "AND lower(item_type) = 'movie'"),
            "series": _breakdown(con, where, params, "series", 10, "AND lower(item_type) = 'episode'"),
            "clients": _breakdown(con, where, params, "client", 8),
            "devices": _breakdown(con, where, params, "device", 8),
            "methods": _breakdown(con, where, params, "method", 6),
            "types": _breakdown(con, where, params, "item_type", 6),
            "heatmap": _heatmap(con, where, params),
            "recent": recent,
            "people_filter": people_filter,
        }


def status() -> dict:
    with db.connect() as con:
        state = {r["server"]: dict(r) for r in con.execute("SELECT * FROM sync_state")}
        counts = {r[0]: r[1] for r in con.execute("SELECT server, COUNT(*) FROM plays GROUP BY server")}
    servers = []
    for s in config.load_servers():
        st = state.get(s.key, {})
        servers.append({
            "key": s.key, "label": s.label, "url": s.url, "rows": counts.get(s.key, 0),
            "last_ok": st.get("last_ok"), "last_try": st.get("last_try"), "last_error": st.get("last_error"),
        })
    return {"servers": servers, "interval_minutes": config.SYNC_INTERVAL_MINUTES}


def list_users() -> list[dict]:
    with db.connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT u.server, u.user_id, u.name, COALESCE(u.alias, '') AS alias, "
            "(SELECT COUNT(*) FROM plays p WHERE p.server = u.server AND p.user_id = u.user_id) AS plays "
            "FROM users u ORDER BY lower(COALESCE(NULLIF(u.alias,''), u.name)), u.server")]


def set_alias(server: str, user_id: str, alias: str) -> bool:
    with db.connect() as con:
        cur = con.execute("UPDATE users SET alias = ? WHERE server = ? AND user_id = ?",
                          (alias.strip() or None, server, user_id))
        return cur.rowcount > 0
