#!/usr/bin/env bash
# deploy.sh – Deploy solar-management API service
# Run with: sudo ./deploy.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Help ──────────────────────────────────────────────────────────────────────

for arg in "$@"; do
    case "$arg" in
        -h|--help)
            cat <<'EOF'
Usage: sudo ./deploy.sh

One-time deployment for solar-management. Steps performed:
  1. Install system packages  (nmap curl python3 python3-pip)
  2. Install Python packages  (from requirements.txt)
  3. Discover devices on LAN and generate config.cfg
  4. Generate solar-management.service (project root)
  5. Install + enable service in /etc/systemd/system/
  6. daemon-reload
  7. Start service
  8. Health check: curl localhost:<port>/api/status

Safe to re-run – all steps are idempotent.
EOF
            exit 0
            ;;
    esac
done

# ── Helpers ───────────────────────────────────────────────────────────────────

ok()   { printf "  \033[32m✓\033[0m  %s\n" "$*"; }
fail() { printf "  \033[31m✗\033[0m  %s\n" "$*" >&2; }
step() { printf "\n\033[1;36m══ %s\033[0m\n" "$*"; }
die()  { fail "$*"; exit 1; }

# Read a single value from an INI file: ini_get <file> <section> <key>
ini_get() {
    awk -F' *= *' -v sec="[$2]" -v k="$3" '
        /^\[/ { in_sec = ($0 == sec) }
        in_sec && $1 == k { print $2; exit }
    ' "$1"
}

# ── Sudo guard ────────────────────────────────────────────────────────────────

[[ $EUID -eq 0 ]] || die "Must run as root:  sudo $0"
SERVICE_USER="${SUDO_USER:-$USER}"

# ── Step 1: System packages ───────────────────────────────────────────────────

step "1/8  System packages"
APT_PKGS=(nmap curl python3 python3-pip)
MISSING_APT=()
for pkg in "${APT_PKGS[@]}"; do
    dpkg -s "$pkg" &>/dev/null || MISSING_APT+=("$pkg")
done

if (( ${#MISSING_APT[@]} )); then
    echo "  Installing: ${MISSING_APT[*]}"
    apt-get install -y "${MISSING_APT[@]}" 2>&1 \
        | grep -E '^(Get:|Unpacking|Setting up|Processing)' \
        | sed 's/^/    /' || true
    ok "Installed: ${MISSING_APT[*]}"
else
    ok "All apt packages already present"
fi

# ── Step 2: Python packages ───────────────────────────────────────────────────

step "2/8  Python packages"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"
[[ -f "$REQUIREMENTS" ]] || die "requirements.txt not found at $REQUIREMENTS"

MISSING_PY=()
while IFS= read -r req; do
    [[ -z "$req" || "$req" == \#* ]] && continue
    pkg=$(echo "$req" | sed 's/[>=<!].*//')
    python3 -c "import $pkg" &>/dev/null || MISSING_PY+=("$req")
done < "$REQUIREMENTS"

if (( ${#MISSING_PY[@]} )); then
    echo "  Installing: ${MISSING_PY[*]}"
    pip install -q "${MISSING_PY[@]}" --break-system-packages || die "pip install failed"
    ok "Installed: ${MISSING_PY[*]}"
else
    ok "All Python packages already present"
fi

# ── Step 3: Discover + generate config ────────────────────────────────────────

step "3/8  Discover devices & generate config"
DISCOVER_OUT=$("$SCRIPT_DIR/discover.sh" --generate-config 2>&1) \
    || { echo "$DISCOVER_OUT"; die "discover.sh failed"; }
echo "$DISCOVER_OUT"

# Determine what was actually generated this run
GENERATED_SOLIS=0; GENERATED_DEYE=0
grep -q "Generated solis/config.cfg" <<< "$DISCOVER_OUT" && GENERATED_SOLIS=1
grep -q "Generated deye/config.cfg"  <<< "$DISCOVER_OUT" && GENERATED_DEYE=1

(( GENERATED_SOLIS || GENERATED_DEYE )) \
    || die "No inverter found – cannot deploy"
ok "Config generated"

# ── Step 4: Generate service file ─────────────────────────────────────────────

step "4/8  Generate solar-management.service"

SOLIS_CFG="$SCRIPT_DIR/solis/config.cfg"
DEYE_CFG="$SCRIPT_DIR/deye/config.cfg"

if (( GENERATED_SOLIS )); then
    API_SCRIPT="$SCRIPT_DIR/solis/solis-api.py"
    API_WORKDIR="$SCRIPT_DIR/solis"
    API_PORT=$(ini_get "$SOLIS_CFG" SolisAPI port)
    API_PORT=${API_PORT:-5000}
    INVERTER_TYPE="Solis"
else
    API_SCRIPT="$SCRIPT_DIR/deye/deye-api.py"
    API_WORKDIR="$SCRIPT_DIR/deye"
    API_PORT=$(ini_get "$DEYE_CFG" DeyeAPI port)
    API_PORT=${API_PORT:-5000}
    INVERTER_TYPE="Deye"
fi

SERVICE_FILE="$SCRIPT_DIR/solar-management.service"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Solar Management API (${INVERTER_TYPE})
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${API_WORKDIR}
ExecStart=/usr/bin/python3 ${API_SCRIPT}
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=solar-management

[Install]
WantedBy=multi-user.target
EOF

ok "solar-management.service written (${INVERTER_TYPE} API, port ${API_PORT}, user ${SERVICE_USER})"

# ── Step 5: Install + enable ──────────────────────────────────────────────────

step "5/8  Install service"
cp "$SERVICE_FILE" /etc/systemd/system/solar-management.service
systemctl enable solar-management --quiet
ok "Installed and enabled /etc/systemd/system/solar-management.service"

# ── Step 6: daemon-reload ─────────────────────────────────────────────────────

step "6/8  daemon-reload"
systemctl daemon-reload
ok "systemd daemon reloaded"

# ── Step 7: Start service ─────────────────────────────────────────────────────

step "7/8  Start service"
systemctl restart solar-management
ok "solar-management started"

# ── Step 8: Health check ──────────────────────────────────────────────────────

step "8/8  Health check"
sleep 2
if curl -sf "http://localhost:${API_PORT}/api/status" >/dev/null; then
    ok "API responding on :${API_PORT}  →  http://localhost:${API_PORT}/api/status"
else
    fail "API did not respond on :${API_PORT}"
    echo "  Check logs:  journalctl -u solar-management -n 30" >&2
    exit 1
fi

printf "\n\033[32mDeployment complete.\033[0m\n"
