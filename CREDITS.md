# Créditos

O Bozera Subs é uma orquestração em volta destes projetos e serviços. O mérito do trabalho pesado é deles.

## Legendas e sincronização

| Projeto | Papel aqui | Licença |
|---|---|---|
| [ffsubsync](https://github.com/smacke/ffsubsync), de Stephen Macke | Alinha a legenda ao áudio do vídeo (o coração do projeto) | MIT |
| [subliminal](https://github.com/Diaoul/subliminal) | Análise do vídeo, pontuação das candidatas e provedor OpenSubtitles | MIT |
| [babelfish](https://github.com/Diaoul/babelfish) | Códigos de idioma (pt-BR / pt) | BSD-3-Clause |
| [FFmpeg](https://ffmpeg.org/) | Leitura do áudio, duração, preparo de áudio para o player | LGPL-2.1+ / GPL (pacote do Debian) |

## Aplicação

| Projeto | Licença |
|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) | MIT |
| [Starlette](https://github.com/encode/starlette) | BSD-3-Clause |
| [Pydantic](https://github.com/pydantic/pydantic) | MIT |
| [Uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause |
| [aiofiles](https://github.com/Tinche/aiofiles) | Apache-2.0 |
| [redis-py](https://github.com/redis/redis-py) | MIT |
| [Requests](https://github.com/psf/requests) | Apache-2.0 |
| [Python](https://www.python.org/) | PSF License |

## Infraestrutura

| Projeto | Papel | Licença |
|---|---|---|
| [Redis](https://redis.io/) (imagem `redis:7-alpine`) | Fila de jobs, sessões e métricas | Redis 7.4+: RSALv2 / SSPLv1 (até 7.2: BSD-3). Para uma alternativa sob BSD, o [Valkey](https://valkey.io/) funciona sem mudar o código |
| [Docker](https://www.docker.com/) e Compose | Empacotamento | Apache-2.0 |

## Ecossistema

- **[Jellyfin](https://jellyfin.org/)** (GPL-2.0): o servidor de mídia livre que torna tudo isto possível. O Bozera Subs só lê o banco dele, somente leitura, e grava legendas ao lado dos vídeos.
- **[OpenSubtitles.org](https://www.opensubtitles.org)**, **[SubDL](https://subdl.com)** e **[SubSource](https://subsource.net)**: os provedores. E principalmente as pessoas que **traduzem e sincronizam legendas de graça** e as publicam lá. Considere contribuir ou apoiar esses sites.

---

O código próprio deste repositório é MIT ([LICENSE](LICENSE)); cada projeto acima segue a sua licença. Esqueci alguém? Abra uma issue ou um PR.
