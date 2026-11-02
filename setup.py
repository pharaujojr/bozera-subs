#!/usr/bin/env python3
"""Primeira etapa da instalação: descobre o Jellyfin e as pastas de mídia neste servidor.

Roda no terminal, fora do Docker, porque os volumes precisam existir antes dos containers.
Gera .env, docker-compose.override.yml e data/config/install.json. O restante
(conta, provedores e chaves) é configurado no navegador.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
INSTALLED_DEFAULTS = {
    "data": "/var/lib/jellyfin/data",
    "metadata": "/var/lib/jellyfin/metadata",
    "root": "/var/lib/jellyfin/root/default",
}


def ask(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{question}{suffix}: ").strip()
    return answer or default


def ask_yes(question: str, default: bool = True) -> bool:
    answer = input(f"{question} [{'S/n' if default else 's/N'}]: ").strip().lower()
    return default if not answer else answer.startswith("s")


def run(*command: str) -> Optional[str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def docker_jellyfin_containers() -> list[str]:
    output = run("docker", "ps", "-a", "--format", "{{.Names}}\t{{.Image}}")
    if not output:
        return []
    names = []
    for line in output.splitlines():
        name, _, image = line.partition("\t")
        if "jellyfin" in name.lower() or "jellyfin" in image.lower():
            names.append(name)
    return names


def docker_mounts(container: str) -> list[dict]:
    output = run("docker", "inspect", container)
    if not output:
        return []
    return [
        {"source": m["Source"], "destination": m["Destination"]}
        for m in json.loads(output)[0].get("Mounts", [])
    ]


def readable(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def locate_in_config(config: Path) -> dict:
    """A imagem oficial usa /config/data; a da linuxserver usa /config/data/data."""
    found = {"data": None, "metadata": None, "root": None}
    for candidate in (config / "data", config / "data" / "data", config):
        if readable(candidate / "jellyfin.db"):
            found["data"] = str(candidate)
            break
    for candidate in (config / "metadata", config / "data" / "metadata"):
        if readable(candidate):
            found["metadata"] = str(candidate)
            break
    for candidate in (config / "root" / "default", config / "data" / "root" / "default"):
        if readable(candidate):
            found["root"] = str(candidate)
            break
    return found


def read_library_folders(root: Optional[str]) -> list[dict]:
    """Lê as bibliotecas que o próprio Jellyfin tem cadastradas."""
    if not root or not readable(Path(root)):
        return []
    folders = []
    try:
        entries = sorted(Path(root).iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            kinds = [f.stem for f in entry.glob("*.collection")]
            links = [f.read_text(encoding="utf-8").strip() for f in entry.glob("*.mblink")]
        except OSError:
            continue
        kind = "series" if "tvshows" in kinds else "movies" if "movies" in kinds else None
        if kind:
            folders += [{"kind": kind, "db_path": link} for link in links if link.startswith("/")]
    return folders


def host_path_for(db_path: str, mounts: list[dict]) -> Optional[str]:
    """No Docker, o banco guarda o caminho de dentro do container do Jellyfin."""
    best = None
    for mount in mounts:
        destination = mount["destination"].rstrip("/")
        if db_path == destination or db_path.startswith(destination + "/"):
            if best is None or len(destination) > len(best["destination"].rstrip("/")):
                best = mount
    if best is None:
        return None
    return best["source"].rstrip("/") + db_path[len(best["destination"].rstrip("/")):]


def build_libraries(folders: list[dict]) -> list[dict]:
    counters = {"movies": 0, "series": 0}
    names = {"movies": "filmes", "series": "series"}
    libraries = []
    for folder in folders:
        counters[folder["kind"]] += 1
        number = counters[folder["kind"]]
        base = names[folder["kind"]]
        libraries.append({
            "kind": folder["kind"],
            "host": folder["host"],
            "db_path": folder["db_path"],
            "container": f"/media/{base}" if number == 1 else f"/media/{base}-{number}",
        })
    return libraries


def database_problem(data_dir: Optional[str]) -> Optional[str]:
    if not data_dir:
        return "pasta de dados não informada"
    path = Path(data_dir) / "jellyfin.db"
    if not readable(path):
        return "não consegui ler jellyfin.db com este usuário; confira a pasta ou rode com sudo"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, timeout=5)
        try:
            conn.execute("SELECT 1 FROM BaseItems LIMIT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return "o banco não tem a tabela BaseItems; é preciso uma versão mais nova do Jellyfin"
    return None


def owner_of(path: str) -> tuple[int, int]:
    info = os.stat(path)
    return info.st_uid, info.st_gid


def yaml_string(value: str) -> str:
    # JSON é YAML válido e resolve aspas, espaços e colchetes nos caminhos
    return json.dumps(value, ensure_ascii=False)


def render_override(jellyfin: dict, libraries: list[dict], group: Optional[int]) -> str:
    shared = [
        yaml_string(jellyfin["data_host"] + ":/jellyfin_db:ro"),
        *[yaml_string(f'{lib["host"]}:{lib["container"]}') for lib in libraries],
    ]
    web = list(shared)
    if jellyfin.get("metadata_host"):
        web.append(yaml_string(f'{jellyfin["metadata_host"]}:/jellyfin_metadata'))
    subtitles = Path(jellyfin["data_host"]) / "subtitles"
    if readable(subtitles):
        web.append(yaml_string(f"{subtitles}:/subtitles:ro"))

    def block(service: str, volumes: list[str]) -> list[str]:
        lines = [f"  {service}:"]
        if group is not None:
            lines += ["    group_add:", f'      - "{group}"']
        lines.append("    volumes:")
        lines += [f"      - {volume}" for volume in volumes]
        return lines

    lines = [
        "# Gerado pelo setup.py. Para mudar, rode o setup.py de novo.",
        "services:",
        *block("web", web),
        *block("worker", shared),
    ]
    return "\n".join(lines) + "\n"


def read_env(path: Path) -> dict:
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.startswith("#"):
                values[key.strip()] = value.strip()
    return values


def render_env(existing: dict, puid: int, pgid: int, port: str, transcode: str) -> str:
    values = {
        "PUID": puid,
        "PGID": pgid,
        "PORT": port,
        "TRANSCODE_CACHE": transcode,
        # Reexecutar o setup não pode derrubar a automação nem reabrir o wizard
        "INTERNAL_API_KEY": existing.get("INTERNAL_API_KEY") or secrets.token_urlsafe(32),
        "SETUP_TOKEN": existing.get("SETUP_TOKEN") or secrets.token_urlsafe(24),
    }
    if existing.get("BIND_ADDRESS"):
        values["BIND_ADDRESS"] = existing["BIND_ADDRESS"]
    return "".join(f"{key}={value}\n" for key, value in values.items())


def server_address() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            return probe.getsockname()[0]
    except OSError:
        return "IP-DO-SERVIDOR"


def collect_jellyfin() -> tuple[dict, list[dict]]:
    print("\n1. Jellyfin")
    containers = docker_jellyfin_containers()
    installed = readable(Path(INSTALLED_DEFAULTS["data"]) / "jellyfin.db")
    if containers:
        print(f"   Container encontrado: {', '.join(containers)}")
    if installed:
        print(f"   Instalação encontrada em {INSTALLED_DEFAULTS['data']}")
    guess = "docker" if containers and not installed else "instalado"

    while True:
        kind = ask("O Jellyfin está instalado no sistema ou em Docker? (instalado/docker)", guess).lower()
        if kind in ("instalado", "docker"):
            break
        print("   Responda instalado ou docker.")

    mounts: list[dict] = []
    if kind == "docker":
        container = ask("Nome do container", containers[0] if containers else "jellyfin")
        mounts = docker_mounts(container)
        if not mounts:
            print("   Não consegui ler os volumes desse container; informe os caminhos manualmente.")
        config = next((m["source"] for m in mounts if m["destination"].rstrip("/") == "/config"), "")
        located = locate_in_config(Path(config)) if config else {"data": None, "metadata": None, "root": None}
        located["container"] = container
    else:
        located = dict(INSTALLED_DEFAULTS)

    while True:
        data_dir = ask("Pasta que contém o jellyfin.db", located.get("data") or "")
        problem = database_problem(data_dir)
        if not problem:
            break
        print(f"   Atenção: {problem}.")
        if data_dir and ask_yes("Usar este caminho mesmo assim?", False):
            break

    metadata = ask("Pasta de metadados do Jellyfin (Enter para pular)", located.get("metadata") or "")
    jellyfin = {
        "type": kind,
        "container": located.get("container"),
        "data_host": data_dir.rstrip("/"),
        "metadata_host": metadata.rstrip("/"),
    }
    return jellyfin, collect_libraries(kind, located.get("root"), mounts)


def collect_libraries(kind: str, root: Optional[str], mounts: list[dict]) -> list[dict]:
    print("\n2. Pastas de mídia")
    folders = []
    for folder in read_library_folders(root):
        host = folder["db_path"] if kind == "instalado" else host_path_for(folder["db_path"], mounts)
        if host:
            folders.append({**folder, "host": host})
    if folders:
        print("   Bibliotecas cadastradas no Jellyfin:")
        for folder in folders:
            label = "filmes" if folder["kind"] == "movies" else "séries"
            print(f"     {label}: {folder['host']}")
        if not ask_yes("Usar estas pastas?"):
            folders = []
    while not any(f["kind"] == "movies" for f in folders):
        print("   Informe ao menos uma pasta de filmes.")
        folders += ask_folders("movies", "filmes", kind)
    if not any(f["kind"] == "series" for f in folders) and ask_yes("Adicionar uma pasta de séries?", False):
        folders += ask_folders("series", "séries", kind)
    return build_libraries(folders)


def ask_folders(library_kind: str, label: str, kind: str) -> list[dict]:
    host = ask(f"Pasta de {label} neste servidor").rstrip("/")
    if not host:
        return []
    db_path = host
    if kind == "docker":
        db_path = ask("Essa pasta aparece com qual caminho dentro do Jellyfin?", host).rstrip("/")
    return [{"kind": library_kind, "host": host, "db_path": db_path}]


def write_files(jellyfin: dict, libraries: list[dict], port: str, transcode: str) -> None:
    media = libraries[0]["host"]
    puid, pgid = owner_of(media) if readable(Path(media)) else (os.getuid(), os.getgid())
    group = None
    if readable(Path(jellyfin["data_host"]) / "jellyfin.db"):
        db_gid = owner_of(str(Path(jellyfin["data_host"]) / "jellyfin.db"))[1]
        group = db_gid if db_gid != pgid else None

    data = ROOT / "data"
    for name in ("config", "auth", "worker"):
        (data / name).mkdir(parents=True, exist_ok=True)
    Path(transcode if transcode.startswith("/") else ROOT / transcode).mkdir(parents=True, exist_ok=True)
    (data / "auth").chmod(0o700)

    install = {
        "jellyfin": {
            "type": jellyfin["type"],
            "container": jellyfin.get("container"),
            "data": jellyfin["data_host"],
            "metadata": jellyfin["metadata_host"],
        },
        "libraries": libraries,
    }
    (data / "config" / "install.json").write_text(json.dumps(install, ensure_ascii=False, indent=1), encoding="utf-8")
    env_path = ROOT / ".env"
    env_path.write_text(render_env(read_env(env_path), puid, pgid, port, transcode), encoding="utf-8")
    env_path.chmod(0o600)
    (ROOT / "docker-compose.override.yml").write_text(render_override(jellyfin, libraries, group), encoding="utf-8")

    if os.getuid() == 0:
        # Rodando com sudo: os containers rodam como o dono da mídia e precisam gravar aqui
        for path in [data, *data.rglob("*"), env_path]:
            os.chown(path, puid, pgid)
    elif os.getuid() != puid:
        print(f"\n   Atenção: os containers rodam como UID {puid}, dono da mídia, mas você é {os.getuid()}.")
        print(f"   Ajuste com: sudo chown -R {puid}:{pgid} {data}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sem-subir", action="store_true", help="só gera os arquivos, sem iniciar os containers")
    args = parser.parse_args()

    print("Bozera Subs: instalação")
    if not shutil.which("docker"):
        print("O Docker não foi encontrado. Instale o Docker e o plugin Compose antes de continuar.")
        return 1
    jellyfin, libraries = collect_jellyfin()

    print("\n3. Aplicação")
    existing = read_env(ROOT / ".env")
    port = ask("Porta da interface web", existing.get("PORT", "8765"))
    transcode = ask("Pasta para o cache de áudio do player", existing.get("TRANSCODE_CACHE", "./data/transcode"))

    write_files(jellyfin, libraries, port, transcode)
    print("\nArquivos gerados: .env, docker-compose.override.yml e data/config/install.json")

    token = read_env(ROOT / ".env")["SETUP_TOKEN"]
    url = f"http://{server_address()}:{port}/setup?token={token}"
    if not args.sem_subir and ask_yes("Iniciar os containers agora?"):
        if subprocess.run(["docker", "compose", "up", "-d", "--build"], cwd=ROOT).returncode != 0:
            print("O Docker Compose falhou; veja as mensagens acima.")
            return 1
    else:
        print("Para iniciar: docker compose up -d --build")
    print(f"\nContinue no navegador (este link cria a conta de administrador):\n  {url}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nInstalação interrompida.")
        sys.exit(130)
