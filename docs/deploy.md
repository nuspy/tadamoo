# Installing TaDaMoo on a server

Three components, one installation. On Linux the interface is the web page the harness serves.

| Component | What it is | Needed |
|---|---|---|
| harness | the server and its web page (`serve/`, `tadamoo/`) | always |
| basalt | the Basalt-Plus engine (NVIDIA Blackwell) | only when asked |
| strata | the Strata engine (NVIDIA, AMD, Intel, Apple) | only when asked |

With no engine the harness answers through the providers (cloud, or an engine somewhere else). `python -m serve.server --standby` starts it that way. A server with no engine binary says so
(`engine_installed: false` in `/health` and `/api/loader/status`): the page hides the Loader tab, and a Load asked for through the API is refused with `409 no_engine` and a plain sentence in English
and Italian ("No engine is installed on this server, so there is nothing to load a model into").

## Linux (systemd)

```
sudo deploy/linux/install.sh install --public-name tadamoo.example.org --proxy nginx --generate-key
sudo deploy/linux/install.sh install --components harness,basalt --model /models/qwen.basalt        # complete release: the engine is in engines/basalt
sudo deploy/linux/install.sh install --components harness,strata --strata-variant cuda13           # Strata, fetched from its own release and checked against its SHA256SUMS.txt
deploy/linux/install.sh status          sudo deploy/linux/install.sh uninstall [--purge]
```

`--dry-run` says what would be done and changes nothing. The service is `tadamoo.service`; its configuration is in `/etc/tadamoo/` (`tadamoo_config.json`, `tadamoo.env`, `proxy/`), the data in `/var/lib/tadamoo`.
The listening address is `--host` (this machine only by default) and the port `--port`. `--no-web` makes a harness that serves only its API (`no-web: true` in the config file; the root answers
`{"web": false}`, the page's files and fonts are not there): for a server that the desktop app, the phone app or other programs use.

### A proxy in front

`--public-name` puts the name into `allowed-hosts`; without it a proxy that forwards the public name as the `Host` header gets `403 host_not_allowed` (see [server-security.md](server-security.md)).
`--proxy nginx|caddy|npm` writes a ready recipe into `/etc/tadamoo/proxy/`: nginx and Caddy files, or the Nginx Proxy Manager settings. They keep the connection open and unbuffered for the streamed answers.
The same settings are on the page, **Settings > Network** (from the PC the server runs on): the address and port to listen on, the public name, the allowed host names, a **Test** that says whether the
address answers and whether the Host rule lets its name through (it asks the server's own `/api/network/ping` through the name, so a proxy in between is part of the test and the page shows the
Host and scheme that arrived), and a guided set-up. The values are written to the config file (`server.host`, `server.port`, `server.allowed-hosts`, `server.public-name`); a change applies when the server is started again and the page says so.
A proxy beside the server should send `Host` and `X-Forwarded-Proto` (`X-Forwarded-Host` too): the links the server writes then use the public name and `https` (see [remote-devices.md](remote-devices.md)).
**The server key** (Settings > Network too) is changed from a client with `POST /api/server-key {current, new}` (the current key is needed; `GET /api/server-key` says whether a key is set and whether it was
set from a client, never the key). The new key applies at once, without a restart; it is kept in the data folder (`server_key.json`) and wins over `--api-key` from the next start on. The previous key is still
believed from this machine (loopback) until the server is started again, so that conversations already running there are not cut off; from the network it stops at once. A server with no key gets one only from
its own PC. The page, the desktop app and the phone app keep the new key in their own entry of the server (`serve/server_key.py`).
The page works at the root of a name (`https://tadamoo.example.org/`). A path such as `/harness/` is not supported yet.

## Docker

`deploy/docker/` holds one Dockerfile for each product (`Dockerfile.harness`, `Dockerfile.engine-basalt`, `Dockerfile.engine-strata`) and a `docker-compose.yml` for the harness alone, with the engine as a commented block.
The harness reaches an engine as a provider: Settings > Providers, an OpenAI-compatible one at `http://engine:7232/v1`.

## Kubernetes and k3s

`deploy/helm/tadamoo` is one chart: the harness (one copy, a volume for its data, a Service, an optional Ingress whose host goes into `allowed-hosts` by itself) and, with `engine.enabled=true`, an engine in a pod of its own.
The engine pod takes `nvidia.com/gpu` and its model volume. The cluster is supposed to have its NVIDIA GPUs already set up (the device plugin, and the runtime class where it is needed).

```
helm install tadamoo deploy/helm/tadamoo --set ingress.enabled=true --set ingress.host=tadamoo.example.org
helm install tadamoo deploy/helm/tadamoo -f deploy/helm/tadamoo/values-k3s.yaml --set ingress.host=tadamoo.example.org --set engine.enabled=true --set engine.model=qwen.basalt
```

`values-k3s.yaml` sets Traefik as the ingress, `local-path` as the storage and the `nvidia` runtime class. The images are built from the Dockerfiles above and pushed to a registry the cluster reads.

## What is not done yet

- The page at a path (`/harness/`): nine calls of the page use an absolute address.
- The desktop app connecting to a remote server, and its GUI-only setup (Windows); the Mac shell (`deploy/mac/`).
