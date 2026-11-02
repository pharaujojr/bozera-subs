"""Wizard de primeiro acesso e configurações de provedores."""
from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import sqlite3
import xmlrpc.client
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen

import redis
from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import auth
import config
from painel import event, require_user

router = APIRouter()
r = redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)

AUTH_TOKEN_TTL = 86400 * 7
ASSETS = Path(__file__).parent / "assets"
ASSET_TYPES = {"logo.svg": "image/svg+xml", "providers.js": "text/javascript; charset=utf-8"}
USERNAME = re.compile(r"^[\w.@-]{1,64}$")
TEST_TITLE = "tt0111161"  # Um Sonho de Liberdade: existe em todos os provedores
TIMEOUT = 15
OPENSUBTITLES_URL = "https://api.opensubtitles.org/xml-rpc"
OPENSUBTITLES_AGENT = "VLSub 0.11.1"


class AccountRequest(BaseModel):
    token: str
    username: str
    password: str


class SettingsRequest(BaseModel):
    max_per_day: Optional[int] = None
    scan_minutes: Optional[int] = None
    providers: Optional[dict[str, dict]] = None


class TestRequest(BaseModel):
    fields: dict[str, str] = {}


def issue_session(request: Request, user: str) -> JSONResponse:
    token = secrets.token_urlsafe(32)
    r.setex(f"token:{token}", AUTH_TOKEN_TTL, user)
    response = JSONResponse({"success": True})
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(key="token", value=token, httponly=True, samesite="lax", secure=secure, max_age=AUTH_TOKEN_TTL)
    return response


def client_of(request: Request) -> str:
    return request.client.host if request.client else "?"


def too_many_failures(client: str) -> bool:
    return int(r.get(f"login:fail:{client}") or 0) >= auth.MAX_FAILURES


def record_failure(client: str) -> int:
    key = f"login:fail:{client}"
    failures = r.incr(key)
    r.expire(key, auth.LOCK_SECONDS)
    return failures


def clear_failures(client: str) -> None:
    r.delete(f"login:fail:{client}")


def _has_session(token: Optional[str]) -> bool:
    return bool(token and r.get(f"token:{token}"))


def _public_settings(settings: dict) -> dict:
    """Valores secretos nunca saem do servidor: a tela só sabe se existem."""
    providers = []
    for name, meta in config.PROVIDERS.items():
        entry = settings["providers"][name]
        providers.append({
            "name": name,
            "label": meta["label"],
            "help": meta["help"],
            "url": meta["url"],
            "enabled": bool(entry.get("enabled")),
            "ready": config.provider_ready(name, settings),
            "fields": [
                {
                    "name": field["name"],
                    "label": field["label"],
                    "secret": field["secret"],
                    "has_value": bool(entry.get(field["name"])),
                    "value": "" if field["secret"] else entry.get(field["name"], ""),
                }
                for field in meta["fields"]
            ],
        })
    return {
        "max_per_day": settings["max_per_day"],
        "scan_minutes": settings["scan_minutes"],
        "wizard_done": settings["wizard_done"],
        "providers": providers,
    }


def _bounded(value: int, low: int, high: int, label: str) -> int:
    if not low <= value <= high:
        raise HTTPException(status_code=400, detail=f"{label} precisa ficar entre {low} e {high}.")
    return value


def _apply_provider_changes(settings: dict, changes: dict[str, dict]) -> bool:
    changed = False
    for name, change in changes.items():
        meta = config.PROVIDERS.get(name)
        if not meta:
            raise HTTPException(status_code=400, detail="Provedor desconhecido.")
        entry = settings["providers"][name]
        for field in meta["fields"]:
            incoming = change.get("fields", {}).get(field["name"], "")
            # Secreto vazio mantém o valor salvo; só null apaga
            if incoming is None:
                changed = changed or bool(entry.get(field["name"]))
                entry[field["name"]] = ""
            elif not isinstance(incoming, str):
                raise HTTPException(status_code=400, detail=f"Valor inválido em {meta['label']}.")
            elif incoming.strip() or not field["secret"]:
                value = incoming.strip()
                changed = changed or entry.get(field["name"]) != value
                entry[field["name"]] = value
        if "enabled" in change:
            enabled = bool(change["enabled"])
            changed = changed or entry.get("enabled") != enabled
            entry["enabled"] = enabled
        if entry["enabled"] and not all(entry.get(field["name"]) for field in meta["fields"]):
            raise HTTPException(status_code=400, detail=f"Preencha os dados de {meta['label']} antes de ativar.")
    return changed


def _http_status(url: str, headers: dict) -> tuple[int, bytes]:
    """A URL pode levar a chave: exceções daqui não vão para log nem para a resposta."""
    request = UrlRequest(url, headers={"User-Agent": "bozera-subs", **headers})
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read(200_000)
    except HTTPError as exc:
        return exc.code, b""


def _test_subdl(fields: dict) -> tuple[bool, str]:
    query = urlencode({"api_key": fields["api_key"], "imdb_id": TEST_TITLE, "type": "movie", "subs_per_page": 1})
    status, body = _http_status(f"https://api.subdl.com/api/v1/subtitles?{query}", {})
    if status == 200:
        try:
            if json.loads(body).get("status"):
                return True, "Chave aceita pelo SubDL."
        except ValueError:
            return False, "O SubDL respondeu algo inesperado."
        return False, "O SubDL recusou a chave."
    if status in (401, 403):
        return False, "O SubDL recusou a chave."
    return False, f"O SubDL respondeu HTTP {status}."


def _test_subsource(fields: dict) -> tuple[bool, str]:
    query = urlencode({"searchType": "imdb", "imdb": TEST_TITLE})
    status, _ = _http_status(f"https://api.subsource.net/api/v1/movies/search?{query}", {"X-API-Key": fields["api_key"]})
    if status == 200:
        return True, "Chave aceita pelo SubSource."
    if status in (401, 403):
        return False, "O SubSource recusou a chave."
    return False, f"O SubSource respondeu HTTP {status}."


class _TimeoutTransport(xmlrpc.client.SafeTransport):
    def make_connection(self, host):
        connection = super().make_connection(host)
        if isinstance(connection, http.client.HTTPConnection):
            connection.timeout = TIMEOUT
        return connection


def _test_opensubtitles(fields: dict) -> tuple[bool, str]:
    proxy = xmlrpc.client.ServerProxy(OPENSUBTITLES_URL, transport=_TimeoutTransport())
    answer = proxy.LogIn(fields["username"], fields["password"], "eng", OPENSUBTITLES_AGENT)
    status = str(answer.get("status", ""))
    if status.startswith("200"):
        return True, "Login aceito pelo OpenSubtitles.org."
    if status.startswith("401"):
        return False, "Usuário ou senha recusados. Confira se a conta é do opensubtitles.org."
    return False, f"O OpenSubtitles.org respondeu: {status[:60]}."


TESTERS = {"opensubtitles": _test_opensubtitles, "subdl": _test_subdl, "subsource": _test_subsource}


def _checks() -> dict:
    install = config.load_install()
    jellyfin = install.get("jellyfin", {})
    checks = []

    def add(label: str, ok: bool, detail: str) -> None:
        checks.append({"label": label, "ok": ok, "detail": detail})

    database = Path(os.environ.get("LIBRARY_DB", "/jellyfin_db/jellyfin.db"))
    try:
        conn = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True, timeout=5)
        try:
            movies = conn.execute(
                "SELECT count(*) FROM BaseItems WHERE Type = 'MediaBrowser.Controller.Entities.Movies.Movie'"
            ).fetchone()[0]
        finally:
            conn.close()
        add("Banco do Jellyfin", True, f"{movies} filmes cadastrados")
    except sqlite3.Error:
        add("Banco do Jellyfin", False, "Não consegui ler o jellyfin.db. Rode o setup.py de novo.")

    metadata = Path("/jellyfin_metadata")
    if jellyfin.get("metadata"):
        writable = os.access(metadata, os.W_OK)
        add("Metadados do Jellyfin", metadata.exists(),
            "Leitura e gravação" if writable else "Só leitura: legendas salvas pelo Jellyfin não poderão ser ajustadas")
    for library in install.get("libraries", []):
        label = "Filmes" if library["kind"] == "movies" else "Séries"
        path = Path(library["container"])
        ok = path.exists() and os.access(path, os.W_OK)
        add(f"{label}: {library['host']}", ok, "Leitura e gravação" if ok else "Sem permissão de gravação nesta pasta")
    if not install.get("libraries"):
        add("Pastas de mídia", False, "Nenhuma pasta configurada. Rode o setup.py.")
    return {"type": jellyfin.get("type"), "container": jellyfin.get("container"), "data": jellyfin.get("data"), "checks": checks}


@router.get("/assets/{name}")
def asset(name: str):
    media_type = ASSET_TYPES.get(name)
    if not media_type:
        raise HTTPException(status_code=404, detail="Não encontrado")
    return FileResponse(ASSETS / name, media_type=media_type)


@router.get("/setup", response_class=HTMLResponse)
def setup_page(token: str = Cookie(None)):
    if auth.exists():
        if config.load_settings()["wizard_done"]:
            return RedirectResponse(url="/", status_code=302)
        if not _has_session(token):
            return RedirectResponse(url="/login", status_code=302)
    return (Path(__file__).parent / "templates" / "setup.html").read_text()


@router.get("/api/setup/state")
def setup_state(token: str = Cookie(None)):
    return {
        "has_account": auth.exists(),
        "authenticated": _has_session(token),
        "wizard_done": config.load_settings()["wizard_done"],
    }


@router.post("/api/setup/account")
def setup_account(req: AccountRequest, request: Request):
    client = client_of(request)
    if auth.exists():
        raise HTTPException(status_code=409, detail="A conta já foi criada.")
    if too_many_failures(client):
        raise HTTPException(status_code=429, detail="Muitas tentativas. Aguarde 15 minutos.")
    expected = os.environ.get("SETUP_TOKEN", "")
    if not expected or not secrets.compare_digest(req.token.encode(), expected.encode()):
        failures = record_failure(client)
        event("WARNING", f"código de instalação recusado de {client} ({failures} de {auth.MAX_FAILURES})")
        raise HTTPException(status_code=403, detail="Código de instalação incorreto. Use o link mostrado pelo setup.py.")
    if not USERNAME.match(req.username):
        raise HTTPException(status_code=400, detail="O usuário só pode ter letras, números e . _ - @")
    problem = auth.password_problem(req.password)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    auth.create_account(req.username, req.password)
    clear_failures(client)
    event("INFO", f"conta de administrador criada: {req.username}")
    return issue_session(request, req.username)


@router.get("/api/setup/checks")
def setup_checks(token: str = Cookie(None)):
    require_user(token)
    return _checks()


@router.post("/api/setup/finish")
def setup_finish(token: str = Cookie(None)):
    user = require_user(token)
    settings = config.load_settings()
    settings["wizard_done"] = True
    config.save_settings(settings)
    event("INFO", f"wizard concluído por {user}")
    return {"success": True}


@router.get("/api/settings")
def get_settings(token: str = Cookie(None)):
    require_user(token)
    return _public_settings(config.load_settings())


@router.put("/api/settings")
def put_settings(req: SettingsRequest, token: str = Cookie(None)):
    user = require_user(token)
    settings = config.load_settings()
    if req.max_per_day is not None:
        settings["max_per_day"] = _bounded(req.max_per_day, 1, 1000, "O limite diário")
    if req.scan_minutes is not None:
        settings["scan_minutes"] = _bounded(req.scan_minutes, 1, 1440, "O intervalo")
    if req.providers is not None and _apply_provider_changes(settings, req.providers):
        config.bump_providers_version(settings)
    config.save_settings(settings)
    event("INFO", f"configurações alteradas por {user}")
    return _public_settings(settings)


@router.post("/api/settings/test/{name}")
def test_provider(name: str, req: TestRequest, token: str = Cookie(None)):
    require_user(token)
    meta = config.PROVIDERS.get(name)
    if not meta:
        raise HTTPException(status_code=404, detail="Provedor desconhecido.")
    stored = config.load_settings()["providers"][name]
    fields = {}
    for field in meta["fields"]:
        fields[field["name"]] = (req.fields.get(field["name"]) or "").strip() or stored.get(field["name"], "")
        if not fields[field["name"]]:
            return {"ok": False, "message": f"Preencha {field['label'].lower()}."}
    try:
        ok, message = TESTERS[name](fields)
    except (URLError, OSError, xmlrpc.client.Error, http.client.HTTPException) as exc:
        # Só o tipo do erro: a mensagem poderia conter a URL com a chave
        return {"ok": False, "message": f"Não consegui falar com {meta['label']} ({type(exc).__name__})."}
    return {"ok": ok, "message": message}
