#!/usr/bin/env bash
# Installer for the LiteLLM proxy that sits beside gpu-lease on the lease host.
#
#     sudo ./litellm/setup.sh
#
# Idempotent: re-run it after a `git pull` or after changing LITELLM_VERSION.
# It never overwrites litellm/litellm.env or the database. Separate from the
# top-level setup.sh on purpose -- the two services share a host and Caddy,
# nothing else.
set -euo pipefail

LITELLM_VERSION=1.105.1

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_GROUP="$(id -gn "$RUN_USER")"
UNIT=/etc/systemd/system/litellm.service
ENV_FILE="$DIR/litellm.env"

[ "$(id -u)" -eq 0 ] || { echo "run this with sudo: sudo ./litellm/setup.sh" >&2; exit 1; }
[ "$RUN_USER" != "root" ] || echo "warning: installing as root; the service will run as root too"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
asuser() { sudo -u "$RUN_USER" "$@"; }

say "Installing system packages"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip postgresql openssl curl

say "Configuration"
if [ ! -f "$ENV_FILE" ]; then
  # Created 600 before anything is written to it.
  install -m 600 -o "$RUN_USER" -g "$RUN_GROUP" /dev/null "$ENV_FILE"
  DB_PASSWORD="$(openssl rand -hex 24)"
  cat > "$ENV_FILE" <<EOF
LITELLM_MASTER_KEY=sk-$(openssl rand -hex 32)
LITELLM_SALT_KEY=sk-$(openssl rand -hex 32)
DATABASE_URL=postgresql://litellm:${DB_PASSWORD}@127.0.0.1:5432/litellm
LITELLM_LOCAL_MODEL_COST_MAP=True
EOF
  echo "created $ENV_FILE with fresh keys. Back it up; it cannot be regenerated."
else
  echo "keeping existing $ENV_FILE"
fi
DB_PASSWORD="$(sed -n 's|^DATABASE_URL=postgresql://litellm:\([^@]*\)@.*|\1|p' "$ENV_FILE")"
[ -n "$DB_PASSWORD" ] || { echo "no litellm password in DATABASE_URL in $ENV_FILE" >&2; exit 1; }

say "Database"
systemctl enable --quiet --now postgresql
# Role and database are created once; the password is (re)applied every run so
# the role always matches litellm.env.
cd /tmp
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='litellm'" | grep -q 1; then
  sudo -u postgres psql -q -c "CREATE ROLE litellm LOGIN"
fi
sudo -u postgres psql -q -c "ALTER ROLE litellm PASSWORD '${DB_PASSWORD}'"
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='litellm'" | grep -q 1; then
  sudo -u postgres createdb -O litellm litellm
fi
cd "$DIR"

say "Installing LiteLLM ${LITELLM_VERSION}"
[ -d "$DIR/.venv" ] || asuser python3 -m venv "$DIR/.venv"
asuser "$DIR/.venv/bin/pip" install --quiet --upgrade pip
# extra_proxy brings the prisma client the database layer needs; [proxy] alone
# starts and then dies on "No module named 'prisma'".
asuser "$DIR/.venv/bin/pip" install --quiet "litellm[proxy,extra_proxy]==${LITELLM_VERSION}"
# Generate the client for this version's schema. Re-run on every install so an
# upgrade never runs against the previous version's client. Downloads the
# prisma engines (~150 MB) into ~/.cache/prisma-python on first run.
SCHEMA="$(asuser "$DIR/.venv/bin/python" -c \
  'import litellm, pathlib; print(pathlib.Path(litellm.__file__).parent / "proxy" / "schema.prisma")')"
(cd /tmp && asuser env PATH="$DIR/.venv/bin:$PATH" "$DIR/.venv/bin/prisma" generate --schema "$SCHEMA" >/dev/null)

say "Installing the systemd service"
cat > "$UNIT" <<UNITEOF
[Unit]
Description=LiteLLM proxy (Bedrock, per-group budgets)
After=network-online.target postgresql.service
Wants=network-online.target
Requires=postgresql.service

[Service]
User=${RUN_USER}
WorkingDirectory=${DIR}
EnvironmentFile=${ENV_FILE}
# The startup migrations shell out to the prisma CLI in the venv.
Environment=PATH=${DIR}/.venv/bin:/usr/local/bin:/usr/bin:/bin
# Keys are managed with keys.py; nothing needs the admin UI's login, and Caddy
# does not route to /ui anyway.
Environment=DISABLE_ADMIN_UI=True
# Localhost only: Caddy terminates TLS in front and forwards the student paths.
ExecStart=${DIR}/.venv/bin/litellm --config ${DIR}/config.yaml \\
    --host 127.0.0.1 --port 4000 --num_workers 1
Restart=always
RestartSec=5

# Holds the master key and calls Bedrock with the host's role.
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
systemctl enable --quiet litellm
systemctl restart litellm

# First start runs the database migrations, which takes a while.
say "Waiting for the proxy"
for _ in $(seq 1 60); do
  if curl -fs --max-time 2 http://127.0.0.1:4000/health/readiness >/dev/null; then
    say "Running"
    cat <<EOF

  logs         journalctl -u litellm -f
  restart      sudo systemctl restart litellm
  readiness    curl -s http://127.0.0.1:4000/health/readiness

  Listening on 127.0.0.1:4000 only. Next:
    1. Caddy: the llm.* site in Caddyfile.example -> /etc/caddy/Caddyfile,
       then sudo systemctl reload caddy
    2. ./litellm/keys.py mint --budget <USD per group>   # -> llm-tokens.csv
EOF
    exit 0
  fi
  if ! systemctl is-active --quiet litellm && [ "$(systemctl show -p NRestarts --value litellm)" -gt 3 ]; then
    break
  fi
  sleep 5
done

say "Proxy did not become ready"
journalctl -u litellm -n 40 --no-pager
exit 1
