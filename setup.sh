#!/usr/bin/env bash
# One-shot installer for the gpu-lease control plane.
#
#     git clone <repo> gpulease && cd gpulease && sudo ./setup.sh
#
# Idempotent: run it again after a `git pull` to pick up code changes. It never
# overwrites gpulease.env or the database.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"   # so `python -m gpulease.*` below resolves regardless of where this was invoked
RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_GROUP="$(id -gn "$RUN_USER")"
UNIT=/etc/systemd/system/gpulease.service

[ "$(id -u)" -eq 0 ] || { echo "run this with sudo: sudo ./setup.sh" >&2; exit 1; }
[ "$RUN_USER" != "root" ] || echo "warning: installing as root; the service will run as root too"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
asuser() { sudo -u "$RUN_USER" "$@"; }

say "Installing system packages"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip openssh-client

say "Creating the virtualenv"
[ -d "$APP_DIR/.venv" ] || asuser python3 -m venv "$APP_DIR/.venv"
asuser "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
asuser "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

say "Configuration"
if [ ! -f "$APP_DIR/gpulease.env" ]; then
  install -m 600 -o "$RUN_USER" -g "$RUN_GROUP" \
    "$APP_DIR/gpulease.env.example" "$APP_DIR/gpulease.env"
  echo "created $APP_DIR/gpulease.env from the example."
  echo "The service will start on the example defaults; edit it and re-run this"
  echo "script (or just: sudo systemctl restart gpulease) to apply your own."
else
  echo "keeping existing $APP_DIR/gpulease.env"
fi

# Load it the same way systemd will, so the steps below agree with the service.
# Note that `sudo -u` scrubs the environment: the steps below find this config
# because gpulease/config.py reads gpulease.env itself as a fallback.
set -a
# shellcheck disable=SC1091
. "$APP_DIR/gpulease.env"
set +a

say "Database"
install -d -m 750 -o "$RUN_USER" -g "$RUN_GROUP" "$APP_DIR/var"
asuser "$APP_DIR/.venv/bin/python" -m gpulease.db init

say "Checking AWS permissions"
# Non-fatal: the service is still worth installing so you can fix IAM and
# restart, rather than having setup bail in the middle.
asuser "$APP_DIR/.venv/bin/python" -m gpulease.aws || \
  echo "^ fix the failures above (see iam-policy.json), then: sudo systemctl restart gpulease"

say "Installing the systemd service"
cat > "$UNIT" <<UNITEOF
[Unit]
Description=gpu-lease control plane
After=network-online.target
Wants=network-online.target

[Service]
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/gpulease.env
# One worker on purpose: the reaper runs as a thread in this process.
ExecStart=${APP_DIR}/.venv/bin/uvicorn gpulease.api:app \\
    --host ${GPULEASE_HOST:-127.0.0.1} --port ${GPULEASE_PORT:-8000} --workers 1
Restart=always
RestartSec=5

# This process holds every live session's SSH private key and speaks to AWS
# with the host's role. Cheap containment, nothing exotic.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectControlGroups=true
ProtectKernelTunables=true
RestrictSUIDSGID=true
UMask=0077

[Install]
WantedBy=multi-user.target
UNITEOF

systemctl daemon-reload
systemctl enable --quiet gpulease
systemctl restart gpulease
sleep 2

if systemctl is-active --quiet gpulease; then
  say "Running"
else
  say "Service failed to start"
  journalctl -u gpulease -n 30 --no-pager
  exit 1
fi

PORT="${GPULEASE_PORT:-8000}"
BIND="${GPULEASE_HOST:-127.0.0.1}"
IP=$(curl -s --max-time 2 -H "X-aws-ec2-metadata-token: $(
  curl -sX PUT --max-time 2 http://169.254.169.254/latest/api/token \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null)" \
  http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || true)

cat <<EOF

  logs         journalctl -u gpulease -f
  restart      sudo systemctl restart gpulease
  healthz      curl -s http://127.0.0.1:${PORT}/healthz
EOF

# The next steps differ entirely depending on whether this is reachable from
# the network yet, and printing the wrong set is how a course ends up serving
# tokens over plaintext for a semester.
if [ "$BIND" = "127.0.0.1" ] || [ "$BIND" = "localhost" ]; then
  cat <<EOF

  Listening on ${BIND}:${PORT} - not reachable from outside this host yet.
  That is deliberate: bearer tokens and the session SSH private keys students
  get back both cross this connection, so it belongs behind TLS.

  Next:
    1. Put Caddy in front of it. Point a DNS name at this host first, then see
       README -> "Put TLS in front" for the install, and:
         sudo cp Caddyfile.example /etc/caddy/Caddyfile
         sudo nano /etc/caddy/Caddyfile      # your DNS name
         sudo systemctl reload caddy
       Open 80 and 443 to your students. Do NOT open ${PORT}.
    2. ./admin.py roster roster.csv     # mints tokens.csv
    3. Give students cli/gpulease.py and tell them:
         export GPULEASE_API=https://<your-dns-name>
         python3 gpulease.py login <their-token>
EOF
else
  cat <<EOF

  API          http://${IP:-<this-host>}:${PORT}

  WARNING: bound to ${BIND}, so this is plain HTTP. Bearer tokens AND the
  session SSH private keys handed back by /session both cross the network in
  the clear, and anyone on-path can stop another group's session or log into
  their nodes. Use this only on a trusted network. To fix it, set
  GPULEASE_HOST=127.0.0.1 and see README -> "Put TLS in front".

  Next:
    1. Open port ${PORT} to your students in this host's security group.
    2. ./admin.py roster roster.csv     # mints tokens.csv
    3. Give students cli/gpulease.py and tell them:
         export GPULEASE_API=http://${IP:-<this-host>}:${PORT}
         python3 gpulease.py login <their-token>
EOF
fi
