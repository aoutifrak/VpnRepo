#!/usr/bin/env bash
set -euo pipefail

# vpnMan bootstrap installer
# - Installs dependencies (Debian/Ubuntu)
# - Optionally clones repo from --repo or uses local directory
# - Copies .ovpn/.conf files from --ovpn (dir or URL .zip/.tar.gz)
# - Sets up venv and systemd service
# - Starts API on 127.0.0.1:PORT
#
# Usage examples:
#   sudo ./install.sh --ovpn /root/ovpn --port 8080
#   sudo ./install.sh --repo https://github.com/your/repo.git --ovpn https://example.com/configs.zip
#   sudo ./install.sh --src /home/user/vpnMan --ovpn /home/user/ovpn --configs-dir /etc/vpn/configs --port 9090

REPO=""
SRC=""
OVPN_SRC=""
CONFIGS_DIR="/etc/vpn/configs"
PORT="8080"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      REPO="$2"; shift 2 ;;
    --src)
      SRC="$2"; shift 2 ;;
    --ovpn)
      OVPN_SRC="$2"; shift 2 ;;
    --configs-dir)
      CONFIGS_DIR="$2"; shift 2 ;;
    --port)
      PORT="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--repo <git_url> | --src <local_dir>] --ovpn <dir|url> [--configs-dir /etc/vpn/configs] [--port 8080]";
      exit 0 ;;
    *)
      echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$REPO" && -z "$SRC" ]]; then
  # default to current script directory as source
  SRC="$(cd "$(dirname "$0")" && pwd)"
fi

APP_ROOT="/opt/vpnMan"
APP_DIR="$APP_ROOT/app"
VENV_DIR="$APP_ROOT/.venv"
UNIT_PATH="/etc/systemd/system/vpnMan.service"

log() { echo "[+] $*"; }
err() { echo "[!] $*" >&2; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || return 1
}

install_pkgs() {
  if require_cmd apt-get; then
    log "Installing packages via apt..."
    apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
      python3 python3-venv python3-pip git curl rsync unzip tar docker.io
  else
    err "Unsupported distro: please install docker, python3-venv, git, curl, rsync, unzip, tar manually."
  fi
}

setup_source() {
  mkdir -p "$APP_DIR"
  if [[ -n "$REPO" ]]; then
    log "Cloning repo $REPO ..."
    tmpdir=$(mktemp -d)
    git clone --depth=1 "$REPO" "$tmpdir"
    rsync -a --delete "$tmpdir"/vpnMan/ "$APP_DIR"/
    rm -rf "$tmpdir"
  else
    log "Copying source from $SRC ..."
    rsync -a --delete "$SRC"/ "$APP_DIR"/
  fi
}

copy_ovpn() {
  mkdir -p "$CONFIGS_DIR"
  if [[ -z "$OVPN_SRC" ]]; then
    log "No --ovpn provided. Skipping OVPN copy. Ensure configs exist in $CONFIGS_DIR"
    return 0
  fi
  if [[ "$OVPN_SRC" =~ ^https?:// ]]; then
    log "Downloading OVPN archive from $OVPN_SRC ..."
    tmpd=$(mktemp -d)
    cd "$tmpd"
    fname="download"
    curl -fsSL "$OVPN_SRC" -o "$fname"
    file "$fname" | grep -qi zip && unzip -o "$fname" -d extracted || true
    file "$fname" | grep -qi gzip && tar -xzf "$fname" -C "$tmpd" || true
    file "$fname" | grep -qi tar && tar -xf "$fname" -C "$tmpd" || true
    if compgen -G "extracted/*" > /dev/null; then srcdir="extracted"; else srcdir="$tmpd"; fi
    find "$srcdir" -type f \( -name '*.ovpn' -o -name '*.conf' \) -print -exec cp -v {} "$CONFIGS_DIR" \;
    cd - >/dev/null
    rm -rf "$tmpd"
  else
    log "Copying OVPN from directory $OVPN_SRC ..."
    cp -v "$OVPN_SRC"/*.ovpn "$CONFIGS_DIR" 2>/dev/null || true
    cp -v "$OVPN_SRC"/*.conf "$CONFIGS_DIR" 2>/dev/null || true
  fi
  chmod 755 "$CONFIGS_DIR"
  chmod 644 "$CONFIGS_DIR"/* 2>/dev/null || true
}

setup_venv() {
  log "Creating venv and installing requirements..."
  python3 -m venv "$VENV_DIR"
  source "$VENV_DIR"/bin/activate
  pip install --upgrade pip
  pip install -r "$APP_DIR"/requirements.txt
  deactivate
}

setup_systemd() {
  log "Writing systemd unit $UNIT_PATH ..."
  cat > "$UNIT_PATH" <<EOF
[Unit]
Description=vpnMan FastAPI service
After=network-online.target docker.service
Requires=docker.service

[Service]
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/uvicorn main:app --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=5
User=root
Group=root

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now vpnMan.service
}

main() {
  install_pkgs
  setup_source
  copy_ovpn
  setup_venv
  setup_systemd
  log "vpnMan installed. API: http://127.0.0.1:$PORT"
  log "Configs directory: $CONFIGS_DIR"
}

main "$@"
