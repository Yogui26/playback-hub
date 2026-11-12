"""Récupère les données des plugins Playback Reporting d'Emby et de Jellyfin."""
import asyncio
import ipaddress
import logging
import re
from datetime import datetime, timedelta

import httpx

from . import config, db
from .config import Server

log = logging.getLogger("playback-hub.collector")
PAGE = 2000
_lock = asyncio.Lock()
_EPISODE = re.compile(r"^(?P<series>.+?) - s\d+e\d+", re.IGNORECASE)


def _norm_id(value) -> str:
    return str(value or "").replace("-", "").lower()


def _norm_ip(value) -> str | None:
    """Nettoie une adresse : retire le port, les crochets et le préfixe IPv4-dans-IPv6."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("["):                      # [2001:db8::1]:8096
        text = text[1:].split("]")[0]
    elif text.count(":") == 1:                    # 192.168.1.5:8096
        text = text.split(":")[0]
    text = text.split("%")[0]
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ip)


def _ip_scope(ip: str | None) -> str | None:
    if not ip:
        return None
    return "remote" if ipaddress.ip_address(ip).is_global else "local"


def _to_int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _method(raw: str) -> str:
    low = (raw or "").lower()
    if low.startswith("transcod"):
        return "Transcodage"
    if low.startswith("directstream"):
        return "Direct Stream"
    if low.startswith("directplay"):
        return "Direct Play"
    return raw or "Inconnu"


def _series(item_type: str, item_name: str) -> str | None:
    if (item_type or "").lower() != "episode":
        return None
    m = _EPISODE.match(item_name or "")
    return m.group("series").strip() if m else (item_name or None)


async def _custom_query(client: httpx.AsyncClient, server: Server, sql: str) -> list[dict]:
    """Exécute une requête SQL via l'API du plugin et renvoie des dictionnaires."""
    r = await client.post(
        f"{server.api_base}/user_usage_stats/submit_custom_query",
        headers=server.headers,
        json={"CustomQueryString": sql, "ReplaceUserId": False},
    )
    if r.status_code == 404:
        raise RuntimeError("plugin Playback Reporting introuvable (404) : est-il installé ?")
    if r.status_code in (401, 403):
        raise RuntimeError(f"clé API refusée ({r.status_code})")
    r.raise_for_status()
    data = r.json()
    # Le plugin écrit « colums » (sic) ; on accepte aussi l'orthographe correcte.
    columns = data.get("colums") or data.get("columns") or []
    rows = data.get("results") or []
    message = str(data.get("message") or "")
    # Sans résultat, le plugin renvoie un message d'information (« Query executed, no data
    # returned ») et aucune colonne : ce n'est pas une erreur, la table est simplement vide.
    if not rows and "error" in message.lower():
        raise RuntimeError(f"requête refusée par le plugin : {message.replace('</br>', ' ')}")
    keys = [str(c).lower() for c in columns]
    return [dict(zip(keys, row)) for row in rows]


async def _sync_users(client: httpx.AsyncClient, server: Server) -> None:
    r = await client.get(f"{server.api_base}/Users", headers=server.headers)
    r.raise_for_status()
    users = [(server.key, _norm_id(u.get("Id")), u.get("Name")) for u in r.json()]
    with db.connect() as con:
        con.executemany(
            "INSERT INTO users(server, user_id, name) VALUES (?,?,?) "
            "ON CONFLICT(server, user_id) DO UPDATE SET name = excluded.name",
            users,
        )


def _known_max(server: Server) -> int:
    with db.connect() as con:
        row = con.execute("SELECT COALESCE(MAX(src_rowid), 0) FROM plays WHERE server = ?", (server.key,)).fetchone()
    return row[0]


def _full_resync(server: Server) -> bool:
    with db.connect() as con:
        return con.execute("SELECT 1 FROM meta WHERE key = ?", (f"full_resync:{server.key}",)).fetchone() is not None


async def poll_sessions() -> None:
    """Note l'adresse IP des lectures en cours (Jellyfin ne l'enregistre pas dans son plugin)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    seen = []
    async with httpx.AsyncClient(timeout=15, verify=config.VERIFY_TLS, follow_redirects=True) as client:
        for server in config.load_servers():
            try:
                r = await client.get(f"{server.api_base}/Sessions", headers=server.headers)
                r.raise_for_status()
                for session in r.json():
                    item = session.get("NowPlayingItem") or {}
                    ip = _norm_ip(session.get("RemoteEndPoint"))
                    if item.get("Id") and session.get("UserId") and ip:
                        seen.append((server.key, _norm_id(session["UserId"]), _norm_id(item["Id"]), ip, now, now))
            except Exception as e:
                log.debug("%s : sessions indisponibles (%s)", server.label, e)
    with db.connect() as con:
        con.executemany(
            "INSERT INTO session_ips(server, user_id, item_id, ip, first_seen, last_seen) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(server, user_id, item_id, ip) DO UPDATE SET last_seen = excluded.last_seen",
            seen,
        )
        con.execute("DELETE FROM session_ips WHERE last_seen < datetime('now', 'localtime', '-30 days')")


def fill_missing_ips() -> None:
    """Rattache aux lectures récentes sans adresse celle observée dans la session correspondante."""
    with db.connect() as con:
        rows = con.execute(
            "SELECT p.server, p.src_rowid, ("
            "  SELECT s.ip FROM session_ips s WHERE s.server = p.server AND s.user_id = p.user_id"
            "  AND s.item_id = p.item_id AND s.last_seen >= datetime(p.date_created, '-10 minutes')"
            "  AND s.first_seen <= datetime(p.date_created, '+' || (p.duration + 1800) || ' seconds')"
            "  ORDER BY s.last_seen DESC LIMIT 1) AS ip "
            "FROM plays p WHERE p.ip IS NULL AND p.date_created >= datetime('now', 'localtime', '-7 days')"
        ).fetchall()
        con.executemany(
            "UPDATE plays SET ip = ?, ip_scope = ? WHERE server = ? AND src_rowid = ?",
            [(r["ip"], _ip_scope(r["ip"]), r["server"], r["src_rowid"]) for r in rows if r["ip"]],
        )


async def poll_sessions_forever() -> None:
    while True:
        try:
            await poll_sessions()
        except Exception:
            log.exception("erreur inattendue pendant la lecture des sessions")
        await asyncio.sleep(config.SESSION_POLL_SECONDS)


def _store(server: Server, rows: list[dict]) -> None:
    out = []
    for r in rows:
        item_type = r.get("itemtype") or ""
        item_name = r.get("itemname") or ""
        raw_method = r.get("playbackmethod") or ""
        duration = max(0, _to_int(r.get("playduration")) - _to_int(r.get("pauseduration")))
        ip = _norm_ip(r.get("remoteaddress") or r.get("remoteendpoint") or r.get("ipaddress"))
        out.append((
            server.key, _to_int(r.get("rid")), str(r.get("datecreated") or "")[:19],
            _norm_id(r.get("userid")), _norm_id(r.get("itemid")), item_type, item_name,
            _series(item_type, item_name), _method(raw_method), raw_method,
            r.get("clientname") or "Inconnu", r.get("devicename") or "Inconnu", duration, ip, _ip_scope(ip),
        ))
    with db.connect() as con:
        con.executemany(
            "INSERT INTO plays(server, src_rowid, date_created, user_id, item_id, item_type, item_name,"
            " series, method, method_raw, client, device, duration, ip, ip_scope)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(server, src_rowid) DO UPDATE SET date_created=excluded.date_created,"
            " user_id=excluded.user_id, item_id=excluded.item_id, item_type=excluded.item_type,"
            " item_name=excluded.item_name, series=excluded.series, method=excluded.method,"
            " method_raw=excluded.method_raw, client=excluded.client, device=excluded.device,"
            " duration=excluded.duration, ip=COALESCE(excluded.ip, plays.ip),"
            " ip_scope=COALESCE(excluded.ip_scope, plays.ip_scope)",
            out,
        )


async def sync_server(server: Server) -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fetched = 0
    try:
        async with httpx.AsyncClient(timeout=60, verify=config.VERIFY_TLS, follow_redirects=True) as client:
            try:
                await _sync_users(client, server)
            except Exception as e:  # les stats restent utilisables sans les noms
                log.warning("%s : liste des utilisateurs indisponible (%s)", server.label, e)
            # Nouvelles lignes + lignes récentes, dont la durée évolue encore pendant la lecture.
            full = _full_resync(server)
            known = 0 if full else _known_max(server)
            recent = (datetime.now() - timedelta(hours=config.RESYNC_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
            cursor = 0
            while True:
                rows = await _custom_query(
                    client, server,
                    f"SELECT rowid AS rid, * FROM PlaybackActivity WHERE rowid > {cursor} "
                    f"AND (rowid > {known} OR DateCreated >= '{recent}') ORDER BY rowid LIMIT {PAGE}",
                )
                if not rows:
                    break
                _store(server, rows)
                fetched += len(rows)
                cursor = max(_to_int(r.get("rid")) for r in rows)
                if len(rows) < PAGE:
                    break
        if full:
            with db.connect() as con:
                con.execute("DELETE FROM meta WHERE key = ?", (f"full_resync:{server.key}",))
        fill_missing_ips()
        error = None
        log.info("%s : %d lignes synchronisées", server.label, fetched)
    except Exception as e:
        error = str(e) or e.__class__.__name__
        log.error("%s : échec de la synchro : %s", server.label, error)
    with db.connect() as con:
        con.execute(
            "INSERT INTO sync_state(server, last_try, last_ok, last_error, fetched) VALUES (?,?,?,?,?) "
            "ON CONFLICT(server) DO UPDATE SET last_try=excluded.last_try, last_error=excluded.last_error,"
            " fetched=excluded.fetched, last_ok=COALESCE(excluded.last_ok, sync_state.last_ok)",
            (server.key, now, None if error else now, error, fetched),
        )
    return fetched


async def sync_all() -> None:
    from . import live  # import tardif : live dépend de ce module
    async with _lock:
        await asyncio.gather(*(sync_server(s) for s in config.load_servers()))
        try:
            await live.sync_downloads_all()
        except Exception:
            log.exception("erreur inattendue pendant la lecture des téléchargements")


async def run_forever() -> None:
    while True:
        try:
            await sync_all()
        except Exception:
            log.exception("erreur inattendue pendant la synchro")
        await asyncio.sleep(config.SYNC_INTERVAL_MINUTES * 60)
