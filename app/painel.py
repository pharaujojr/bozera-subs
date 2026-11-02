"""Painel: cobertura de legendas, sincronizações, sistema e logs."""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Optional

import redis
from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

import config

router = APIRouter()
r = redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)

LIBRARY_DB = os.environ.get("LIBRARY_DB", "/jellyfin_db/jellyfin.db")
DATA_DIR = Path(os.environ.get("WORKER_DATA_DIR", "/data/worker"))

LOG_KEY = "painel:logs"
LOG_KEEP = 5000
METRICS_KEY = "painel:metrics"
METRICS_KEEP = 1440  # uma amostra por minuto, 24 horas
LEVELS = {"INFO": 20, "WARNING": 30, "ERROR": 40}
WORKER_STALE_SECONDS = 1500  # o ciclo roda a cada 10 min

PT_TAG = re.compile(r"(^|[._\- ])(pob|por|pt|pt[-_]?br|portug\w*|brazil\w*|brasil\w*)([._\- ]|$)", re.I)
EXTRAS_FOLDERS = {
    "featurettes", "extras", "behind the scenes", "deleted scenes", "interviews",
    "scenes", "shorts", "trailers", "specials", "bonus", "samples", "other",
}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
CREATED_BY_WORKER = {"ok", "ok_sem_sync"}

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def event(level: str, msg: str, **extra) -> None:
    """Grava no stdout e na lista do Redis que a aba Logs lê."""
    entry = {"ts": time.time(), "level": level, "source": "sync", "msg": msg, **extra}
    print(f"{level} {msg}", flush=True)
    try:
        r.lpush(LOG_KEY, json.dumps(entry, ensure_ascii=False))
        r.ltrim(LOG_KEY, 0, LOG_KEEP - 1)
    except redis.RedisError as exc:
        print(f"ERROR log não gravado no Redis: {type(exc).__name__}", flush=True)


def require_user(token: Optional[str]) -> str:
    user = token and r.get(f"token:{token}")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def _classify(streams: list[dict], stem: str, entry: dict, media_path: Path) -> tuple[str, Optional[float]]:
    portuguese = []
    for s in streams:
        name = Path(s["path"] or "").name.lower()
        if name.startswith(stem.lower()):
            name = name[len(stem):]
        if PT_TAG.search(s["language"] or "") or PT_TAG.search(s["title"] or "") or PT_TAG.search(name):
            portuguese.append(s)
    status = entry.get("status")
    if not portuguese:
        if status in CREATED_BY_WORKER or status == "ja_tinha" or _pt_on_disk(media_path, stem):
            return "fora_jellyfin", None
        return "sem_legenda", None
    added = max(filter(None, (_mtime(s["path"]) for s in portuguese if s["external"])), default=None)
    if status in CREATED_BY_WORKER:
        return "automacao", added
    if any(s["external"] for s in portuguese):
        return "existente", added
    return "embutida", added


def _stream_label(stream: dict) -> str:
    """O Jellyfin grava as legendas .pob que baixa só com o título, sem idioma."""
    tag = stream["language"] or stream["title"] or ""
    if PT_TAG.search(tag):
        return "português"
    return tag.lower() or "sem idioma"


def _mtime(host_path: Optional[str]) -> Optional[float]:
    if not host_path:
        return None
    path = config.from_db_path(host_path) or host_path
    if path.startswith("%MetadataPath%"):
        path = "/jellyfin_metadata" + path[len("%MetadataPath%"):]
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _pt_on_disk(video: Path, stem: str) -> bool:
    try:
        for f in video.parent.glob(f"{glob.escape(stem)}*"):
            if f.suffix.lower() in SUBTITLE_EXTENSIONS and PT_TAG.search(f.name.lower()[len(stem):]):
                return True
    except OSError:
        pass
    return False


def _read_state() -> dict:
    path = DATA_DIR / "state.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"videos": {}}


def _read_library() -> list[dict]:
    # As gravações recentes do Jellyfin ficam no -wal, que immutable=1 ignora.
    # O banco está em volume somente leitura, então a leitura é feita numa cópia.
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "jellyfin.db"
        shutil.copy(LIBRARY_DB, copy)
        if os.path.exists(LIBRARY_DB + "-wal"):
            shutil.copy(LIBRARY_DB + "-wal", str(copy) + "-wal")
        conn = sqlite3.connect(copy, timeout=10)
        try:
            items = conn.execute(
                "SELECT Id, Path, Name, ProductionYear FROM BaseItems "
                "WHERE Type = 'MediaBrowser.Controller.Entities.Movies.Movie'"
            ).fetchall()
            by_item: dict[str, list[dict]] = {}
            for item, language, title, path, external in conn.execute(
                "SELECT ItemId, Language, Title, Path, IsExternal FROM MediaStreamInfos WHERE StreamType = 2"
            ):
                by_item.setdefault(item, []).append(
                    {"language": language, "title": title, "path": path, "external": bool(external)}
                )
        finally:
            conn.close()

    videos = _read_state().get("videos", {})
    roots = {lib["container"].rstrip("/") for lib in config.libraries("movies")}
    movies = []
    for item, path, name, year in items:
        container_path = config.from_db_path(path or "")
        root = next((root for root in roots if container_path and container_path.startswith(root + "/")), None)
        if not root:
            continue
        media_path = Path(container_path)
        relative = media_path.relative_to(root)
        if any(part.lower() in EXTRAS_FOLDERS for part in relative.parts[:-1]):
            continue
        entry = videos.get(container_path, {})
        streams = by_item.get(item, [])
        category, added = _classify(streams, relative.stem, entry, media_path)
        movies.append({
            "path": container_path,
            "file": relative.name,
            "title": name or relative.stem,
            "year": year,
            "category": category,
            "added_at": added,
            "sem_sync": entry.get("status") == "ok_sem_sync",
            "next_try": entry.get("next_try"),
            "subs": sorted({_stream_label(s) for s in streams}),
        })
    movies.sort(key=lambda m: m["title"].lower())
    return movies


def _cached(key: str, ttl: float, build):
    with _cache_lock:
        cached = _cache.get(key)
        if cached and time.time() - cached[0] < ttl:
            return cached[1]
        value = build()
        _cache[key] = (time.time(), value)
        return value


def _read_temperature() -> dict:
    for hwmon in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if Path(hwmon, "name").read_text().strip() != "coretemp":
                continue
            for label in glob.glob(f"{hwmon}/temp*_label"):
                if Path(label).read_text().startswith("Package"):
                    base = label[: -len("_label")]
                    read = lambda suffix: int(Path(base + suffix).read_text()) / 1000
                    return {"now": read("_input"), "high": read("_max"), "crit": read("_crit")}
        except OSError:
            continue
    return {}


def _read_memory() -> dict:
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            values[key] = int(rest.split()[0]) * 1024
    return {
        "ram_total": values["MemTotal"],
        "ram_used": values["MemTotal"] - values["MemAvailable"],
        "swap_total": values["SwapTotal"],
        "swap_used": values["SwapTotal"] - values["SwapFree"],
    }


def _read_system() -> dict:
    roots = config.libraries("movies")
    disk = shutil.disk_usage(roots[0]["container"] if roots else "/")
    return {
        "temp": _read_temperature(),
        "memory": _read_memory(),
        "load": float(Path("/proc/loadavg").read_text().split()[0]),
        "cpus": os.cpu_count(),
        "disk": {"total": disk.total, "used": disk.used},
    }


def _sampler() -> None:
    while True:
        try:
            memory = _read_memory()
            sample = {
                "ts": int(time.time()),
                "temp": _read_temperature().get("now"),
                "ram": round(100 * memory["ram_used"] / memory["ram_total"], 1),
            }
            r.lpush(METRICS_KEY, json.dumps(sample))
            r.ltrim(METRICS_KEY, 0, METRICS_KEEP - 1)
        except Exception as exc:
            print(f"ERROR amostra do sistema falhou: {type(exc).__name__}", flush=True)
        time.sleep(60)


def start_sampler() -> None:
    threading.Thread(target=_sampler, daemon=True, name="painel-sampler").start()


def _read_jobs(titles: dict[str, str]) -> list[dict]:
    jobs = []
    for key in r.scan_iter("job:*"):
        raw = r.get(key)
        if not raw:
            continue
        job = json.loads(raw)
        video = job.get("video") or ""
        jobs.append({
            "id": key.split(":", 1)[1],
            "type": job.get("type") or "sync",
            "status": job.get("status"),
            "progress": job.get("progress", 0),
            "origin": job.get("origin"),
            "title": titles.get(video) or Path(video).name,
            "created_at": job.get("created_at"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "error": job.get("error"),
        })
    jobs.sort(key=lambda j: j.get("created_at") or 0, reverse=True)
    return jobs


def _worker_health(state: dict) -> dict:
    stamps = []
    for path in (DATA_DIR / "state.json", DATA_DIR / "logs" / "worker.jsonl"):
        try:
            stamps.append(path.stat().st_mtime)
        except OSError:
            pass
    last = max(stamps, default=None)
    return {
        "last_activity": last,
        "online": bool(last and time.time() - last < WORKER_STALE_SECONDS),
        "today": state.get("count", 0) if state.get("day") == time.strftime("%Y-%m-%d") else 0,
        "max_per_day": config.load_settings()["max_per_day"],
    }


def _tail_jsonl(path: Path, max_bytes: int = 2_000_000) -> list[dict]:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = raw.split("\n")
    if size > max_bytes:
        lines = lines[1:]
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def _read_logs() -> list[dict]:
    entries = []
    for path in sorted((DATA_DIR / "logs").glob("*.jsonl")):
        entries += _tail_jsonl(path)
    for raw in r.lrange(LOG_KEY, 0, LOG_KEEP - 1):
        try:
            entries.append(json.loads(raw))
        except ValueError:
            continue
    entries.sort(key=lambda e: e.get("ts", 0))
    return entries


def _filter_logs(source: str, level: str, q: str, limit: int) -> tuple[list[dict], int]:
    minimum = LEVELS.get(level, 0)
    needle = q.strip().lower()
    matches = [
        e for e in _read_logs()
        if (not source or e.get("source") == source)
        and LEVELS.get(e.get("level"), 20) >= minimum
        and (not needle or needle in e.get("msg", "").lower() or needle in (e.get("video") or "").lower())
    ]
    return matches[-limit:], len(matches)


@router.get("/painel", response_class=HTMLResponse)
def painel_page(token: str = Cookie(None)):
    if not token or not r.get(f"token:{token}"):
        return RedirectResponse(url="/login", status_code=302)
    return (Path(__file__).parent / "templates" / "painel.html").read_text()


@router.get("/api/painel")
def painel_data(token: str = Cookie(None)):
    require_user(token)
    movies = _cached("library", 20, _read_library)
    state = _read_state()
    video_titles = {m["path"]: m["title"] for m in movies}
    counts = Counter(m["category"] for m in movies)
    covered = sum(counts[c] for c in ("automacao", "existente", "embutida"))
    settings = config.load_settings()
    ready = sum(config.provider_ready(name, settings) for name in config.PROVIDERS)
    logs_24h = [e for e in _cached("logs", 10, _read_logs) if e.get("ts", 0) > time.time() - 86400]
    return {
        "now": time.time(),
        "summary": {"total": len(movies), "covered": covered, "counts": counts},
        "movies": movies,
        "providers": {"ready": ready, "total": len(config.PROVIDERS)},
        "jobs": _read_jobs(video_titles),
        "worker": _worker_health(state),
        "system": _cached("system", 5, _read_system),
        "log_counts": {
            "errors": sum(e.get("level") == "ERROR" for e in logs_24h),
            "warnings": sum(e.get("level") == "WARNING" for e in logs_24h),
        },
    }


@router.get("/api/painel/metrics")
def painel_metrics(token: str = Cookie(None)):
    require_user(token)
    samples = [json.loads(raw) for raw in r.lrange(METRICS_KEY, 0, METRICS_KEEP - 1)]
    samples.reverse()
    return {"samples": samples}


@router.get("/api/painel/logs")
def painel_logs(source: str = "", level: str = "", q: str = "", limit: int = 500, token: str = Cookie(None)):
    require_user(token)
    entries, total = _filter_logs(source, level, q, max(1, min(limit, 2000)))
    return {"entries": entries, "total": total}


@router.get("/api/painel/logs.txt", response_class=PlainTextResponse)
def painel_logs_text(source: str = "", level: str = "", q: str = "", token: str = Cookie(None)):
    require_user(token)
    entries, _ = _filter_logs(source, level, q, LOG_KEEP * 3)
    lines = []
    for e in entries:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.get("ts", 0)))
        lines.append(f"{stamp} {e.get('level', 'INFO'):7} {e.get('source', '?'):8} {e.get('msg', '')}")
    return PlainTextResponse(
        "\n".join(lines) + "\n",
        headers={"Content-Disposition": 'attachment; filename="bozera-subs-logs.txt"'},
    )


@router.get("/api/painel/job/{job_id}")
def painel_job(job_id: str, token: str = Cookie(None)):
    require_user(token)
    raw = r.get(f"job:{job_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="Job expirado ou inexistente")
    job = json.loads(raw)
    return {"status": job.get("status"), "error": job.get("error"), "log": (job.get("log") or "")[-20000:]}
