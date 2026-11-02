"""Configuração compartilhada entre a aplicação web e o worker.

install.json: onde as coisas estão no servidor. Gerado pelo setup.py.
settings.json: provedores, chaves e limites. Editado pelo wizard e pela aba Configurações.
"""
from __future__ import annotations

import copy
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/data/config"))
INSTALL_FILE = CONFIG_DIR / "install.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

PROVIDERS = {
    "opensubtitles": {
        "label": "OpenSubtitles.org",
        "help": "Usa a conta do opensubtitles.org. A conta do opensubtitles.com é outra e não funciona aqui.",
        "url": "https://www.opensubtitles.org",
        "fields": [
            {"name": "username", "label": "Usuário", "secret": False},
            {"name": "password", "label": "Senha", "secret": True},
        ],
    },
    "subdl": {
        "label": "SubDL",
        "help": "Chave de API gerada na sua conta do SubDL.",
        "url": "https://subdl.com",
        "fields": [{"name": "api_key", "label": "Chave de API", "secret": True}],
    },
    "subsource": {
        "label": "SubSource",
        "help": "Chave de API gerada na sua conta do SubSource.",
        "url": "https://subsource.net",
        "fields": [{"name": "api_key", "label": "Chave de API", "secret": True}],
    },
}

DEFAULT_SETTINGS = {
    "wizard_done": False,
    "max_per_day": 50,
    "scan_minutes": 10,
    "providers_version": 0,
    "providers": {
        name: {"enabled": False, **{field["name"]: "" for field in meta["fields"]}}
        for name, meta in PROVIDERS.items()
    },
}


def write_private(path: Path, data: dict) -> None:
    """Grava de forma atômica e só para o dono: o arquivo guarda chaves de API."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


_install_cache: tuple[float, dict] = (0.0, {})


def load_install() -> dict:
    # to_db_path roda para cada arquivo da biblioteca: só relê quando o arquivo muda
    global _install_cache
    try:
        modified = INSTALL_FILE.stat().st_mtime
        if modified != _install_cache[0]:
            _install_cache = (modified, json.loads(INSTALL_FILE.read_text(encoding="utf-8")))
        return _install_cache[1]
    except (OSError, ValueError):
        return {"jellyfin": {}, "libraries": []}


def libraries(kind: Optional[str] = None) -> list[dict]:
    items = load_install().get("libraries", [])
    return [lib for lib in items if kind is None or lib.get("kind") == kind]


def to_db_path(container_path: str) -> str:
    """Caminho dentro deste container para o caminho gravado no banco do Jellyfin."""
    for lib in libraries():
        root = lib["container"].rstrip("/")
        if container_path == root or container_path.startswith(root + "/"):
            return lib["db_path"].rstrip("/") + container_path[len(root):]
    return container_path


def from_db_path(db_path: str) -> Optional[str]:
    for lib in libraries():
        root = lib["db_path"].rstrip("/")
        if db_path == root or db_path.startswith(root + "/"):
            return lib["container"].rstrip("/") + db_path[len(root):]
    return None


def load_settings() -> dict:
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    providers = saved.pop("providers", {})
    settings.update(saved)
    for name in PROVIDERS:
        settings["providers"][name].update(providers.get(name, {}))
    return settings


def save_settings(settings: dict) -> None:
    write_private(SETTINGS_FILE, settings)


def bump_providers_version(settings: dict) -> None:
    """Avisa o worker que vale tentar de novo os filmes que ficaram sem legenda."""
    settings["providers_version"] = time.time()


def provider_ready(name: str, settings: dict) -> bool:
    entry = settings["providers"].get(name, {})
    fields = PROVIDERS[name]["fields"]
    return bool(entry.get("enabled")) and all(entry.get(field["name"]) for field in fields)
