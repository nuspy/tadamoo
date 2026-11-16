#!/usr/bin/env bash
# TaDaMoo on a Linux server: one installation, three components.
#   harness  the server and its web page (always)
#   basalt   the Basalt-Plus engine (NVIDIA Blackwell), only when asked
#   strata   the Strata engine, only when asked
# The web page is the interface on Linux; with no engine the harness answers through the providers (cloud, or an engine elsewhere).
#
#   sudo ./install.sh install   [options]     sudo ./install.sh update    [options]
#   sudo ./install.sh uninstall [--purge]     ./install.sh status
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CMD="${1:-}"; [ $# -gt 0 ] && shift

PREFIX=/opt/tadamoo; DATA=/var/lib/tadamoo; CONF=/etc/tadamoo; RUN_USER=tadamoo
HOST=127.0.0.1; PORT=7232; COMPONENTS=harness; PUBLIC=(); API_KEY=""; GENERATE_KEY=0; NO_WEB=0; REMOTE_ADMIN=0
BASALT_BIN=""; STRATA_DIR=""; STRATA_DOWNLOAD=0; STRATA_VARIANT=""; BUILD_BASALT=0; MODEL=""; PROXY=none; START=1; DRY=0; YES=0; PURGE=0

usage() {
  cat <<'EOF'
Options of install and update:
  --components LIST    harness[,basalt][,strata]            (default: harness)
  --prefix DIR         where the program goes               (default: /opt/tadamoo)
  --data DIR           the user data: plans, sessions, memory (default: /var/lib/tadamoo)
  --config-dir DIR     the configuration                    (default: /etc/tadamoo)
  --user NAME          the account the service runs as      (default: tadamoo)
  --host ADDRESS       where it listens: 127.0.0.1 (this machine only), a LAN or Tailscale address, 0.0.0.0 (every address)
  --port N             the port                             (default: 7232)
  --public-name NAME   a name the server is reached by (a proxy's); may be repeated; goes into allowed-hosts
  --remote-admin       a client that sends the server key can also do what only this PC could (providers, settings, archives, the vault); needs a key
  --no-web             serve only the API: no web page (for a server that other programs, the desktop app or the phone app use)
  --api-key KEY        the server key (optional)            --generate-key   make one and keep it in the configuration folder
  --engine-bin FILE    a built Basalt-Plus binary           --build-basalt   build it from this tree (CUDA toolkit and cmake needed)
  --strata-dir DIR     the folder of the Strata binaries
  --strata-download    fetch the Strata engine from its own release (checked against its SHA256SUMS.txt) instead of --strata-dir
  --strata-variant V   cuda13 (default), cuda12, hip-halo, hip-rx6000, hip-rx7900 or hip-rx9070 (implies --strata-download)
  --model FILE         the model the engine loads at the Loader's Load (optional)
  --proxy none|nginx|caddy|npm   write the proxy recipe for the public name into the configuration folder
  --no-start           install without starting the service
  --dry-run            say what would be done and change nothing
  --yes                no questions
EOF
}

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
run() { if [ "$DRY" = 1 ]; then printf '+ %s\n' "$*"; else "$@"; fi; }
has() { case ",$COMPONENTS," in *",$1,"*) return 0;; *) return 1;; esac; }

parse() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --components) COMPONENTS="$2"; shift 2;;
      --prefix) PREFIX="$2"; shift 2;;
      --data) DATA="$2"; shift 2;;
      --config-dir) CONF="$2"; shift 2;;
      --user) RUN_USER="$2"; shift 2;;
      --host) HOST="$2"; shift 2;;
      --port) PORT="$2"; shift 2;;
      --public-name) PUBLIC+=("$2"); shift 2;;
      --no-web) NO_WEB=1; shift;;
      --remote-admin) REMOTE_ADMIN=1; shift;;
      --api-key) API_KEY="$2"; shift 2;;
      --generate-key) GENERATE_KEY=1; shift;;
      --engine-bin) BASALT_BIN="$2"; shift 2;;
      --build-basalt) BUILD_BASALT=1; shift;;
      --strata-dir) STRATA_DIR="$2"; shift 2;;
      --strata-download) STRATA_DOWNLOAD=1; shift;;
      --strata-variant) STRATA_VARIANT="$2"; STRATA_DOWNLOAD=1; shift 2;;
      --model) MODEL="$2"; shift 2;;
      --proxy) PROXY="$2"; shift 2;;
      --no-start) START=0; shift;;
      --dry-run) DRY=1; shift;;
      --yes) YES=1; shift;;
      --purge) PURGE=1; shift;;
      -h|--help) usage; exit 0;;
      *) die "unknown option $1 (see --help)";;
    esac
  done
  case "$PORT" in ''|*[!0-9]*) die "--port is a number";; esac
  case "$PROXY" in none|nginx|caddy|npm) ;; *) die "--proxy is none, nginx, caddy or npm";; esac
  has harness || COMPONENTS="harness,$COMPONENTS"
  for c in ${COMPONENTS//,/ }; do case "$c" in harness|basalt|strata) ;; *) die "unknown component $c";; esac; done
}

need_root() { [ "$DRY" = 1 ] || [ "$(id -u)" = 0 ] || die "run as root (sudo): the service and its folders are system ones"; }

python_bin() {
  for p in python3.13 python3.12 python3.11 python3.10 python3; do
    command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null && { command -v "$p"; return; }
  done
  die "Python 3.10 or newer is needed (python3, venv and pip)"
}

copy_program() {
  run mkdir -p "$PREFIX"
  if command -v rsync >/dev/null 2>&1; then
    run rsync -a --delete --exclude /engines --exclude /third_party --exclude .git --exclude .venv --exclude __pycache__ --exclude tests --exclude build --exclude build-win --exclude dist --exclude .claude --exclude '*.exe' --exclude node_modules "$SRC"/ "$PREFIX"/program/
  else
    run rm -rf "$PREFIX/program"; run mkdir -p "$PREFIX/program"
    run bash -c "cd '$SRC' && tar -c --exclude=./engines --exclude=./third_party --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=tests --exclude=build --exclude=build-win --exclude=dist --exclude=.claude --exclude='*.exe' --exclude=node_modules . | tar -x -C '$PREFIX/program'"
  fi
}

# agent-desktop (computer control) and agent-browser (the browser tools' default driver) are built from third_party/ by the vendored-tools workflow; the complete release has them in
# tools/vendored/ and the copy above brings them. They must be runnable by the service account; one in <data>/tools/<name>/ (an update) is taken before them.
vendored_tools() {
  [ -d "$PREFIX/program/tools/vendored" ] || { say "note: no tools/vendored here (agent-desktop, agent-browser): computer control uses the system's own backend and the browser its own driver"; return 0; }
  run find "$PREFIX/program/tools/vendored" -type f ! -name '*.md' ! -name '*.txt' ! -name LICENSE -exec chmod 0755 {} +
}

make_venv() {
  local py; py="$(python_bin)"
  [ -x "$PREFIX/.venv/bin/python" ] || run "$py" -m venv "$PREFIX/.venv"
  run "$PREFIX/.venv/bin/pip" install --quiet --upgrade pip
  run "$PREFIX/.venv/bin/pip" install --quiet -r "$PREFIX/program/requirements.txt"
}

engine_bin() {
  has basalt || return 0
  command -v nvidia-smi >/dev/null 2>&1 || say "warning: nvidia-smi is not there: Basalt-Plus needs an NVIDIA Blackwell GPU and its driver"
  if [ -n "$BASALT_BIN" ]; then
    run mkdir -p "$PREFIX/engines/basalt/bin"; run install -m 0755 "$BASALT_BIN" "$PREFIX/engines/basalt/bin/basalt"
  elif [ -x "$SRC/engines/basalt/bin/basalt" ]; then
    run mkdir -p "$PREFIX/engines"; run rm -rf "$PREFIX/engines/basalt"; run cp -a "$SRC/engines/basalt" "$PREFIX/engines/basalt"
  elif [ "$BUILD_BASALT" = 1 ]; then
    command -v cmake >/dev/null 2>&1 || die "--build-basalt needs cmake and the CUDA toolkit"
    run cmake -S "$SRC" -B "$PREFIX/engines/basalt/build" -DCMAKE_BUILD_TYPE=Release
    run cmake --build "$PREFIX/engines/basalt/build" -j
    run mkdir -p "$PREFIX/engines/basalt/bin"; run install -m 0755 "$PREFIX/engines/basalt/build/bin/basalt" "$PREFIX/engines/basalt/bin/basalt"
  else
    die "basalt asked: give --engine-bin FILE (a built binary) or --build-basalt (the complete release has the engine in engines/basalt)"
  fi
}

strata_download() {
  local variant="${STRATA_VARIANT:-cuda13}" tmp name url sum got
  case "$variant" in cuda13|cuda12) variant="cuda-$variant";; esac
  say "Strata: looking for the $variant engine in the latest release of Niko1221/Strata"
  [ "$DRY" = 1 ] && { say "+ download and check the Strata engine ($variant) into $PREFIX/engines/strata"; return 0; }
  tmp="$(mktemp -d)"
  python3 - "$variant" > "$tmp/found" <<'PY' || die "could not read the Strata release (network?)"
import json, re, sys, urllib.request
variant = sys.argv[1]
rel = json.load(urllib.request.urlopen("https://api.github.com/repos/Niko1221/Strata/releases/latest", timeout=30))
pat = re.compile(r"^strata-engine-.*-linux-x86_64-" + re.escape(variant) + r"\.tar\.gz$")
asset = next((a for a in rel["assets"] if pat.match(a["name"])), None)
sums = next((a for a in rel["assets"] if a["name"] == "SHA256SUMS.txt"), None)
if not asset or not sums:
    sys.exit(1)
print(asset["name"], asset["browser_download_url"], sums["browser_download_url"])
PY
  read -r name url sum < "$tmp/found" || die "no Strata engine called $variant for linux-x86_64 in its latest release"
  curl -fL --retry 3 -o "$tmp/$name" "$url"; curl -fsSL -o "$tmp/SHA256SUMS.txt" "$sum"
  want="$(awk -v n="$name" '$2 == n {print $1}' "$tmp/SHA256SUMS.txt")"; got="$(sha256sum "$tmp/$name" | cut -d' ' -f1)"
  [ -n "$want" ] && [ "$want" = "$got" ] || die "the Strata archive does not match its SHA256SUMS.txt"
  mkdir -p "$PREFIX/engines/strata"; tar -xzf "$tmp/$name" -C "$PREFIX/engines/strata"; rm -rf "$tmp"
  say "Strata engine installed: $name"
}

strata_bin() {
  has strata || return 0
  if [ -n "$STRATA_DIR" ]; then
    run mkdir -p "$PREFIX/engines/strata"; run cp -a "$STRATA_DIR"/. "$PREFIX/engines/strata/"
  elif [ "$STRATA_DOWNLOAD" = 1 ]; then
    strata_download
  else
    die "strata asked: give --strata-dir DIR (the folder of its binaries) or --strata-download"
  fi
}

write_config() {
  run mkdir -p "$CONF" "$DATA"
  local hosts="" n
  for n in "${PUBLIC[@]+"${PUBLIC[@]}"}"; do hosts="$hosts\"$n\", "; done
  hosts="[${hosts%, }]"
  local web=""
  [ "$NO_WEB" = 1 ] && web=', "no-web": true'
  [ "$REMOTE_ADMIN" = 1 ] && web="$web"', "remote-admin": true'
  if [ "$DRY" = 1 ]; then say "+ write $CONF/tadamoo_config.json (allowed-hosts $hosts${web:+, no-web})"; else
    printf '{"server": {"allowed-hosts": %s%s}}\n' "$hosts" "$web" > "$CONF/tadamoo_config.json"
  fi
  [ "$GENERATE_KEY" = 1 ] && [ -z "$API_KEY" ] && API_KEY="$(head -c 24 /dev/urandom | base64 | tr -d '=+/' | cut -c1-32)"
  if [ "$DRY" = 1 ]; then say "+ write $CONF/tadamoo.env (mode 600)"; else
    umask 077
    printf 'TADAMOO_DATA=%s\nTADAMOO_API_KEY=%s\n' "$DATA" "$API_KEY" > "$CONF/tadamoo.env"
    [ -n "$API_KEY" ] && say "server key: $API_KEY   (kept in $CONF/tadamoo.env)"
  fi
}

write_unit() {
  local engine="" extra=""
  if has basalt; then engine="--engine-bin $PREFIX/engines/basalt/bin/basalt"; elif has strata; then engine="--engine-bin $PREFIX/engines/strata/strata"; fi
  [ -n "$MODEL" ] && extra="--model $MODEL"
  local unit="[Unit]
Description=TaDaMoo harness (server and web page)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
EnvironmentFile=$CONF/tadamoo.env
WorkingDirectory=$PREFIX/program
ExecStart=$PREFIX/.venv/bin/python -m serve.server --standby $engine $extra --host $HOST --port $PORT --config $CONF/tadamoo_config.json --api-key \${TADAMOO_API_KEY}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target"
  if [ "$DRY" = 1 ]; then say "+ write /etc/systemd/system/tadamoo.service:"; printf '%s\n' "$unit" | sed 's/^/    /'; else printf '%s\n' "$unit" > /etc/systemd/system/tadamoo.service; fi
}

write_proxy() {
  [ "$PROXY" = none ] && return 0
  [ "${#PUBLIC[@]}" -gt 0 ] || die "--proxy needs --public-name"
  local name="${PUBLIC[0]}" dir="$CONF/proxy"
  run mkdir -p "$dir"
  case "$PROXY" in
    nginx) local f="$dir/tadamoo.nginx.conf"; local body="server {
    listen 80;
    server_name $name;
    client_max_body_size 512m;
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
}
map \$http_upgrade \$connection_upgrade { default upgrade; '' close; }";;
    caddy) local f="$dir/Caddyfile"; local body="$name {
    reverse_proxy 127.0.0.1:$PORT {
        flush_interval -1
    }
}";;
    npm) local f="$dir/nginx-proxy-manager.md"; local body="Nginx Proxy Manager: Proxy Host
- Domain Names: $name
- Scheme: http, Forward Hostname/IP: the address of this server, Forward Port: $PORT
- Turn on: Websockets Support, Block Common Exploits (optional); SSL tab: request a certificate, Force SSL.
- Advanced tab (custom configuration): proxy_buffering off; proxy_read_timeout 3600s; client_max_body_size 512m;
The name '$name' is already in allowed-hosts of this server (the proxy sends it as the Host header).";;
  esac
  if [ "$DRY" = 1 ]; then say "+ write $f"; else printf '%s\n' "$body" > "$f"; say "proxy recipe: $f"; fi
}

do_install() {
  parse "$@"; need_root
  say "components: $COMPONENTS   listens on $HOST:$PORT   program $PREFIX   data $DATA"
  if [ "$YES" = 0 ] && [ "$DRY" = 0 ] && [ -t 0 ]; then read -r -p "go on? [y/N] " a; [ "$a" = y ] || [ "$a" = Y ] || die "stopped"; fi
  id "$RUN_USER" >/dev/null 2>&1 || run useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$RUN_USER"
  copy_program; vendored_tools; make_venv; engine_bin; strata_bin; write_config; write_unit; write_proxy
  run chown -R "$RUN_USER": "$DATA"; run chmod 640 "$CONF/tadamoo.env"; run chgrp "$RUN_USER" "$CONF/tadamoo.env"
  run systemctl daemon-reload; run systemctl enable tadamoo
  [ "$START" = 1 ] && run systemctl restart tadamoo
  say "done. status: systemctl status tadamoo   page: http://$HOST:$PORT/"
}

do_uninstall() {
  parse "$@"; need_root
  run systemctl disable --now tadamoo || true
  run rm -f /etc/systemd/system/tadamoo.service; run systemctl daemon-reload
  run rm -rf "$PREFIX" "$CONF"
  [ "$PURGE" = 1 ] && run rm -rf "$DATA" || say "the data in $DATA is kept (--purge removes it)"
}

do_status() { systemctl status tadamoo --no-pager 2>&1 | sed -n 1,12p; curl -s -m 5 "http://127.0.0.1:${PORT}/health" || true; echo; }

case "$CMD" in
  install|update) do_install "$@";;
  uninstall) do_uninstall "$@";;
  status) parse "$@"; do_status;;
  ""|-h|--help|help) usage;;
  *) die "unknown command $CMD (install, update, uninstall, status)";;
esac
