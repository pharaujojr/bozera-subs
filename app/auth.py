"""Conta de administrador: hash scrypt em arquivo, fora da imagem. Não existe senha padrão."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

AUTH_FILE = Path(os.environ.get("AUTH_FILE", "/data/auth/auth.json"))
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
MAX_FAILURES = 10
LOCK_SECONDS = 900


def _derive(password: str, salt: bytes, params: dict) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=params["n"], r=params["r"], p=params["p"],
        maxmem=256 * 1024 * 1024, dklen=32,
    )


def _load() -> Optional[dict]:
    try:
        return json.loads(AUTH_FILE.read_text())
    except (OSError, ValueError):
        return None


def _save(data: dict) -> None:
    tmp = AUTH_FILE.with_name(f".{AUTH_FILE.name}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, AUTH_FILE)


def _build_record(username: str, password: str) -> dict:
    salt = secrets.token_bytes(16)
    return {
        "username": username,
        "algo": "scrypt",
        **SCRYPT,
        "salt": salt.hex(),
        "hash": _derive(password, salt, SCRYPT).hex(),
        "changed_at": time.time(),
    }


def exists() -> bool:
    return _load() is not None


def create_account(username: str, password: str) -> None:
    _save(_build_record(username, password))


def verify(username: str, password: str) -> bool:
    """Sem arquivo de conta ninguém entra: falha fechada."""
    record = _load()
    if not record or len(password) > MAX_PASSWORD_LENGTH:
        return False
    computed = _derive(password, bytes.fromhex(record["salt"]), record)
    password_ok = hmac.compare_digest(computed, bytes.fromhex(record["hash"]))
    username_ok = hmac.compare_digest(username.encode("utf-8"), record["username"].encode("utf-8"))
    return password_ok and username_ok


def password_problem(password: str, current: str = "") -> Optional[str]:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"A senha precisa ter pelo menos {MIN_PASSWORD_LENGTH} caracteres."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"A senha pode ter no máximo {MAX_PASSWORD_LENGTH} caracteres."
    if current and password == current:
        return "A nova senha precisa ser diferente da atual."
    return None


def set_password(new: str) -> None:
    record = _load()
    _save(_build_record(record["username"], new))
