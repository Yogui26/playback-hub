"""Lectures en cours et téléchargements.

- Lectures en cours : sessions actives des deux serveurs (/Sessions), interrogées à la demande.
- Téléchargements : journal d'activité des serveurs (entrées de type « download »), plus les
  tâches de synchronisation hors ligne d'Emby (/Sync/Jobs) quand le serveur les expose.
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import config, db
from .collector import _ip_scope, _norm_id, _norm_ip
from .config import Server

log = logging.getLogger("playback-hub.live")
TICKS = 10_000_000
ACTIVITY_PAGE = 500
ACTIVITY_MAX_PAGES = 20
_cache = {"at": 0.0, "data": None}


# --------------------------------------------------------------------------- utilitaires

def _local(iso: str | None) -> str | None:
    """Date ISO d'un serveur (UTC) -> 'YYYY-MM-DD HH:MM:SS' en heure locale, comme les lectures."""
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _display_name(item: dict) -> tuple[str, str | None]:
    """(nom affiché, série) au même format que le plugin : « Série - s01e02 - Épisode »."""
    name = item.get("Name") or ""
    if (item.get("Type") or "").lower() == "episode" and item.get("SeriesName"):
        season, episode = item.get("ParentIndexNumber"), item.get("IndexNumber")
        if season is not None and episode is not None:
            return f"{item['SeriesName']} - s{int(season):02d}e{int(episode):02d} - {name}", item["SeriesName"]
        return f"{item['SeriesName']} - {name}", item["SeriesName"]
    return name, None


def _people() -> dict:
    with db.connect() as con:
        return {(r["server"], r["user_id"]): (r["alias"] or "").strip() or (r["name"] or "").strip()
                for r in con.execute("SELECT server, user_id, name, alias FROM users")}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20, verify=config.VERIFY_TLS, follow_redirects=True)


# --------------------------------------------------------------------------- lectures en cours

def _resolution(item: dict) -> str | None:
    for stream in item.get("MediaStreams") or []:
        if stream.get("Type") == "Video":
            width, height = stream.get("Width") or 0, stream.get("Height") or 0
            if width >= 3800 or height >= 2000:
                return "4K"
            if width >= 1900 or height >= 1000:
                return "1080p"
            if width >= 1260 or height >= 700:
                return "720p"
            return f"{height}p" if height else None
    return None


def _session(server: Server, s: dict, people: dict) -> dict | None:
    item = s.get("NowPlayingItem")
    if not item:
        return None
    state, trans = s.get("PlayState") or {}, s.get("TranscodingInfo") or {}
    raw = (state.get("PlayMethod") or "").lower()
    video_direct, audio_direct = trans.get("IsVideoDirect"), trans.get("IsAudioDirect")
    if raw == "transcode" or (not raw and trans):
        method = "Direct Stream" if (video_direct and audio_direct) else "Transcodage"
    elif raw == "directstream":
        method = "Direct Stream"
    else:
        method = "Direct Play"
    detail = []
    if method == "Transcodage":
        if video_direct is False and trans.get("VideoCodec"):
            detail.append(f"vidéo → {trans['VideoCodec']}")
        if audio_direct is False and trans.get("AudioCodec"):
            detail.append(f"audio → {trans['AudioCodec']}")
        reasons = trans.get("TranscodeReasons")
        if isinstance(reasons, list) and reasons:
            detail.append(", ".join(str(r) for r in reasons[:3]))
        elif isinstance(reasons, str) and reasons and reasons != "0":
            detail.append(reasons)
    kind = (item.get("Type") or "").lower()
    if kind == "episode":
        title = item.get("SeriesName") or item.get("Name") or ""
        season, episode = item.get("ParentIndexNumber"), item.get("IndexNumber")
        code = f"S{int(season):02d}E{int(episode):02d} · " if season is not None and episode is not None else ""
        subtitle = f"{code}{item.get('Name') or ''}"
    elif kind == "audio":
        title = item.get("Name") or ""
        subtitle = " · ".join(x for x in [", ".join(item.get("Artists") or []), item.get("Album")] if x)
    else:
        title = item.get("Name") or ""
        subtitle = str(item.get("ProductionYear") or "")
    user_id = _norm_id(s.get("UserId"))
    ip = _norm_ip(s.get("RemoteEndPoint"))
    bitrate = trans.get("Bitrate") or item.get("Bitrate")
    return {
        "server": server.key,
        "person": people.get((server.key, user_id)) or s.get("UserName") or "Inconnu",
        "title": title, "subtitle": subtitle, "type": item.get("Type") or "",
        "device": s.get("DeviceName") or "Inconnu", "client": s.get("Client") or "Inconnu",
        "ip": ip, "ip_scope": _ip_scope(ip),
        "method": method, "detail": " · ".join(detail),
        "paused": bool(state.get("IsPaused")),
        "position": int((state.get("PositionTicks") or 0) / TICKS),
        "runtime": int((item.get("RunTimeTicks") or 0) / TICKS),
        "bitrate": int(bitrate) if bitrate else None,
        "resolution": _resolution(item),
    }


async def _server_sessions(client: httpx.AsyncClient, server: Server, people: dict) -> tuple[list, str | None]:
    try:
        r = await client.get(f"{server.api_base}/Sessions", headers=server.headers, timeout=8)
        r.raise_for_status()
        rows = [_session(server, s, people) for s in r.json()]
        return [x for x in rows if x], None
    except Exception as e:
        return [], f"{server.label} : {str(e) or e.__class__.__name__}"


async def now_playing() -> dict:
    """Lectures en cours sur tous les serveurs (réponse gardée 5 secondes)."""
    if _cache["data"] is not None and time.monotonic() - _cache["at"] < 5:
        return _cache["data"]
    people = _people()
    async with _client() as client:
        results = await asyncio.gather(*(_server_sessions(client, s, people) for s in config.load_servers()))
    sessions = [row for rows, _ in results for row in rows]
    sessions.sort(key=lambda x: (x["paused"], x["person"].lower(), x["title"].lower()))
    data = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sessions": sessions,
        "errors": [err for _, err in results if err],
    }
    _cache.update(at=time.monotonic(), data=data)
    return data


# --------------------------------------------------------------------------- téléchargements

def _meta_get(key: str):
    with db.connect() as con:
        row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(key: str, value: str) -> None:
    with db.connect() as con:
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


def is_download(entry_type: str) -> bool:
    low = (entry_type or "").lower()
    return "download" in low and "subtitle" not in low


async def _activity(client: httpx.AsyncClient, server: Server, since: str | None) -> list[dict]:
    entries, start = [], 0
    for _ in range(ACTIVITY_MAX_PAGES):
        params = {"StartIndex": start, "Limit": ACTIVITY_PAGE}
        if since:
            params["MinDate"] = since
        r = await client.get(f"{server.api_base}/System/ActivityLog/Entries", headers=server.headers, params=params)
        r.raise_for_status()
        items = r.json().get("Items") or []
        entries += items
        if len(items) < ACTIVITY_PAGE:
            break
        start += ACTIVITY_PAGE
    return entries


async def _items(client: httpx.AsyncClient, server: Server, ids: list[str]) -> dict:
    """Nom, type et taille des médias téléchargés. Sans réponse, on garde le texte du journal."""
    found = {}
    for i in range(0, len(ids), 40):
        try:
            r = await client.get(f"{server.api_base}/Items", headers=server.headers,
                                 params={"Ids": ",".join(ids[i:i + 40]), "Fields": "MediaSources"})
            r.raise_for_status()
            for item in r.json().get("Items") or []:
                name, series = _display_name(item)
                sizes = [m.get("Size") for m in item.get("MediaSources") or [] if m.get("Size")]
                found[_norm_id(item.get("Id"))] = (name, item.get("Type"), series, sizes[0] if sizes else None)
        except Exception as e:
            log.debug("%s : détails des médias indisponibles (%s)", server.label, e)
    return found


_UPSERT = (
    "INSERT INTO downloads(server, kind, key, date_created, user_id, item_id, item_name, item_type, series,"
    " detail, status, size, items) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
    " ON CONFLICT(server, kind, key) DO UPDATE SET date_created = MIN(downloads.date_created, excluded.date_created),"
    " item_name = COALESCE(excluded.item_name, downloads.item_name),"
    " item_type = COALESCE(excluded.item_type, downloads.item_type),"
    " series = COALESCE(excluded.series, downloads.series), detail = COALESCE(excluded.detail, downloads.detail),"
    " status = COALESCE(excluded.status, downloads.status), size = COALESCE(excluded.size, downloads.size),"
    " items = excluded.items"
)


async def _sync_activity(client: httpx.AsyncClient, server: Server, state: dict) -> None:
    since = _meta_get(f"activity_since:{server.key}")
    started = datetime.now(timezone.utc)
    entries = await _activity(client, server, since)
    types = state.setdefault("types", {})
    seen_ids = set(state.get("recent_ids") or [])
    downloads = []
    for e in entries:
        entry_id = str(e.get("Id"))
        if entry_id not in seen_ids:                      # la fenêtre relue chevauche la précédente
            types[e.get("Type") or "?"] = types.get(e.get("Type") or "?", 0) + 1
        if is_download(e.get("Type")) and e.get("UserId"):
            downloads.append(e)
    state["recent_ids"] = [str(e.get("Id")) for e in entries][:5000]
    wanted = sorted({_norm_id(e.get("ItemId")) for e in downloads if e.get("ItemId")})
    info = await _items(client, server, wanted) if wanted else {}
    rows = []
    for e in downloads:
        date = _local(e.get("Date"))
        if not date:
            continue
        user_id, item_id = _norm_id(e.get("UserId")), _norm_id(e.get("ItemId"))
        name, item_type, series, size = info.get(item_id, (None, None, None, None))
        # Certaines applis génèrent des dizaines d'entrées pour un seul téléchargement :
        # on en garde une par utilisateur, média et jour.
        key = f"{user_id}:{item_id or e.get('Id')}:{date[:10]}"
        rows.append((server.key, "download", key, date, user_id, item_id, name or e.get("Name"), item_type,
                     series, e.get("ShortOverview") or None, None, size, 1))
    with db.connect() as con:
        con.executemany(_UPSERT, rows)
    # Prochaine lecture : à partir de la veille, pour ne rien manquer.
    _meta_set(f"activity_since:{server.key}", (started - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))


async def _sync_jobs(client: httpx.AsyncClient, server: Server) -> None:
    """Téléchargements hors ligne des applis Emby (fonction « Télécharger » d'Emby Premiere)."""
    r = await client.get(f"{server.api_base}/Sync/Jobs", headers=server.headers)
    r.raise_for_status()
    rows = []
    for job in r.json().get("Items") or []:
        date = _local(job.get("DateCreated"))
        if not date or job.get("Id") is None:
            continue
        requested = job.get("RequestedItemIds") or []
        rows.append((server.key, "sync", str(job["Id"]), date, _norm_id(job.get("UserId")),
                     _norm_id(requested[0]) if requested else None, job.get("Name") or job.get("ParentName"),
                     job.get("Category"), None, job.get("TargetName"), job.get("Status"), None,
                     int(job.get("ItemCount") or 1)))
    with db.connect() as con:
        con.executemany(_UPSERT, rows)


async def sync_downloads(server: Server) -> None:
    key = f"downloads_state:{server.key}"
    state = json.loads(_meta_get(key) or "{}")
    async with _client() as client:
        try:
            await _sync_activity(client, server, state)
            state["activity"] = "ok"
        except Exception as e:
            state["activity"] = str(e) or e.__class__.__name__
            log.warning("%s : journal d'activité indisponible (%s)", server.label, state["activity"])
        if server.key == "emby":
            try:
                await _sync_jobs(client, server)
                state["sync"] = "ok"
            except Exception as e:
                state["sync"] = str(e) or e.__class__.__name__
                log.info("%s : tâches de téléchargement hors ligne indisponibles (%s)", server.label, state["sync"])
    state["checked"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _meta_set(key, json.dumps(state))


async def sync_downloads_all() -> None:
    await asyncio.gather(*(sync_downloads(s) for s in config.load_servers()))


def downloads_state() -> list[dict]:
    out = []
    for s in config.load_servers():
        state = json.loads(_meta_get(f"downloads_state:{s.key}") or "{}")
        types = sorted((state.get("types") or {}).items(), key=lambda kv: -kv[1])
        out.append({"key": s.key, "label": s.label, "activity": state.get("activity"), "sync": state.get("sync"),
                    "checked": state.get("checked"), "types": [{"type": t, "count": n} for t, n in types[:40]]})
    return out
