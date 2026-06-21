#!/usr/bin/env bash
# discover.sh – Find Deye/Solis inverters and Battery Emulators on the LAN
#
# Usage:
#   ./discover.sh                             # auto-detect local subnet
#   ./discover.sh 192.168.22.0/24            # explicit subnet
#   ./discover.sh 192.168.22.0/24 5          # explicit subnet + timeout (seconds)
#   ./discover.sh --generate-config          # scan + write solis/config.cfg and deye/config.cfg
#   ./discover.sh 192.168.22.0/24 --generate-config

set -euo pipefail

# ── Help (before anything else) ───────────────────────────────────────────────

for arg in "$@"; do
    case "$arg" in
        -h|--help)
            cat <<'EOF'
Usage: discover.sh [SUBNET] [TIMEOUT] [OPTIONS]

  SUBNET            CIDR to scan (default: auto-detect from default route)
  TIMEOUT           Probe timeout in seconds (default: 3)
  --generate-config After scanning, write solis/config.cfg and deye/config.cfg
                    from the .example files, filling in the discovered IP and SN
  -h, --help        Show this help

Examples:
  ./discover.sh
  ./discover.sh 192.168.22.0/24
  ./discover.sh 192.168.22.0/24 5
  ./discover.sh --generate-config
  ./discover.sh 192.168.22.0/24 --generate-config

Detected devices:
  port 8899  →  Deye datalogger   (SolarmanV5, logger SN auto-discovered)
  port 502   →  Solis inverter    (Modbus TCP, serial read from registers)
  port 80    →  Battery Emulator  (confirmed by HTML title)

Dependencies: nmap, curl, python3 + pip packages: pymodbus, pysolarmanv5, jinja2
EOF
            exit 0
            ;;
    esac
done

# ── Dependency check ──────────────────────────────────────────────────────────

missing=0

check_cmd() {
    if ! command -v "$1" &>/dev/null; then
        echo "  MISSING: $1" >&2
        echo "  $2" >&2
        missing=1
    fi
}

check_py() {
    if ! python3 -c "import $1" &>/dev/null; then
        echo "  MISSING python package: $1" >&2
        echo "  pip install $2" >&2
        missing=1
    fi
}

echo "Checking dependencies …" >&2
check_cmd nmap      "sudo apt install nmap"
check_cmd curl      "sudo apt install curl"
check_cmd python3   "sudo apt install python3"
if ! python3 -c "import pymodbus, pysolarmanv5, jinja2" &>/dev/null; then
    python3 -c "
import sys
for pkg, inst in [('pymodbus','pymodbus'),('pysolarmanv5','pysolarmanv5'),('jinja2','jinja2')]:
    try: __import__(pkg)
    except ImportError:
        print(f'  MISSING python package: {pkg}', file=sys.stderr)
        print(f'  pip install {inst}', file=sys.stderr)
" >&2
    missing=1
fi

if (( missing )); then
    echo "" >&2
    echo "Fix the above and re-run." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOLIS_MONITOR="$SCRIPT_DIR/solis/solis-monitor.py"
DEYE_MONITOR="$SCRIPT_DIR/deye/deye-monitor.py"

# ── Argument parsing ──────────────────────────────────────────────────────────

GENERATE_CONFIG=0
POSITIONAL=()
for arg in "$@"; do
    case "$arg" in
        --generate-config) GENERATE_CONFIG=1 ;;
        -h|--help) ;; # already handled above
        *) POSITIONAL+=("$arg") ;;
    esac
done

SUBNET="${POSITIONAL[0]:-}"
TIMEOUT="${POSITIONAL[1]:-3}"

# ── Subnet auto-detect ────────────────────────────────────────────────────────

if [[ -z "$SUBNET" ]]; then
    # Follow the default-route interface so VPNs / secondary interfaces are ignored
    DEFAULT_IFACE=$(ip route show default 2>/dev/null \
        | awk 'NR==1 {for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}')
    SUBNET=$(ip route show scope link dev "$DEFAULT_IFACE" 2>/dev/null \
        | grep -v '^169\.254\.' \
        | awk 'NR==1 && /\// {print $1}')
    [[ -z "$SUBNET" ]] && { echo "ERROR: could not detect local subnet" >&2; exit 1; }
fi

# ── Helpers ───────────────────────────────────────────────────────────────────

get_mac() {
    ip neigh show "$1" 2>/dev/null \
        | grep -oE '([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}' \
        | head -1 \
        | tr 'a-f' 'A-F' \
        || echo "unknown"
}

# ── Probes ────────────────────────────────────────────────────────────────────

probe_deye() {
    timeout 3 python3 "$DEYE_MONITOR" --ip "$1" --show-serial 2>/dev/null || echo "unknown"
}

probe_solis() {
    timeout 3 python3 "$SOLIS_MONITOR" --ip "$1" --port "$2" --show-serial 2>/dev/null || echo "unknown"
}

probe_battery_emulator() {
    local ip="$1" mac="$2"
    local html
    html=$(curl -sf --max-time "$TIMEOUT" "http://${ip}/" 2>/dev/null) || return 1
    grep -q '<title>Battery Emulator</title>' <<< "$html" || return 1

    local sw hw soc inv_proto bat_proto
    sw=$(        grep -oP 'Software:\s*\K[\d.a-z]+'     <<< "$html" | head -1)
    hw=$(        grep -oP 'Hardware:\s*\K[^@<]+'        <<< "$html" | head -1 | xargs)
    soc=$(       grep -oP 'Scaled SOC:\s*\K[\d.]+'      <<< "$html" | head -1)
    inv_proto=$( grep -oP 'Inverter protocol:\s*\K[^<]+' <<< "$html" | head -1 | xargs)
    bat_proto=$( grep -oP 'Battery protocol:\s*\K[^<]+'  <<< "$html" | head -1 | xargs)

    echo "┌─ Battery Emulator ───────────────────────────────"
    printf "│  IP         : %s\n"  "$ip"
    printf "│  MAC        : %s\n"  "$mac"
    [[ -n "$sw"        ]] && printf "│  Software   : %s\n"   "$sw"
    [[ -n "$hw"        ]] && printf "│  Hardware   : %s\n"   "$hw"
    [[ -n "$soc"       ]] && printf "│  SOC        : %s%%\n" "$soc"
    [[ -n "$inv_proto" ]] && printf "│  Inv proto  : %s\n"   "$inv_proto"
    [[ -n "$bat_proto" ]] && printf "│  Bat proto  : %s\n"   "$bat_proto"
    echo "└──────────────────────────────────────────────────"
}

# ── Scan + probe ──────────────────────────────────────────────────────────────

echo "Scanning $SUBNET …" >&2

found=0
# First device of each type with a valid SN – used by --generate-config
SOLIS_IP="" SOLIS_SERIAL=""
DEYE_IP=""  DEYE_SN=""

while IFS= read -r line; do
    # nmap -oG emits two lines per host; we only want the one with port info
    [[ "$line" != Host:* ]]  && continue
    [[ "$line" != *Ports:* ]] && continue

    IP=$(  awk '{print $2}'            <<< "$line")
    PORTS=$(grep -oE '[0-9]+/open'    <<< "$line" | grep -oE '^[0-9]+' | tr '\n' ' ')
    MAC=$(get_mac "$IP")

    if grep -qw 8899 <<< "$PORTS"; then
        SN=$(probe_deye "$IP")
        echo "┌─ Deye Inverter ──────────────────────────────────"
        printf "│  IP         : %s\n" "$IP"
        printf "│  MAC        : %s\n" "$MAC"
        printf "│  Logger SN  : %s\n" "$SN"
        echo "└──────────────────────────────────────────────────"
        found=1
        [[ -z "$DEYE_IP" && "$SN" != "unknown" ]] && { DEYE_IP="$IP"; DEYE_SN="$SN"; }
    fi

    if grep -qw 502 <<< "$PORTS"; then
        SERIAL=$(probe_solis "$IP" 502)
        echo "┌─ Solis Inverter ─────────────────────────────────"
        printf "│  IP         : %s\n" "$IP"
        printf "│  MAC        : %s\n" "$MAC"
        printf "│  Serial     : %s\n" "$SERIAL"
        echo "└──────────────────────────────────────────────────"
        found=1
        [[ -z "$SOLIS_IP" && "$SERIAL" != "unknown" ]] && { SOLIS_IP="$IP"; SOLIS_SERIAL="$SERIAL"; }
    fi

    if grep -qw 80 <<< "$PORTS"; then
        probe_battery_emulator "$IP" "$MAC" && found=1 || true
    fi

done < <(nmap -p 80,502,8899 --open -T4 -oG - "$SUBNET" 2>/dev/null)

(( found )) || echo "No inverters or Battery Emulators found."

# ── Config generation ─────────────────────────────────────────────────────────

if (( GENERATE_CONFIG )); then
    generated=0

    if [[ -n "$SOLIS_IP" ]]; then
        src="$SCRIPT_DIR/solis/config.cfg.example"
        dst="$SCRIPT_DIR/solis/config.cfg"
        sed \
            -e "s|^inverter_ip = .*|inverter_ip = $SOLIS_IP|" \
            -e "s|^serial = .*|serial = $SOLIS_SERIAL|" \
            "$src" > "$dst"
        echo "Generated solis/config.cfg  (IP: $SOLIS_IP  serial: $SOLIS_SERIAL)"
        generated=1
    else
        echo "WARNING: no Solis inverter with valid serial found – config not generated" >&2
    fi

    if [[ -n "$DEYE_IP" ]]; then
        src="$SCRIPT_DIR/deye/config.cfg.example"
        dst="$SCRIPT_DIR/deye/config.cfg"
        sed \
            -e "s|^inverter_ip = .*|inverter_ip = $DEYE_IP|" \
            -e "s|^inverter_sn = .*|inverter_sn = $DEYE_SN|" \
            "$src" > "$dst"
        echo "Generated deye/config.cfg   (IP: $DEYE_IP  SN: $DEYE_SN)"
        generated=1
    else
        echo "WARNING: no Deye inverter with valid SN found – config not generated" >&2
    fi

    (( generated )) || echo "No configs were generated."
fi
