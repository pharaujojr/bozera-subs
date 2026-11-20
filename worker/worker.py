"""Baixa legendas pt-BR para os filmes novos e sincroniza pela interface web."""
from __future__ import annotations

import glob
import hashlib
import io
import json
import logging
import logging.handlers
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import xmlrpc.client
import zipfile
from datetime import date
from pathlib import Path
from typing import Optional

import requests
from babelfish import Language
from subliminal import refine, region, scan_video
from subliminal.core import ProviderPool
from subliminal.providers.opensubtitles import OpenSubtitlesProvider
from subliminal.score import compute_score

import config

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/worker"))
SYNC_URL = os.environ.get("SYNC_URL", "http://web:8000")
INTERNAL_API_KEY = os.environ["INTERNAL_API_KEY"]
ONLY = os.environ.get("ONLY", "")
LIBRARY_DB = os.environ.get("LIBRARY_DB", "/jellyfin_db/jellyfin.db")

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v"}
EXTRAS_FOLDERS = {
    "featurettes", "extras", "behind the scenes", "deleted scenes", "interviews",
    "scenes", "shorts", "trailers", "specials", "bonus", "samples", "other",
}
MIN_VIDEO_SIZE = 200 * 1024 * 1024
STABLE_SECONDS = 600
SYNC_TIMEOUT = 3600
MAX_SUBTITLE_SIZE = 5 * 1024 * 1024
RETRY_NOT_FOUND = 7 * 86400
RETRY_ERROR = 86400
# Última fala antes de 70% da duração do vídeo = legenda incompleta
MIN_COVERAGE = 0.7
# Português de Portugal só entra se não houver pt-BR em nenhuma fonte
LANGUAGES = (
    {"suffix": "pt-BR", "lang": Language("por", "BR"), "subdl": "BR_PT", "subsource": "brazilian_portuguese"},
    {"suffix": "pt", "lang": Language("por"), "subdl": "PT", "subsource": "portuguese"},
)
PT_WORDS = re.compile(r"\b(não|você|está|então|também|muito|obrigad[oa])\b", re.I)
PT_TAG = re.compile(r"(^|[._\- ])(pob|por|pt|pt[-_]?br|portug\w*|brazil\w*|brasil\w*)([._\- ]|$)", re.I)
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt"}

STATE_FILE = DATA_DIR / "state.json"
RAW_DIR = DATA_DIR / "raw"

log = logging.getLogger("worker")

SETTINGS = config.load_settings()
OPENSUBTITLES_DOWN = False

LOG_DIR = DATA_DIR / "logs"


class JsonLines(logging.Formatter):
    """Uma linha JSON por evento: é o que a aba Logs do painel lê."""

    def __init__(self, source: str) -> None:
        super().__init__()
        self.source = source

    def format(self, record: logging.LogRecord) -> str:
        entry = {"ts": record.created, "level": record.levelname, "source": self.source, "msg": record.getMessage()}
        if getattr(record, "video", None):
            entry["video"] = record.video
        return json.dumps(entry, ensure_ascii=False)


def add_event_log(source: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / f"{source}.jsonl", maxBytes=5_000_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(JsonLines(source))
    handler.addFilter(lambda record: record.name.split(".")[0] == "worker")
    logging.getLogger().addHandler(handler)


def _timing_seconds(path: Path) -> list[float]:
    found = re.findall(r"(\d+):(\d\d):(\d\d)[,.](\d+)\s*-->", read_subtitle(path))
    return [int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000 for h, m, s, ms in found]


def describe_shift(raw: Path, output: Path) -> str:
    """Diz quanto a sincronização moveu a legenda, no início e no fim do filme."""
    before, after = _timing_seconds(raw), _timing_seconds(output)
    if not before or len(before) != len(after):
        return "ajuste não medido"
    return f"ajuste de {after[0] - before[0]:+.1f} s no início e {after[-1] - before[-1]:+.1f} s no fim, {len(after)} falas"


class QuotaError(Exception):
    pass


def refresh_settings() -> None:
    global SETTINGS, OPENSUBTITLES_DOWN
    SETTINGS = config.load_settings()
    OPENSUBTITLES_DOWN = False


def provider_on(name: str) -> bool:
    if name == "opensubtitles" and OPENSUBTITLES_DOWN:
        return False
    return config.provider_ready(name, SETTINGS)


def movie_roots() -> list[Path]:
    return [Path(lib["container"]) for lib in config.libraries("movies")]


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"videos": {}, "day": "", "count": 0}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    os.replace(tmp, STATE_FILE)


def list_videos() -> list[Path]:
    videos = []
    for root in movie_roots():
        for path in root.rglob("*"):
            if path.suffix.lower() not in VIDEO_EXTENSIONS or not path.is_file():
                continue
            relative = path.relative_to(root)
            if any(part.startswith(".") for part in relative.parts) or "sample" in path.name.lower():
                continue
            if any(part.lower() in EXTRAS_FOLDERS for part in relative.parts[:-1]):
                continue
            if ONLY and ONLY.lower() not in str(relative).lower():
                continue
            if path.stat().st_size >= MIN_VIDEO_SIZE:
                videos.append(path)
    return sorted(videos, key=lambda p: p.stat().st_mtime, reverse=True)


def is_stable(video: Path) -> bool:
    if any(f.suffix == ".part" for f in video.parent.iterdir()):
        return False
    return time.time() - video.stat().st_mtime > STABLE_SECONDS


def is_portuguese_tag(name: str, stem: str = "") -> bool:
    """Procura o idioma no nome, sem contar o título do filme ("Pt 1", por exemplo)."""
    name = name.lower()
    if stem and name.startswith(stem.lower()):
        name = name[len(stem):]
    return bool(PT_TAG.search(name))


def read_subtitle(path: Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def has_portuguese_file(video: Path) -> bool:
    """Só conta o que o Jellyfin mostra: arquivo ao lado do vídeo, com o nome dele."""
    for f in video.parent.glob(f"{glob.escape(video.stem)}*"):
        if f.suffix.lower() not in SUBTITLE_EXTENSIONS or not f.is_file():
            continue
        if is_portuguese_tag(f.name, video.stem):
            return True
        if len(PT_WORDS.findall(read_subtitle(f))) >= 20:
            return True
    return False


def find_local_source(video: Path) -> Optional[tuple[Path, dict]]:
    """Subs/*.srt dos torrents não aparecem no Jellyfin, mas servem de fonte sem gastar cota."""
    if video.parent in movie_roots():
        return None
    siblings = [
        f for f in video.parent.rglob("*")
        if f.suffix.lower() in VIDEO_EXTENSIONS and f.is_file() and f.stat().st_size >= MIN_VIDEO_SIZE
        and not any(p.lower() in EXTRAS_FOLDERS for p in f.relative_to(video.parent).parts[:-1])
    ]
    if len(siblings) > 1:
        return None
    brazilian, other = [], []
    for f in video.parent.rglob("*.srt"):
        relative = f.relative_to(video.parent).parts
        if any(p.lower() in EXTRAS_FOLDERS for p in relative[:-1]):
            continue
        name = f.name.lower()
        if "brazil" in name or "brasil" in name or re.search(r"pt[-_]?br|\.pob\.", name):
            brazilian.append(f)
        elif is_portuguese_tag(f.name, video.stem):
            other.append(f)
    if brazilian:
        return max(brazilian, key=lambda f: f.stat().st_size), LANGUAGES[0]
    if other:
        return max(other, key=lambda f: f.stat().st_size), LANGUAGES[1]
    return None


def has_portuguese_embedded(video: Path) -> bool:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries",
         "stream_tags=language,title", "-of", "json", str(video)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        return False
    for stream in json.loads(result.stdout).get("streams", []):
        tags = {k.lower(): v for k, v in stream.get("tags", {}).items()}
        if is_portuguese_tag(tags.get("language", "")) or is_portuguese_tag(tags.get("title", "")):
            return True
    return False


def has_portuguese_in_jellyfin(video: Path) -> bool:
    """O Jellyfin guarda as legendas que ele baixou em metadata/, fora da pasta do filme."""
    db_path = config.to_db_path(str(video))
    # immutable=1: o banco está vivo e montado somente leitura
    conn = sqlite3.connect(f"file:{LIBRARY_DB}?mode=ro&immutable=1", uri=True, timeout=10)
    try:
        rows = conn.execute(
            "SELECT m.Language, m.Title, m.Path FROM MediaStreamInfos m "
            "JOIN BaseItems b ON b.Id = m.ItemId WHERE b.Path = ? AND m.StreamType = 2",
            (db_path,),
        ).fetchall()
    finally:
        conn.close()
    for language, title, path in rows:
        if is_portuguese_tag(language or "") or is_portuguese_tag(title or ""):
            return True
        if path and is_portuguese_tag(Path(path).name, video.stem):
            return True
    return False


def existing_portuguese(video: Path) -> Optional[str]:
    if has_portuguese_file(video):
        return "ja_tinha"
    if has_portuguese_embedded(video):
        return "embutida"
    if has_portuguese_in_jellyfin(video):
        return "no_jellyfin"
    return None


def movie_identities(video: Path) -> list[tuple[str, int]]:
    """Títulos para buscar: correção manual, nome do arquivo e o que o Jellyfin identificou."""
    identities: list[tuple[str, Optional[int]]] = []
    overrides = DATA_DIR / "titulos.json"
    if overrides.exists():
        manual = json.loads(overrides.read_text()).get(video.name)
        if manual:
            identities.append((manual["title"], manual["year"]))
    scanned = scan_video(str(video))
    guess_year = scanned.year
    identities.append((scanned.title, guess_year))

    db_path = config.to_db_path(str(video))
    conn = sqlite3.connect(f"file:{LIBRARY_DB}?mode=ro&immutable=1", uri=True, timeout=10)
    try:
        row = conn.execute(
            "SELECT Name, OriginalTitle, ProductionYear FROM BaseItems WHERE Path = ?", (db_path,)
        ).fetchone()
    finally:
        conn.close()
    # O Jellyfin às vezes identifica errado (Alone 2007 virou Ghost in the Shell 2005)
    if row and row[2] and (guess_year is None or abs(row[2] - guess_year) <= 1):
        identities += [(row[0], row[2]), (row[1], row[2])]

    result, seen = [], set()
    for title, year in identities:
        key = _normalize(title or "")
        # Título só com caracteres não latinos normaliza para vazio e casaria com tudo
        if key and year and key not in seen:
            seen.add(key)
            result.append((title, year))
    return result


def jellyfin_imdb(video: Path) -> Optional[str]:
    """IMDb identificado pelo Jellyfin, só se o ano bater com o nome do arquivo."""
    guess_year = scan_video(str(video)).year
    db_path = config.to_db_path(str(video))
    conn = sqlite3.connect(f"file:{LIBRARY_DB}?mode=ro&immutable=1", uri=True, timeout=10)
    try:
        row = conn.execute(
            "SELECT p.ProviderValue, b.ProductionYear FROM BaseItems b "
            "JOIN BaseItemProviders p ON p.ItemId = b.Id WHERE b.Path = ? AND p.ProviderId = 'Imdb'",
            (db_path,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0] or not row[1]:
        return None
    if guess_year is not None and abs(row[1] - guess_year) > 1:
        return None
    return row[0]


def download_opensubtitles(video: Path, title: str, year: int, language: dict) -> Optional[str]:
    if not provider_on("opensubtitles"):
        return None
    entry = SETTINGS["providers"]["opensubtitles"]
    credentials = {"username": entry["username"], "password": entry["password"]}
    scanned = scan_video(str(video))
    refine(scanned, refiners=("hash",))
    scanned.title, scanned.year = title, year
    lang = language["lang"]
    with ProviderPool(providers=["opensubtitles"], provider_configs={"opensubtitles": credentials}) as pool:
        found = [s for s in pool.list_subtitles(scanned, {lang}) if s.language == lang]
        # Sem hash igual, exige título e ano iguais: a busca devolve outros filmes
        usable = []
        for subtitle in found:
            matches = subtitle.get_matches(scanned)
            if "hash" in matches or {"title", "year"} <= matches:
                usable.append(subtitle)
        usable.sort(key=lambda s: compute_score(s, scanned), reverse=True)
        log.info("OpenSubtitles %s (%s, %s): %d resultados, %d aceitos", language["suffix"], title, year, len(found), len(usable), extra={"video": video.name})
        failures = 0
        for subtitle in usable[:3]:
            pool.download_subtitle(subtitle)
            if subtitle.content and subtitle.is_valid():
                if covers_video(subtitle.text, video):
                    return subtitle.text
                continue
            failures += 1
        if failures:
            raise QuotaError("download vazio, provável limite diário")
    return None


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _safe_get(url: str, **kwargs) -> Optional[requests.Response]:
    """A chave do SubDL vai na URL: nunca deixar a exceção do requests chegar ao log."""
    try:
        response = requests.get(url, timeout=30, **kwargs)
    except requests.RequestException as exc:
        log.warning("provedor indisponível: %s", type(exc).__name__)
        return None
    if response.status_code != 200:
        log.warning("provedor respondeu HTTP %d", response.status_code)
        return None
    return response


def _srt_from_zip(data: bytes) -> Optional[str]:
    """Lê em memória; o zip vem de terceiros e nada dele é extraído em disco."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = [
            e for e in archive.infolist()
            if e.filename.lower().endswith(".srt") and 0 < e.file_size <= MAX_SUBTITLE_SIZE
        ]
        if not entries:
            return None
        raw = archive.read(max(entries, key=lambda e: e.file_size))
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _title_candidates(title: str) -> list[str]:
    """Subtítulo ou "Remastered" no nome do arquivo zeram a busca: tenta títulos mais curtos."""
    words = title.split()
    names = [" ".join(words[:size]) for size in range(len(words), max(len(words) - 4, 0), -1)]
    return [n for i, n in enumerate(names) if i == 0 or len(_normalize(n)) >= 5]


def _same_movie(wanted: str, name: Optional[str], wanted_year: int, year: Optional[int]) -> bool:
    found = _normalize(name or "")
    if not found or (found != wanted and (len(found) < 4 or not wanted.startswith(found))):
        return False
    # O ano varia entre bases (Hagazussa: 2017 no arquivo, 2018 no SubDL)
    return abs((year or 0) - wanted_year) <= 1


_durations: dict[str, Optional[float]] = {}
_CUE_END = re.compile(r"-->\s*(\d+):(\d\d):(\d\d)")


def video_duration(video: Path) -> Optional[float]:
    key = str(video)
    if key not in _durations:
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", key],
                capture_output=True, text=True, timeout=120,
            ).stdout.strip()
            _durations[key] = float(out) if out else None
        except (subprocess.SubprocessError, ValueError, OSError):
            _durations[key] = None
    return _durations[key]


def covers_video(text: str, video: Optional[Path]) -> bool:
    """Legenda partida em CD1/CD2 acaba no meio do filme (Funny Games 1997: 43 de 109 min)."""
    duration = video_duration(video) if video else None
    if not duration:
        return True
    ends = [int(h) * 3600 + int(m) * 60 + int(s) for h, m, s in _CUE_END.findall(text)]
    last = max(ends, default=0)
    if last >= MIN_COVERAGE * duration:
        return True
    log.info("legenda incompleta recusada: última fala em %.0f de %.0f min (parte de CD1/CD2?)",
             last / 60, duration / 60, extra={"video": video.name})
    return False


def _is_acceptable(text: Optional[str], video: Optional[Path] = None) -> bool:
    return (bool(text) and text.count("-->") >= 50 and len(PT_WORDS.findall(text)) >= 5
            and covers_video(text, video))


def _release_tokens(text: str) -> set[str]:
    return set(re.split(r"[^a-z0-9]+", text.lower()))


def download_subdl(video: Path, title: str, year: int, language: dict, imdb: Optional[str] = None) -> Optional[str]:
    if not provider_on("subdl"):
        return None
    key = SETTINGS["providers"]["subdl"]["api_key"]
    wanted = _normalize(title)
    for name in [None] if imdb else _title_candidates(title):
        lookup = {"imdb_id": imdb} if imdb else {"film_name": name, "year": year}
        response = _safe_get(
            "https://api.subdl.com/api/v1/subtitles",
            params={"api_key": key, **lookup, "type": "movie",
                    "languages": language["subdl"], "subs_per_page": 30},
        )
        if response is None:
            return None
        payload = response.json()
        films = [
            r for r in payload.get("results") or []
            if imdb or _same_movie(wanted, r.get("name"), year, r.get("year"))
        ]
        if films and payload.get("subtitles"):
            break
    else:
        return None
    tokens = set(re.split(r"[^a-z0-9]+", video.stem.lower()))
    subtitles = sorted(
        payload.get("subtitles") or [],
        key=lambda s: (not s.get("hi"), len(tokens & set(re.split(r"[^a-z0-9]+", str(s.get("release_name")).lower())))),
        reverse=True,
    )
    log.info("SubDL %s (%s): %d resultados", language["suffix"], imdb or f"{title}, {year}", len(subtitles), extra={"video": video.name})
    for subtitle in subtitles[:3]:
        archive = _safe_get("https://dl.subdl.com" + subtitle["url"])
        if archive is None:
            continue
        try:
            text = _srt_from_zip(archive.content)
        except zipfile.BadZipFile:
            continue
        if _is_acceptable(text, video):
            return text
        log.info("SubDL %s: legenda recusada no filtro de qualidade", language["suffix"], extra={"video": video.name})
    return None


def download_subsource(video: Path, title: str, year: int, language: dict, imdb: Optional[str] = None) -> Optional[str]:
    if not provider_on("subsource"):
        return None
    base = "https://api.subsource.net/api/v1"
    headers = {"X-API-Key": SETTINGS["providers"]["subsource"]["api_key"]}
    wanted = _normalize(title)
    subtitles: list[dict] = []
    for name in [None] if imdb else _title_candidates(title):
        lookup = {"searchType": "imdb", "imdb": imdb} if imdb else {"searchType": "text", "q": name, "year": year, "type": "movie"}
        response = _safe_get(f"{base}/movies/search", headers=headers, params=lookup)
        if response is None:
            return None
        movie_ids = {
            m["movieId"] for m in response.json().get("data") or []
            if imdb or (m.get("type") == "movie" and _same_movie(wanted, m.get("title"), year, m.get("releaseYear")))
        }
        for movie_id in movie_ids:
            listing = _safe_get(f"{base}/subtitles", headers=headers,
                                params={"movieId": movie_id, "language": language["subsource"], "limit": 100})
            if listing is not None:
                subtitles += listing.json().get("data") or []
        if subtitles:
            break
    tokens = _release_tokens(video.stem)
    subtitles.sort(
        key=lambda s: (not s.get("hearingImpaired"),
                       len(tokens & _release_tokens(" ".join(s.get("releaseInfo") or []))),
                       s.get("downloads") or 0),
        reverse=True,
    )
    log.info("SubSource %s (%s): %d resultados", language["suffix"], imdb or f"{title}, {year}", len(subtitles), extra={"video": video.name})
    for subtitle in subtitles[:3]:
        archive = _safe_get(f"{base}/subtitles/{subtitle['subtitleId']}/download", headers=headers)
        if archive is None:
            continue
        try:
            text = _srt_from_zip(archive.content)
        except zipfile.BadZipFile:
            continue
        if _is_acceptable(text, video):
            return text
        log.info("SubSource %s: legenda recusada no filtro de qualidade", language["suffix"], extra={"video": video.name})
    return None


def find_subtitle(video: Path) -> Optional[tuple[str, dict]]:
    quota = None
    identities = movie_identities(video)
    imdb = jellyfin_imdb(video)
    log.info("buscando legenda: IMDb %s, títulos %s", imdb or "desconhecido", [f"{t} ({y})" for t, y in identities], extra={"video": video.name})
    for language in LANGUAGES:
        if imdb:
            text = download_subdl(video, "", 0, language, imdb) or download_subsource(video, "", 0, language, imdb)
            if text is not None:
                return text, language
        for title, year in identities:
            text = None
            try:
                text = download_opensubtitles(video, title, year, language)
            except QuotaError as exc:
                quota = exc
            if text is None:
                text = download_subdl(video, title, year, language)
            if text is None:
                text = download_subsource(video, title, year, language)
            if text is not None:
                return text, language
    if quota:
        raise quota
    return None


def run_sync(video: Path, raw: Path, output: Path) -> None:
    headers = {"X-Internal-Key": INTERNAL_API_KEY}
    response = requests.post(
        f"{SYNC_URL}/api/sync",
        json={"video_path": str(video), "subtitle_path": str(raw), "output_path": str(output)},
        headers=headers, timeout=30,
    )
    response.raise_for_status()
    job_id = response.json()["job_id"]
    log.info("enviada para sincronização (job %s)", job_id[:8], extra={"video": video.name})
    deadline = time.time() + SYNC_TIMEOUT
    while time.time() < deadline:
        time.sleep(15)
        job = requests.get(f"{SYNC_URL}/api/sync/status/{job_id}", headers=headers, timeout=30).json()
        if job["status"] == "completed":
            return
        if job["status"] == "failed":
            raise RuntimeError(job.get("error") or "falha na sincronização")
    raise TimeoutError("a sincronização excedeu o tempo limite")


def process(video: Path, state: dict) -> str:
    local = find_local_source(video)
    if local and not covers_video(read_subtitle(local[0]), video):
        local = None
    if local:
        text, language = read_subtitle(local[0]), local[1]
        log.info("usando legenda local: %s", local[0].name, extra={"video": video.name})
    else:
        found = find_subtitle(video)
        state["count"] += 1
        if found is None:
            return "sem_legenda"
        text, language = found

    raw = RAW_DIR / f"{hashlib.sha1(str(video).encode()).hexdigest()}.srt"
    raw.write_text(text, encoding="utf-8")
    # O Jellyfin 12.4 não reconhece .pob como idioma, só como título
    output = video.with_name(f"{video.stem}.{language['suffix']}.srt")
    try:
        run_sync(video, raw, output)
        log.info("OK, sincronizada: %s (%s)", output.name, describe_shift(raw, output), extra={"video": video.name})
        return "ok"
    except Exception as exc:
        # Legenda sem sincronizar ainda é melhor que nenhuma; dá para ajustar à mão
        log.warning("sincronização falhou (%s), mantendo original: %s", exc, video.name, extra={"video": video.name})
        tmp = output.with_name(f".{output.name}.tmp")
        shutil.copyfile(raw, tmp)
        os.replace(tmp, output)
        return "ok_sem_sync"


def check_opensubtitles() -> None:
    """Conta recusada não pode derrubar o ciclo: os outros provedores continuam."""
    global OPENSUBTITLES_DOWN
    if not provider_on("opensubtitles"):
        return
    entry = SETTINGS["providers"]["opensubtitles"]
    provider = OpenSubtitlesProvider(username=entry["username"], password=entry["password"])
    try:
        provider.initialize()
    except Exception as exc:
        OPENSUBTITLES_DOWN = True
        log.warning("OpenSubtitles indisponível neste ciclo: %s", type(exc).__name__)
        return
    # O servidor não implementa o close chamado aqui; o login já foi validado
    try:
        provider.terminate()
    except xmlrpc.client.Fault:
        pass


def retry_after_provider_change(state: dict) -> None:
    """Chave nova vale para os filmes que estavam esperando 7 dias."""
    version = SETTINGS.get("providers_version", 0)
    if state.get("providers_version") == version:
        return
    state["providers_version"] = version
    waiting = [v for v in state["videos"].values() if v.get("status") in ("sem_legenda", "erro")]
    for entry in waiting:
        entry.pop("next_try", None)
    if waiting:
        log.info("provedores alterados: %d filmes voltam para a busca", len(waiting))


def run_cycle(state: dict) -> None:
    refresh_settings()
    max_per_day = SETTINGS["max_per_day"]
    has_provider = any(provider_on(name) for name in config.PROVIDERS)
    if not has_provider:
        log.warning("nenhum provedor configurado; filmes sem português vão esperar")
    check_opensubtitles()
    retry_after_provider_change(state)
    today = date.today().isoformat()
    if state["day"] != today:
        state.update(day=today, count=0)

    videos = list_videos()
    started = time.time()
    tally = {"resolvidos": 0, "processados": 0, "aguardando_retry": 0, "instaveis": 0}
    log.info("ciclo iniciado: %d vídeos, %d de %d downloads hoje", len(videos), state["count"], max_per_day)
    for video in videos:
        if state["count"] >= max_per_day:
            log.warning("limite diário de %d downloads atingido", max_per_day)
            break
        key = str(video)
        entry = state["videos"].get(key, {})
        if entry.get("status") in ("ok", "ok_sem_sync", "embutida", "ja_tinha", "no_jellyfin"):
            tally["resolvidos"] += 1
            continue
        if time.time() < entry.get("next_try", 0):
            tally["aguardando_retry"] += 1
            continue
        if not is_stable(video):
            tally["instaveis"] += 1
            log.info("aguardando o arquivo estabilizar: %s", video.name, extra={"video": video.name})
            continue

        found = existing_portuguese(video)
        if found:
            state["videos"][key] = {"status": found}
            tally["resolvidos"] += 1
            log.info("já tem português (%s): %s", found, video.name, extra={"video": video.name})
        elif not has_provider and not find_local_source(video):
            tally["aguardando_retry"] += 1
            continue
        else:
            tally["processados"] += 1
            log.info("processando: %s", video.name, extra={"video": video.name})
            try:
                status = process(video, state)
                entry = {"status": status}
                if status == "sem_legenda":
                    entry["next_try"] = time.time() + RETRY_NOT_FOUND
                    log.warning("sem legenda aceitável, nova tentativa em 7 dias: %s", video.name, extra={"video": video.name})
                state["videos"][key] = entry
            except QuotaError as exc:
                log.warning("%s; parando até o próximo dia", exc)
                state["count"] = max_per_day
                save_state(state)
                break
            except Exception as exc:
                log.error("erro em %s: %s; nova tentativa em 24 h", video.name, type(exc).__name__, extra={"video": video.name})
                state["videos"][key] = {"status": "erro", "next_try": time.time() + RETRY_ERROR}
        save_state(state)
    save_state(state)
    log.info("ciclo concluído em %.0f s: %s", time.time() - started, tally)


def dry_run(state: dict) -> None:
    totals: dict[str, int] = {}
    for video in list_videos():
        status = state["videos"].get(str(video), {}).get("status") or existing_portuguese(video)
        if not status:
            status = "usaria_local" if find_local_source(video) else "buscaria"
        totals[status] = totals.get(status, 0) + 1
    print(json.dumps(totals, ensure_ascii=False))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("subliminal").setLevel(logging.ERROR)
    # Só o close inofensivo do provedor; os demais erros continuam no log
    logging.getLogger().handlers[0].addFilter(
        lambda record: "improperly terminated" not in record.getMessage()
    )
    logging.getLogger("rebulk").setLevel(logging.ERROR)
    region.configure("dogpile.cache.memory", replace_existing_backend=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    add_event_log("worker")

    state = load_state()
    if "--dry-run" in sys.argv:
        dry_run(state)
        return

    while True:
        try:
            run_cycle(state)
        except Exception as exc:
            log.error("ciclo falhou: %s", type(exc).__name__)
        if "--once" in sys.argv:
            return
        time.sleep(SETTINGS["scan_minutes"] * 60)


if __name__ == "__main__":
    main()
