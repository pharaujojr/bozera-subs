# Bozera Subs

Encontra e sincroniza legendas em português para os filmes do Jellyfin, com painel, logs e editor de legendas no navegador.

- **Worker:** verifica a biblioteca periodicamente e procura legenda pt-BR (ou pt-PT, se não houver) nos filmes que ainda não têm.
- **Sincronização:** ajusta a legenda ao áudio do seu arquivo com o [ffsubsync](https://github.com/smacke/ffsubsync) e grava `<filme>.pt-BR.srt` ao lado do vídeo.
- **Painel:** cobertura da biblioteca, sincronizações, temperatura e RAM em 24 h, logs com filtro e download.
- **Provedores:** OpenSubtitles.org, SubDL e SubSource.

## Requisitos

- Docker com o plugin Compose
- Python 3.9 ou mais novo no servidor, só para o instalador
- Jellyfin cujo `jellyfin.db` tenha a tabela `BaseItems`, instalado no sistema ou em Docker, na mesma máquina

## Instalação

```bash
git clone git@github.com:pharaujojr/bozera-subs.git
cd bozera-subs
python3 setup.py
```

A instalação tem duas etapas:

1. **No terminal, `setup.py`:** detecta se o Jellyfin está instalado ou em Docker, encontra o banco, os metadados e as bibliotecas, e gera `.env`, `docker-compose.override.yml` e `data/config/install.json`. Ela roda fora do navegador porque o Docker só monta pastas ao criar os containers. Se o seu usuário não conseguir ler a pasta do Jellyfin, rode com `sudo`.
2. **No navegador:** abra o link mostrado no fim do `setup.py`. Ele contém um código de uso único que autoriza criar a conta de administrador. Depois disso, o wizard confere as permissões e configura os provedores.

Não existe senha padrão. Antes de a conta ser criada, tudo redireciona para o wizard.

## Chaves de API e provedores

Ficam em **Painel → Configurações**, onde também é possível testar cada provedor, apagar dados salvos e mudar o limite de buscas e o intervalo de verificação.

| Provedor | O que informar | Observação |
|---|---|---|
| OpenSubtitles.org | Usuário e senha | A conta do opensubtitles.com é outra e não funciona |
| SubDL | Chave de API | |
| SubSource | Chave de API | |

Ao salvar uma mudança nos provedores, os filmes que estavam sem legenda voltam para a busca no próximo ciclo. Sem mudança, um filme sem legenda só é tentado de novo depois de 7 dias.

## Mudar pastas ou o Jellyfin

```bash
python3 setup.py
docker compose up -d
```

## Atualizar

```bash
git pull
docker compose up -d --build
```

## Onde ficam os dados

Tudo em `data/`, fora do Git:

| Caminho | Conteúdo | Quem acessa |
|---|---|---|
| `data/auth/auth.json` | Hash scrypt da senha | Só a interface |
| `data/config/install.json` | Caminhos do Jellyfin e da mídia | Interface e worker |
| `data/config/settings.json` | Provedores e chaves de API, em texto, com permissão 600 | Interface e worker |
| `data/worker/` | Estado, legendas originais e logs | Worker grava, interface lê |

## Segurança

- A interface usa HTTP puro. Para acessar de fora, coloque na frente um proxy com HTTPS ou use uma VPN como o Tailscale; não abra a porta direto na internet. Para limitar ao próprio servidor, defina `BIND_ADDRESS=127.0.0.1` no `.env`.
- Com 10 erros de login, o IP fica bloqueado por 15 minutos. Atrás de um proxy, todos os acessos podem aparecer com o mesmo IP.
- Quem faz login pode ler e gravar arquivos acessíveis ao container pela API do editor. Use só uma conta de confiança.

## Testes

```bash
python3 -m unittest discover -s tests
```

## Estrutura

```
app/        Interface web (FastAPI), painel e wizard
worker/     Busca de legendas
shared/     Configuração usada pelos dois
setup.py    Instalador de terminal
```
