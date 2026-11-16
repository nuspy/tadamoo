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
HOST=127.0.0.1; PORT=7232; COMPONENTS=harness; PUBLIC=(); API_KEY=""; GENERATE_KEY=0; NO_WEB=0
BASALT_BIN=""; STRATA_DIR=""; BUILD_BASALT=0; MODEL=""; PROXY=none; START=1; DRY=0; YES=0; PURGE=0

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
  --no-web             serve only the API: no web page (for a server that other programs, the desktop app or the phone app use)
  --api-key KEY        the server key (optional)            --generate-key   make one and keep it in the configuration folder
  --engine-bin FILE    a built Basalt-Plus binary           --build-basalt   build it from this tree (CUDA toolkit and cmake needed)
  --strata-dir DIR     the folder of the Strata binaries
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
      --api-key) API_KEY="$2"; shift 2;;
      --generate-key) GENERATE_KEY=1; shift;;
      --engine-bin) BASALT_BIN="$2"; shift 2;;
      --build-basalt) BUILD_BASALT=1; shift;;
      --strata-dir) STRATA_DIR="$2"; shift 2;;
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
    run rsync -a --delete --exclude .git --exclude .venv --exclude __pycache__ --exclude tests --exclude build --exclude build-win --exclude dist --exclude .claude --exclude '*.exe' --exclude node_modules "$SRC"/ "$PREFIX"/program/
  else
    run rm -rf "$PREFIX/program"; run mkdir -p "$PREFIX/program"
    run bash -c "cd '$SRC' && tar -c --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=tests --exclude=build --exclude=build-win --exclude=dist --exclude=.claude --exclude='*.exe' --exclude=node_modules . | tar -x -C '$PREFIX/program'"
  fi
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
  elif [ "$BUILD_BASALT" = 1 ]; then
    command -v cmake >/dev/null 2>&1 || die "--build-basalt needs cmake and the CUDA toolkit"
    run cmake -S "$SRC" -B "$PREFIX/engines/basalt/build" -DCMAKE_BUILD_TYPE=Release
    run cmake --build "$PREFIX/engines/basalt/build" -j
    run mkdir -p "$PREFIX/engines/basalt/bin"; run install -m 0755 "$PREFIX/engines/basalt/build/bin/basalt" "$PREFIX/engines/basalt/bin/basalt"
  else
    die "basalt asked: give --engine-bin FILE (a built binary) or --build-basalt"
  fi
}

strata_bin() {
  has strata || return 0
  [ -n "$STRATA_DIR" ] || die "strata asked: give --strata-dir DIR (the folder of its binaries)"
  run mkdir -p "$PREFIX/engines/strata"; run cp -a "$STRATA_DIR"/. "$PREFIX/engines/strata/"
}

write_config() {
  run mkdir -p "$CONF" "$DATA"
  local hosts="" n
  for n in "${PUBLIC[@]+"${PUBLIC[@]}"}"; do hosts="$hosts\"$n\", "; done
  hosts="[${hosts%, }]"
  local web=""
  [ "$NO_WEB" = 1 ] && web=', "no-web": true'
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
  copy_program; make_venv; engine_bin; strata_bin; write_config; write_unit; write_proxy
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
