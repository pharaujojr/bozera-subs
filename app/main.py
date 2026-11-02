from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

import redis
import secrets
from fastapi import BackgroundTasks, Cookie, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import auth
import config
import wizard
from painel import event, router as painel_router, start_sampler

# Pode ficar acessível de fora: sem documentação da API aberta sem login
app = FastAPI(title="Bozera Subs", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(painel_router)
app.include_router(wizard.router)


class SetupGate:
    """Sem conta, tudo leva ao wizard; com wizard pendente, a sessão também volta para ele."""

    NO_ACCOUNT = ("/setup", "/assets/", "/api/setup/")
    PENDING = NO_ACCOUNT + ("/api/settings", "/api/logout", "/api/check-auth", "/login", "/api/login")

    def __init__(self, inner) -> None:
        self.inner = inner

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and not self._allowed(scope):
            if scope["path"].startswith("/api/"):
                response = JSONResponse({"detail": "Conclua a instalação para continuar."}, status_code=403)
            else:
                response = RedirectResponse(url="/setup", status_code=302)
            await response(scope, receive, send)
            return
        await self.inner(scope, receive, send)

    def _allowed(self, scope) -> bool:
        path = scope["path"]
        request = Request(scope)
        expected = os.environ.get("INTERNAL_API_KEY", "")
        internal = request.headers.get("x-internal-key", "")
        if expected and internal and secrets.compare_digest(internal, expected):
            return True
        if not auth.exists():
            return path.startswith(self.NO_ACCOUNT)
        if config.load_settings()["wizard_done"] or path.startswith(self.PENDING):
            return True
        token = request.cookies.get("token")
        return not (token and r.get(f"token:{token}"))


app.add_middleware(SetupGate)


class QuietAccessLog(logging.Filter):
    """Polling e checagens de saúde escondiam os acessos que importam."""

    NOISE = ("/api/sync/status", "/api/painel", "/api/artwork", "/api/video/status")

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        path, status = str(args[2]), args[4]
        # Página de login antiga em cache manda a senha na URL: nunca registrar a query
        if "?" in path and path.startswith(("/api/login", "/api/change-password")):
            path = path.split("?", 1)[0]
            record.args = (args[0], args[1], path, *args[3:])
        if isinstance(status, int) and status >= 400:
            return True
        return path != "/" and not path.startswith(self.NOISE)


logging.getLogger("uvicorn.access").addFilter(QuietAccessLog())


@app.on_event("startup")
def cleanup_orphaned_jobs() -> None:
    """A restart kills any in-flight ffmpeg/ffs subprocess. Since job status now
    lives in Redis (persistent), a stale 'running' entry would otherwise sit
    forever instead of just vanishing like the old in-memory dict did."""
    for key in r.scan_iter("job:*"):
        raw = r.get(key)
        if not raw:
            continue
        data = json.loads(raw)
        if data.get("status") in ("pending", "running"):
            data["status"] = "failed"
            data["error"] = "Interrompido por reinício do servidor"
            r.set(key, json.dumps(data), keepttl=True)
            event("WARNING", f"job interrompido pelo reinício: {Path(data.get('video') or '').name}")
    event("INFO", "interface iniciada")
    start_sampler()

# Redis holds small, short-lived state: auth tokens, job status, codec-probe
# results. The actual transcoded video files stay on disk (TRANSCODE_CACHE_DIR)
# — they're multi-GB and don't belong in an in-memory store.
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
r = redis.from_url(REDIS_URL, decode_responses=True)

AUTH_TOKEN_TTL = 86400 * 7  # 7 days, matches the cookie's max_age

MEDIA_PATHS = [lib["container"] for lib in config.libraries()]
SUBTITLES_PATH = Path(os.environ.get("SUBTITLES_PATH", "/subtitles"))
METADATA_PATH = Path(os.environ.get("METADATA_PATH", "/jellyfin_metadata"))
LIBRARY_DB = os.environ.get("LIBRARY_DB", "/jellyfin_db/jellyfin.db")

to_db_path = config.to_db_path

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".flv"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}

VIDEO_MIME_TYPES = {
    ".mkv": "video/x-matroska",
    ".mp4": "video/mp4",
    ".avi": "video/x-msvideo",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
}

# Audio codecs browsers can decode natively. Anything else (AC3/EAC3/DTS/TrueHD/PCM…)
# needs remuxing before <video> will produce sound.
BROWSER_AUDIO_CODECS = {"aac", "mp3", "opus", "vorbis"}

TRANSCODE_CACHE_DIR = Path(os.environ.get("TRANSCODE_CACHE_DIR", "/transcode_cache"))
TRANSCODE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def probe_audio_codec(path: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15,
        )
        codec = out.stdout.strip()
        return codec or None
    except Exception:
        return None


def probe_duration_seconds(path: str) -> Optional[float]:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def needs_audio_transcode(path: str) -> bool:
    """True when the file's audio codec isn't natively playable by browsers."""
    try:
        mtime = int(os.path.getmtime(path))
    except OSError:
        return False

    cache_key = f"audiocodec:{hashlib.sha256(path.encode('utf-8')).hexdigest()}:{mtime}"
    cached = r.get(cache_key)
    if cached is not None:
        return cached == "1"

    codec = probe_audio_codec(path)
    result = codec is not None and codec not in BROWSER_AUDIO_CODECS
    r.setex(cache_key, 86400 * 7, "1" if result else "0")
    return result


def transcode_cache_path(video_path: str) -> Path:
    key = hashlib.sha256(video_path.encode("utf-8")).hexdigest()
    return TRANSCODE_CACHE_DIR / f"{key}.mp4"


JOB_TTL = 3600  # jobs auto-expire from Redis 1h after their last update


def job_get(job_id: str) -> Optional[dict]:
    raw = r.get(f"job:{job_id}")
    return json.loads(raw) if raw else None


def job_set(job_id: str, data: dict) -> None:
    r.setex(f"job:{job_id}", JOB_TTL, json.dumps(data))


def job_update(job_id: str, **fields) -> None:
    data = job_get(job_id) or {}
    data.update(fields)
    job_set(job_id, data)


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class SyncRequest(BaseModel):
    video_path: str
    subtitle_path: str
    output_path: Optional[str] = None


def get_item_id_from_db(video_path: str) -> Optional[str]:
    """Query jellyfin.db (Jellyfin 10.9+) to find the item Id for a given path."""
    db = Path(LIBRARY_DB)
    if not db.exists():
        return None
    try:
        # immutable=1 avoids write-lock errors on a live database mounted read-only
        conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        cursor = conn.cursor()
        cursor.execute("SELECT Id FROM BaseItems WHERE Path = ?", (to_db_path(video_path),))
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            return row[0].lower()  # subtitles dirs use lowercase UUIDs
    except Exception as e:
        print(f"[db] {e}")
    return None


def load_title_metadata() -> dict[str, dict]:
    """Bulk-read titles from jellyfin.db, keyed by the DB's Path value.

    One query for the whole library instead of one per file — used to build
    clean display titles (movie name + year, or series/season/episode) instead
    of showing raw folder/file names in the UI.
    """
    db = Path(LIBRARY_DB)
    if not db.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT Path, Name, ProductionYear, SeriesName, ParentIndexNumber, IndexNumber "
            "FROM BaseItems WHERE Path IS NOT NULL"
        )
        rows = cursor.fetchall()
        conn.close()
        return {
            path: {
                "name": name,
                "year": year,
                "series_name": series_name,
                "season_number": season_number,
                "episode_number": episode_number,
            }
            for path, name, year, series_name, season_number, episode_number in rows
        }
    except Exception as e:
        print(f"[db] {e}")
        return {}


def build_display_title(meta: Optional[dict], fallback: str) -> str:
    if not meta or not meta.get("name"):
        return fallback
    if meta.get("series_name"):
        season = meta.get("season_number")
        episode = meta.get("episode_number")
        if season is not None and episode is not None:
            return f"{meta['series_name']} · S{season:02d}E{episode:02d} · {meta['name']}"
        return f"{meta['series_name']} · {meta['name']}"
    if meta.get("year"):
        return f"{meta['name']} ({meta['year']})"
    return meta["name"]


def find_local_subtitles(video_path: str) -> list[dict]:
    video = Path(video_path)
    results = []
    try:
        for f in video.parent.iterdir():
            if f.suffix.lower() in SUBTITLE_EXTENSIONS and f.stem.startswith(video.stem):
                results.append({
                    "path": str(f),
                    "source": "local",
                    "display": f"[Local] {f.name}",
                })
    except PermissionError:
        pass
    return results


def find_jellyfin_subtitles(video_path: str) -> list[dict]:
    item_id = get_item_id_from_db(video_path)
    if not item_id:
        return []

    results = []

    # Local 1: /var/lib/jellyfin/data/subtitles/{id[:2]}/{id}/  (plugin OpenSubtitles)
    sub_dir = SUBTITLES_PATH / item_id[:2] / item_id
    if sub_dir.exists():
        for f in sorted(sub_dir.glob("*.srt")):
            results.append({
                "path": str(f),
                "source": "jellyfin_cache",
                "display": f"[Jellyfin] {f.name} (índice {f.stem})",
            })

    # Local 2: /var/lib/jellyfin/metadata/library/{id_nodash[:2]}/{id_nodash}/  (SaveSubtitlesWithMedia)
    id_nodash = item_id.replace("-", "")
    meta_dir = METADATA_PATH / "library" / id_nodash[:2] / id_nodash
    if meta_dir.exists():
        for f in sorted(meta_dir.iterdir()):
            if f.suffix.lower() in SUBTITLE_EXTENSIONS:
                results.append({
                    "path": str(f),
                    "source": "jellyfin_cache",
                    "display": f"[Jellyfin] {f.name}",
                })

    return results


_SRT_TS = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})"
)


def _ts_to_ms(ts: str) -> int:
    h, m, rest = ts.replace(",", ".").split(":")
    s, ms = rest.split(".")
    return int(h) * 3_600_000 + int(m) * 60_000 + int(s) * 1_000 + int(ms)


def _ms_to_ts(ms: int) -> str:
    ms = max(0, ms)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def safe_replace(tmp: Path, dst: Path) -> None:
    """Move tmp → dst atomically, handling cross-device links."""
    try:
        os.replace(tmp, dst)
    except OSError:
        # Cross-device: stage a temp next to the destination, then rename
        stage = dst.parent / f".{uuid.uuid4().hex}.srt"
        try:
            shutil.copy2(tmp, stage)
            os.chmod(stage, 0o664)
            os.replace(stage, dst)
        finally:
            stage.unlink(missing_ok=True)
            tmp.unlink(missing_ok=True)


def apply_shift(subtitle_path: str, offset_ms: int, output_path: str) -> None:
    src = Path(subtitle_path)
    text = src.read_text(encoding="utf-8", errors="replace")

    def _shift(match: re.Match) -> str:
        start = _ts_to_ms(match.group(1)) + offset_ms
        end = _ts_to_ms(match.group(2)) + offset_ms
        return f"{_ms_to_ts(start)} --> {_ms_to_ts(end)}"

    dst = Path(output_path)
    tmp = Path(f"/tmp/{uuid.uuid4().hex}.srt")
    try:
        tmp.write_text(_SRT_TS.sub(_shift, text), encoding="utf-8")
        os.chmod(tmp, 0o664)
        safe_replace(tmp, dst)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


class ShiftRequest(BaseModel):
    subtitle_path: str
    offset_ms: int          # negativo = adiantar, positivo = atrasar
    video_path: Optional[str] = None   # se fornecido, salva ao lado do vídeo
    output_path: Optional[str] = None


@app.post("/api/shift")
def shift_subtitle(req: ShiftRequest, token: str = Cookie(None)):
    require_auth(token)
    if not Path(req.subtitle_path).exists():
        raise HTTPException(status_code=404, detail="Legenda não encontrada")

    output = req.output_path or req.subtitle_path

    try:
        apply_shift(req.subtitle_path, req.offset_ms, output)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    sign = "+" if req.offset_ms >= 0 else ""
    event("INFO", f"ajuste manual de {sign}{req.offset_ms / 1000:.3f} s: {Path(output).name}", video=Path(output).name)
    return {
        "output": output,
        "offset_ms": req.offset_ms,
        "offset_label": f"{sign}{req.offset_ms / 1000:.3f}s",
    }


_PCT_RE = re.compile(r"\b(\d+)%\|")

# ffsubsync runs two tqdm passes (reference audio, then subtitle) — each one
# reports its own 0-100%. We map them onto a single 0-100 job progress.
_PHASE2_MARKER = "extracting speech segments from subtitles"


def _iter_output_chunks(stream):
    """Split a subprocess stream on '\\r' as well as '\\n'.

    tqdm (used by ffsubsync) rewrites its progress bar with carriage returns
    and only emits a real newline once a bar finishes — plain line iteration
    over the stream would silently swallow every intermediate update.
    """
    buf = ""
    while True:
        chunk = stream.read(4096)
        if not chunk:
            if buf:
                yield buf
            return
        buf += chunk
        while True:
            cr, nl = buf.find("\r"), buf.find("\n")
            candidates = [i for i in (cr, nl) if i != -1]
            if not candidates:
                break
            idx = min(candidates)
            yield buf[:idx]
            buf = buf[idx + 1:]


def run_sync_job(job_id: str, video: str, subtitle: str, output: str) -> None:
    started = time.time()
    job_update(job_id, status="running", progress=0, started_at=started)
    name = Path(video).name
    event("INFO", f"sincronização iniciada: {name}", video=name, job=job_id)

    tmp = Path(f"/tmp/{uuid.uuid4().hex}.srt")
    proc = None
    try:
        proc = subprocess.Popen(
            ["ffs", video, "-i", subtitle, "-o", str(tmp)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        log_lines: list[str] = []
        phase = 1
        for chunk in _iter_output_chunks(proc.stdout):
            chunk = chunk.rstrip()
            if not chunk:
                continue
            log_lines.append(chunk)
            if _PHASE2_MARKER in chunk:
                phase = 2
            m = _PCT_RE.search(chunk)
            if m:
                phase_pct = int(m.group(1))
                overall = phase_pct // 2 if phase == 1 else 50 + phase_pct // 2
                job_update(job_id, progress=overall)

        proc.wait()
        log = "\n".join(log_lines)

        if proc.returncode == 0 and tmp.exists():
            os.chmod(tmp, 0o664)
            safe_replace(tmp, Path(output))
            job_update(job_id, status="completed", progress=100, log=log, finished_at=time.time())
            event("INFO", f"sincronização concluída em {time.time() - started:.0f} s: {Path(output).name}", video=name, job=job_id)
        else:
            tmp.unlink(missing_ok=True)
            errors = [l for l in log_lines if "ERROR" in l or "error" in l.lower()]
            job_update(
                job_id,
                status="failed",
                error="\n".join(errors) if errors else "ffsubsync não gerou arquivo de saída",
                log=log,
                finished_at=time.time(),
            )
            event("ERROR", f"sincronização falhou (código {proc.returncode}): {name}", video=name, job=job_id)
    except Exception as exc:
        if proc and proc.poll() is None:
            proc.kill()
        tmp.unlink(missing_ok=True)
        job_update(job_id, status="failed", error=str(exc), finished_at=time.time())
        event("ERROR", f"sincronização falhou ({type(exc).__name__}): {name}", video=name, job=job_id)


_PROGRESS_TIME_RE = re.compile(r"out_time=(\d+):(\d+):(\d+)\.\d+")


def run_transcode_job(job_id: str, video_path: str, cache_path: Path) -> None:
    started = time.time()
    job_update(job_id, status="running", progress=0, started_at=started)
    name = Path(video_path).name
    event("INFO", f"preparando áudio para o player: {name}", video=name, job=job_id)

    duration = probe_duration_seconds(video_path) or 0
    tmp = cache_path.with_suffix(".tmp.mp4")
    proc = None
    try:
        proc = subprocess.Popen(
            [
                "ffmpeg", "-y", "-i", video_path,
                "-map", "0:v:0", "-map", "0:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2",
                "-progress", "pipe:1", "-nostats",
                str(tmp),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

        for line in proc.stdout:
            if duration > 0:
                m = _PROGRESS_TIME_RE.search(line)
                if m:
                    h, mnt, s = m.groups()
                    out_seconds = int(h) * 3600 + int(mnt) * 60 + int(s)
                    pct = max(0, min(99, int(out_seconds / duration * 100)))
                    job_update(job_id, progress=pct)

        proc.wait()

        if proc.returncode == 0 and tmp.exists():
            os.chmod(tmp, 0o664)
            os.replace(tmp, cache_path)
            job_update(job_id, status="completed", progress=100, finished_at=time.time())
            event("INFO", f"áudio preparado em {time.time() - started:.0f} s: {name}", video=name, job=job_id)
        else:
            tmp.unlink(missing_ok=True)
            job_update(job_id, status="failed", error="Falha ao preparar áudio (ffmpeg)", finished_at=time.time())
            event("ERROR", f"falha ao preparar áudio (código {proc.returncode}): {name}", video=name, job=job_id)
    except Exception as exc:
        if proc and proc.poll() is None:
            proc.kill()
        tmp.unlink(missing_ok=True)
        job_update(job_id, status="failed", error=str(exc), finished_at=time.time())
        event("ERROR", f"falha ao preparar áudio ({type(exc).__name__}): {name}", video=name, job=job_id)


def require_auth(token: str = Cookie(None)):
    """Check if user is authenticated."""
    user = token and r.get(f"token:{token}")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_auth_or_internal(token: Optional[str], internal_key: Optional[str]):
    """Same as require_auth, but also accepts the key used by the worker."""
    expected = os.environ.get("INTERNAL_API_KEY", "")
    if expected and internal_key and secrets.compare_digest(internal_key, expected):
        return "worker"
    return require_auth(token)


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return (Path(__file__).parent / "templates" / "login.html").read_text()


@app.post("/api/login")
def login(req: LoginRequest, request: Request):
    client = wizard.client_of(request)
    if wizard.too_many_failures(client):
        event("WARNING", f"login bloqueado por excesso de tentativas: {client}")
        raise HTTPException(status_code=429, detail="Muitas tentativas. Aguarde 15 minutos.")
    if not auth.verify(req.username, req.password):
        failures = wizard.record_failure(client)
        event("WARNING", f"login recusado de {client} ({failures} de {auth.MAX_FAILURES})")
        raise HTTPException(status_code=401, detail="Usuário ou senha incorretos.")
    wizard.clear_failures(client)
    event("INFO", f"login de {req.username} a partir de {client}")
    return wizard.issue_session(request, req.username)


@app.get("/trocar-senha", response_class=HTMLResponse)
def change_password_page(token: str = Cookie(None)):
    if not token or not r.get(f"token:{token}"):
        return RedirectResponse(url="/login", status_code=302)
    return (Path(__file__).parent / "templates" / "trocar-senha.html").read_text()


@app.post("/api/change-password")
def change_password(req: ChangePasswordRequest, request: Request, token: str = Cookie(None)):
    user = require_auth(token)
    client = wizard.client_of(request)
    if wizard.too_many_failures(client):
        raise HTTPException(status_code=429, detail="Muitas tentativas. Aguarde 15 minutos.")
    if not auth.verify(user, req.current_password):
        wizard.record_failure(client)
        event("WARNING", f"troca de senha recusada: senha atual incorreta, de {client}")
        raise HTTPException(status_code=401, detail="A senha atual está incorreta.")
    problem = auth.password_problem(req.new_password, req.current_password)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    auth.set_password(req.new_password)
    # Qualquer sessão aberta com a senha antiga deixa de valer
    for key in r.scan_iter("token:*"):
        r.delete(key)
    event("INFO", f"senha de {user} alterada; demais sessões encerradas")
    return wizard.issue_session(request, user)


@app.post("/api/logout")
def logout(token: str = Cookie(None)):
    if token:
        r.delete(f"token:{token}")
    response = JSONResponse({"success": True})
    response.delete_cookie("token")
    return response


@app.get("/api/check-auth")
def check_auth(token: str = Cookie(None)):
    user = token and r.get(f"token:{token}")
    if user:
        return {"authenticated": True, "user": user}
    return {"authenticated": False}


@app.get("/", response_class=HTMLResponse)
def root(token: str = Cookie(None)):
    if not token or not r.get(f"token:{token}"):
        return RedirectResponse(url="/login", status_code=302)
    return (Path(__file__).parent / "templates" / "index.html").read_text()


@app.get("/api/libraries")
def list_libraries(token: str = Cookie(None)):
    require_auth(token)
    return [
        {"path": p, "name": Path(p).name, "accessible": Path(p).exists()}
        for p in MEDIA_PATHS
    ]


@app.get("/api/movies")
def list_movies(library: str = "", search: str = "", page: int = 1, per_page: int = 60, token: str = Cookie(None)):
    require_auth(token)
    bases = [Path(library)] if library else [Path(p) for p in MEDIA_PATHS]
    title_meta = load_title_metadata()
    videos = []
    for base in bases:
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if f.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            if search and search.lower() not in f.name.lower() and search.lower() not in str(f.parent).lower():
                continue
            meta = title_meta.get(to_db_path(str(f)))
            videos.append({
                "path": str(f),
                "name": f.name,
                "folder": f.parent.name,
                "title": build_display_title(meta, fallback=f.parent.name),
                "library": str(base),
            })
    videos.sort(key=lambda x: x["title"].lower())
    total = len(videos)
    start = (page - 1) * per_page
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "videos": videos[start : start + per_page],
    }


@app.get("/api/video")
def get_video(path: str, token: str = Cookie(None)):
    require_auth(token)
    video = Path(path)
    if not video.exists():
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")

    if needs_audio_transcode(path):
        cache_path = transcode_cache_path(path)
        if not cache_path.exists():
            raise HTTPException(status_code=409, detail="Áudio ainda não preparado — chame /api/video/prepare")
        return FileResponse(cache_path, media_type="video/mp4")

    media_type = VIDEO_MIME_TYPES.get(video.suffix.lower(), "application/octet-stream")
    return FileResponse(video, media_type=media_type)


@app.get("/api/video/status")
def video_status(path: str, token: str = Cookie(None)):
    require_auth(token)
    if not Path(path).exists():
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")
    transcode = needs_audio_transcode(path)
    ready = True if not transcode else transcode_cache_path(path).exists()
    return {"needs_transcode": transcode, "ready": ready}


@app.post("/api/video/prepare")
def video_prepare(path: str, background_tasks: BackgroundTasks, token: str = Cookie(None)):
    require_auth(token)
    if not Path(path).exists():
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")

    cache_path = transcode_cache_path(path)
    if cache_path.exists():
        return {"ready": True, "job_id": None}

    # Avoid launching a duplicate transcode for the same file while one is in flight
    active_key = f"active_transcode:{hashlib.sha256(path.encode('utf-8')).hexdigest()}"
    existing_job_id = r.get(active_key)
    if existing_job_id:
        existing_job = job_get(existing_job_id)
        if existing_job and existing_job.get("status") in ("pending", "running"):
            return {"ready": False, "job_id": existing_job_id}

    job_id = str(uuid.uuid4())
    job_set(job_id, {"type": "transcode", "status": "pending", "progress": 0, "video": path, "error": None, "created_at": time.time()})
    r.setex(active_key, JOB_TTL, job_id)

    background_tasks.add_task(run_transcode_job, job_id, path, cache_path)
    return {"ready": False, "job_id": job_id}


@app.get("/api/artwork")
def get_artwork(video_path: str, token: str = Cookie(None)):
    require_auth(token)
    item_id = get_item_id_from_db(video_path)
    if not item_id:
        raise HTTPException(status_code=404, detail="Capa não encontrada")
    id_nodash = item_id.replace("-", "")
    artwork_path = METADATA_PATH / "library" / id_nodash[:2] / id_nodash / "poster.jpg"
    if not artwork_path.exists():
        raise HTTPException(status_code=404, detail="Capa não encontrada")
    return FileResponse(artwork_path, media_type="image/jpeg")


@app.get("/api/subtitles")
def get_subtitles(video_path: str, token: str = Cookie(None)):
    require_auth(token)
    if not Path(video_path).exists():
        raise HTTPException(status_code=404, detail="Arquivo de vídeo não encontrado")
    local = find_local_subtitles(video_path)
    cached = find_jellyfin_subtitles(video_path)
    return {"local": local, "jellyfin_cache": cached, "all": local + cached}


@app.get("/api/subtitle-content")
def get_subtitle_content(path: str, token: str = Cookie(None)):
    require_auth(token)
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
        return {"content": content}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/sync")
def sync_subtitle(req: SyncRequest, background_tasks: BackgroundTasks, token: str = Cookie(None), x_internal_key: str = Header(None)):
    origin = require_auth_or_internal(token, x_internal_key)
    if not Path(req.video_path).exists():
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")
    if not Path(req.subtitle_path).exists():
        raise HTTPException(status_code=404, detail="Legenda não encontrada")

    output = req.output_path or req.subtitle_path

    job_id = str(uuid.uuid4())
    job_set(job_id, {
        "status": "pending",
        "progress": 0,
        "video": req.video_path,
        "subtitle": req.subtitle_path,
        "output": output,
        "log": "",
        "error": None,
        "origin": origin,
        "created_at": time.time(),
    })

    background_tasks.add_task(run_sync_job, job_id, req.video_path, req.subtitle_path, output)
    return {"job_id": job_id, "output": output}


@app.get("/api/sync/status/{job_id}")
def sync_status(job_id: str, token: str = Cookie(None), x_internal_key: str = Header(None)):
    require_auth_or_internal(token, x_internal_key)
    job = job_get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")
    return job


@app.get("/player", response_class=HTMLResponse)
def player_page(token: str = Cookie(None)):
    if not token or not r.get(f"token:{token}"):
        return RedirectResponse(url="/login", status_code=302)
    return (Path(__file__).parent / "templates" / "player.html").read_text()
