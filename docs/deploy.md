# Installing TaDaMoo on a server

Three components, one installation. On Linux the interface is the web page the harness serves.

| Component | What it is | Needed |
|---|---|---|
| harness | the server and its web page (`serve/`, `tadamoo/`) | always |
| basalt | the Basalt-Plus engine (NVIDIA Blackwell) | only when asked |
| strata | the Strata engine (NVIDIA, AMD, Intel, Apple) | only when asked |

With no engine the harness answers through the providers (cloud, or an engine somewhere else). `python -m serve.server --standby` starts it that way.

## Linux (systemd)

```
sudo deploy/linux/install.sh install --public-name tadamoo.example.org --proxy nginx --generate-key
sudo deploy/linux/install.sh install --components harness,basalt --engine-bin /path/to/basalt --model /models/qwen.basalt
deploy/linux/install.sh status          sudo deploy/linux/install.sh uninstall [--purge]
```

`--dry-run` says what would be done and changes nothing. The service is `tadamoo.service`; its configuration is in `/etc/tadamoo/` (`tadamoo_config.json`, `tadamoo.env`, `proxy/`), the data in `/var/lib/tadamoo`.
The listening address is `--host` (this machine only by default) and the port `--port`.

### A proxy in front

`--public-name` puts the name into `allowed-hosts`; without it a proxy that forwards the public name as the `Host` header gets `403 host_not_allowed` (see [server-security.md](server-security.md)).
`--proxy nginx|caddy|npm` writes a ready recipe into `/etc/tadamoo/proxy/`: nginx and Caddy files, or the Nginx Proxy Manager settings. They keep the connection open and unbuffered for the streamed answers.
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

- A harness that does not serve its web page.
- The page at a path (`/harness/`): nine calls of the page use an absolute address.
- `X-Forwarded-Proto` for the absolute links the server writes behind HTTPS.
- The desktop app connecting to a remote server, and its GUI-only setup (Windows); the Mac shell (`deploy/mac/`).
