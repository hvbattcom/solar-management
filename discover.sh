#!/usr/bin/env bash
# discover.sh – Find Deye/Solis inverters and Battery Emulators on the LAN
#
# Usage:
#   ./discover.sh                             # auto-detect local subnet
#   ./discover.sh 192.168.22.0/24            # explicit subnet
#   ./discover.sh 192.168.22.0/24 5          # explicit subnet + timeout (seconds)
#   ./discover.sh --generate-config          # scan + write solis/config.cfg and deye/config.cfg
#   ./discover.sh 192.168.22.0/24 --generate-config
#   ./discover.sh --generate-config --from-file  # skip the scan, use found.yaml
#
# Every run merges its results into found.yaml, so re-running after a scan
# that only found one device fills in the other without losing what's already
# known. deploy.sh reads found.yaml instead of re-scanning.

set -euo pipefail

# ── Help (before anything else) ───────────────────────────────────────────────

for arg in "$@"; do
    case "$arg" in
        -h|--help)
            cat <<'EOF'
Usage: discover.sh [SUBNET] [TIMEOUT] [OPTIONS]

  SUBNET            CIDR to scan (default: auto-detect from default route)
  TIMEOUT           Probe timeout in seconds (default: 8 – must be at least
                    the monitors' own ~5s connect timeout, or probes get
                    killed before the device can respond)
  --generate-config After scanning, write solis/config.cfg and deye/config.cfg
                    from the .example files, filling in the discovered IP and SN
  --from-file       Skip the network scan; reuse whatever is already recorded
                    in found.yaml (combine with --generate-config to (re)write
                    config.cfg files without re-scanning)
  --rated-power-kw=N   With --generate-config, also set inverter_power_kw in
                        whichever config.cfg gets generated (both brands).
  --mppt-count=N        Same, but mppt_count -- Solis config only; Deye
                        auto-detects this from a hardware register instead.
  --selling-enabled=true|false
                        Same, but selling_enabled (both brands) -- whether
                        this plant sells energy back to the grid.
  -h, --help        Show this help

Every run (scan or --from-file) merges results into found.yaml: a device found
today is kept on record even if a later scan misses it, so a flaky scan that
only spots the inverter (or only the battery) doesn't lose the other one.
deploy.sh reads found.yaml to decide what to deploy.

Examples:
  ./discover.sh
  ./discover.sh 192.168.22.0/24
  ./discover.sh 192.168.22.0/24 5
  ./discover.sh --generate-config
  ./discover.sh 192.168.22.0/24 --generate-config
  ./discover.sh --generate-config --from-file

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
FOUND_FILE="$SCRIPT_DIR/found.yaml"

# ── Argument parsing ──────────────────────────────────────────────────────────

GENERATE_CONFIG=0
FROM_FILE=0
RATED_POWER_KW=""
MPPT_COUNT=""
SELLING_ENABLED=""
POSITIONAL=()
for arg in "$@"; do
    case "$arg" in
        --generate-config) GENERATE_CONFIG=1 ;;
        --from-file) FROM_FILE=1 ;;
        --rated-power-kw=*) RATED_POWER_KW="${arg#*=}" ;;
        --mppt-count=*) MPPT_COUNT="${arg#*=}" ;;
        --selling-enabled=*) SELLING_ENABLED="${arg#*=}" ;;
        -h|--help) ;; # already handled above
        *) POSITIONAL+=("$arg") ;;
    esac
done

SUBNET="${POSITIONAL[0]:-}"
TIMEOUT="${POSITIONAL[1]:-8}"

# ── found.yaml helpers ─────────────────────────────────────────────────────────

# yaml_get <file> <key> – read a "key: value" (optionally quoted) line
yaml_get() {
    local file="$1" key="$2"
    [[ -f "$file" ]] || return 0
    grep "^${key}: " "$file" 2>/dev/null | head -1 \
        | sed -e "s/^${key}: //" -e 's/^"//' -e 's/"$//' || true
}

# Batteries are recorded as numbered flat keys (battery_1_ip, battery_2_ip,
# ...) rather than a real YAML list, so yaml_get's plain grep can still read
# them back -- no yq/python-yaml dependency needed.
read_prev_batteries() {
    PREV_BATTERY_IPS=() PREV_BATTERY_MACS=() PREV_BATTERY_SEEN=()
    local count i
    count=$(yaml_get "$FOUND_FILE" battery_count)
    [[ "$count" =~ ^[0-9]+$ ]] || return 0
    for (( i = 1; i <= count; i++ )); do
        PREV_BATTERY_IPS+=("$(yaml_get "$FOUND_FILE" "battery_${i}_ip")")
        PREV_BATTERY_MACS+=("$(yaml_get "$FOUND_FILE" "battery_${i}_mac")")
        PREV_BATTERY_SEEN+=("$(yaml_get "$FOUND_FILE" "battery_${i}_last_seen")")
    done
}

# Sorts the FINAL_BATTERY_* arrays in place by IP (numeric per-octet, not
# lexical) -- batt_id in configurable-exporter is assigned by this order.
sort_final_batteries_by_ip() {
    local n=${#FINAL_BATTERY_IPS[@]}
    (( n > 1 )) || return 0
    local i order new_ips=() new_macs=() new_seen=()
    order=$(for (( i = 0; i < n; i++ )); do printf '%s %d\n' "${FINAL_BATTERY_IPS[$i]}" "$i"; done \
        | sort -t. -k1,1n -k2,2n -k3,3n -k4,4n | awk '{print $2}')
    while IFS= read -r i; do
        new_ips+=("${FINAL_BATTERY_IPS[$i]}")
        new_macs+=("${FINAL_BATTERY_MACS[$i]}")
        new_seen+=("${FINAL_BATTERY_SEEN[$i]}")
    done <<< "$order"
    FINAL_BATTERY_IPS=("${new_ips[@]}")
    FINAL_BATTERY_MACS=("${new_macs[@]}")
    FINAL_BATTERY_SEEN=("${new_seen[@]}")
}

# Previously recorded devices, if any – carried forward unless this run overrides them
PREV_SOLIS_IP=$(yaml_get "$FOUND_FILE" solis_ip)
PREV_SOLIS_SERIAL=$(yaml_get "$FOUND_FILE" solis_serial)
PREV_SOLIS_LAST_SEEN=$(yaml_get "$FOUND_FILE" solis_last_seen)
PREV_DEYE_IP=$(yaml_get "$FOUND_FILE" deye_ip)
PREV_DEYE_SN=$(yaml_get "$FOUND_FILE" deye_sn)
PREV_DEYE_LAST_SEEN=$(yaml_get "$FOUND_FILE" deye_last_seen)
read_prev_batteries

if (( FROM_FILE )) && [[ ! -f "$FOUND_FILE" ]]; then
    echo "ERROR: --from-file given but $FOUND_FILE does not exist yet — run ./discover.sh at least once first" >&2
    exit 1
fi

# ── Subnet auto-detect ────────────────────────────────────────────────────────

if (( ! FROM_FILE )) && [[ -z "$SUBNET" ]]; then
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
# solis-monitor.py / deye-monitor.py connect with a 5s internal timeout (plus a
# retry on connection failure), so wrapping them in a shorter outer `timeout`
# can kill a probe before the device gets a fair chance to respond. Use the
# script's own $TIMEOUT (not a hardcoded value shorter than that) and retry
# once more here in case of a transient miss.

probe_deye() {
    local ip="$1" result
    for _ in 1 2; do
        result=$(timeout "$TIMEOUT" python3 "$DEYE_MONITOR" --ip "$ip" --show-serial 2>/dev/null)
        [[ -n "$result" && "$result" != "unknown" ]] && { echo "$result"; return; }
    done
    echo "unknown"
}

probe_solis() {
    local ip="$1" port="$2" result
    for _ in 1 2; do
        result=$(timeout "$TIMEOUT" python3 "$SOLIS_MONITOR" --ip "$ip" --port "$port" --show-serial 2>/dev/null)
        [[ -n "$result" && "$result" != "unknown" ]] && { echo "$result"; return; }
    done
    echo "unknown"
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

found=0
# First device of each type seen *this run* – "" means not seen this run
SOLIS_IP="" SOLIS_SERIAL=""
DEYE_IP=""  DEYE_SN=""
# Every battery emulator seen this run (there can be more than one)
BATTERY_IPS=() BATTERY_MACS=()

if (( ! FROM_FILE )); then
    echo "Scanning $SUBNET …" >&2

    while IFS= read -r line; do
        # nmap -oG emits two lines per host; we only want the one with port info
        [[ "$line" != Host:* ]]  && continue
        [[ "$line" != *Ports:* ]] && continue

        IP=$(  awk '{print $2}'            <<< "$line")
        PORTS=$(grep -oE '[0-9]+/open'    <<< "$line" | grep -oE '^[0-9]+' | tr '\n' ' ')
        MAC=$(get_mac "$IP")

        if grep -qw 8899 <<< "$PORTS"; then
            SN=$(probe_deye "$IP")
            if [[ "$SN" != "unknown" ]]; then
                echo "┌─ Deye Inverter ──────────────────────────────────"
                printf "│  IP         : %s\n" "$IP"
                printf "│  MAC        : %s\n" "$MAC"
                printf "│  Logger SN  : %s\n" "$SN"
                echo "└──────────────────────────────────────────────────"
                found=1
                [[ -z "$DEYE_IP" ]] && { DEYE_IP="$IP"; DEYE_SN="$SN"; }
            else
                echo "  (port 8899 open on $IP but no valid Solarman logger responded – skipped)" >&2
            fi
        fi

        if grep -qw 502 <<< "$PORTS"; then
            SERIAL=$(probe_solis "$IP" 502)
            if [[ "$SERIAL" != "unknown" ]]; then
                echo "┌─ Solis Inverter ─────────────────────────────────"
                printf "│  IP         : %s\n" "$IP"
                printf "│  MAC        : %s\n" "$MAC"
                printf "│  Serial     : %s\n" "$SERIAL"
                echo "└──────────────────────────────────────────────────"
                found=1
                [[ -z "$SOLIS_IP" ]] && { SOLIS_IP="$IP"; SOLIS_SERIAL="$SERIAL"; }
            else
                echo "  (port 502 open on $IP but no valid Solis serial responded – skipped, possibly a non-Solis Modbus device)" >&2
            fi
        fi

        if grep -qw 80 <<< "$PORTS"; then
            if probe_battery_emulator "$IP" "$MAC"; then
                found=1
                BATTERY_IPS+=("$IP")
                BATTERY_MACS+=("$MAC")
            fi
        fi

    done < <(nmap -p 80,502,8899 --open -T4 -oG - "$SUBNET" 2>/dev/null)

    (( found )) || echo "No inverters or Battery Emulators found this run."
fi

# ── Merge with found.yaml ──────────────────────────────────────────────────────
# A device missed by this run still counts if an earlier run recorded it, so a
# flaky scan that only spots one device doesn't wipe out the other one.

NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)

if (( FROM_FILE )); then
    FINAL_SOLIS_IP="$PREV_SOLIS_IP"; FINAL_SOLIS_SERIAL="$PREV_SOLIS_SERIAL"; FINAL_SOLIS_SEEN="$PREV_SOLIS_LAST_SEEN"
    FINAL_DEYE_IP="$PREV_DEYE_IP";   FINAL_DEYE_SN="$PREV_DEYE_SN";           FINAL_DEYE_SEEN="$PREV_DEYE_LAST_SEEN"
else
    if [[ -n "$SOLIS_IP" ]]; then
        FINAL_SOLIS_IP="$SOLIS_IP"; FINAL_SOLIS_SERIAL="$SOLIS_SERIAL"; FINAL_SOLIS_SEEN="$NOW"
    else
        FINAL_SOLIS_IP="$PREV_SOLIS_IP"; FINAL_SOLIS_SERIAL="$PREV_SOLIS_SERIAL"; FINAL_SOLIS_SEEN="$PREV_SOLIS_LAST_SEEN"
    fi

    if [[ -n "$DEYE_IP" ]]; then
        FINAL_DEYE_IP="$DEYE_IP"; FINAL_DEYE_SN="$DEYE_SN"; FINAL_DEYE_SEEN="$NOW"
    else
        FINAL_DEYE_IP="$PREV_DEYE_IP"; FINAL_DEYE_SN="$PREV_DEYE_SN"; FINAL_DEYE_SEEN="$PREV_DEYE_LAST_SEEN"
    fi

    # Batteries: if this scan found at least one, its full result *replaces*
    # the recorded list wholesale rather than merging with history -- so
    # swapping a battery's Arduino (new MAC, possibly new IP) just becomes
    # the new entry next scan, with no leftover ghost of the old one. Only a
    # scan that finds zero batteries falls back to what was already known
    # (protects against a one-off flaky scan, same as solis/deye above).
    if (( ${#BATTERY_IPS[@]} > 0 )); then
        FINAL_BATTERY_IPS=("${BATTERY_IPS[@]}")
        FINAL_BATTERY_MACS=("${BATTERY_MACS[@]}")
        FINAL_BATTERY_SEEN=()
        for _ in "${BATTERY_IPS[@]}"; do FINAL_BATTERY_SEEN+=("$NOW"); done
        sort_final_batteries_by_ip
    else
        FINAL_BATTERY_IPS=("${PREV_BATTERY_IPS[@]}")
        FINAL_BATTERY_MACS=("${PREV_BATTERY_MACS[@]}")
        FINAL_BATTERY_SEEN=("${PREV_BATTERY_SEEN[@]}")
    fi

    {
        echo "# Auto-generated by discover.sh — do not edit by hand"
        echo "scan_time: \"$NOW\""
        echo "subnet: \"$SUBNET\""
        echo ""
        echo "solis_found: $([[ -n "$FINAL_SOLIS_IP" ]] && echo true || echo false)"
        echo "solis_ip: \"$FINAL_SOLIS_IP\""
        echo "solis_serial: \"$FINAL_SOLIS_SERIAL\""
        echo "solis_last_seen: \"$FINAL_SOLIS_SEEN\""
        echo ""
        echo "deye_found: $([[ -n "$FINAL_DEYE_IP" ]] && echo true || echo false)"
        echo "deye_ip: \"$FINAL_DEYE_IP\""
        echo "deye_sn: \"$FINAL_DEYE_SN\""
        echo "deye_last_seen: \"$FINAL_DEYE_SEEN\""
        echo ""
        echo "battery_found: $([[ ${#FINAL_BATTERY_IPS[@]} -gt 0 ]] && echo true || echo false)"
        echo "battery_count: ${#FINAL_BATTERY_IPS[@]}"
        for (( i = 0; i < ${#FINAL_BATTERY_IPS[@]}; i++ )); do
            n=$((i + 1))
            echo "battery_${n}_ip: \"${FINAL_BATTERY_IPS[$i]}\""
            echo "battery_${n}_mac: \"${FINAL_BATTERY_MACS[$i]}\""
            echo "battery_${n}_last_seen: \"${FINAL_BATTERY_SEEN[$i]}\""
        done
    } > "$FOUND_FILE"

    echo "" >&2
    echo "Updated $FOUND_FILE:" >&2
    printf "  Solis inverter    : %s\n" "$([[ -n "$FINAL_SOLIS_IP"   ]] && echo "✓ $FINAL_SOLIS_IP" || echo "✗ not yet found")" >&2
    printf "  Deye inverter     : %s\n" "$([[ -n "$FINAL_DEYE_IP"    ]] && echo "✓ $FINAL_DEYE_IP"  || echo "✗ not yet found")" >&2
    if (( ${#FINAL_BATTERY_IPS[@]} > 0 )); then
        printf "  Battery Emulator  : ✓ %d found\n" "${#FINAL_BATTERY_IPS[@]}" >&2
        for (( i = 0; i < ${#FINAL_BATTERY_IPS[@]}; i++ )); do
            printf "    batt_id=%d  %s  (%s)\n" "$((i + 1))" "${FINAL_BATTERY_IPS[$i]}" "${FINAL_BATTERY_MACS[$i]}" >&2
        done
    else
        echo "  Battery Emulator  : ✗ not yet found" >&2
    fi
fi

# ── Config generation ─────────────────────────────────────────────────────────
# Uses the merged (found.yaml) results, so a device recorded on an earlier run
# still gets its config written even if this particular scan didn't see it.

if (( GENERATE_CONFIG )); then
    generated=0

    if [[ -n "$FINAL_SOLIS_IP" ]]; then
        src="$SCRIPT_DIR/solis/config.cfg.example"
        dst="$SCRIPT_DIR/solis/config.cfg"
        SOLIS_SED_ARGS=(
            -e "s|^inverter_ip = .*|inverter_ip = $FINAL_SOLIS_IP|"
            -e "s|^serial = .*|serial = $FINAL_SOLIS_SERIAL|"
        )
        [[ -n "$RATED_POWER_KW" ]] && SOLIS_SED_ARGS+=(-e "s|^inverter_power_kw = .*|inverter_power_kw = $RATED_POWER_KW|")
        [[ -n "$MPPT_COUNT" ]] && SOLIS_SED_ARGS+=(-e "s|^mppt_count = .*|mppt_count = $MPPT_COUNT|")
        [[ -n "$SELLING_ENABLED" ]] && SOLIS_SED_ARGS+=(-e "s|^selling_enabled = .*|selling_enabled = $SELLING_ENABLED|")
        sed "${SOLIS_SED_ARGS[@]}" "$src" > "$dst"
        echo "Generated solis/config.cfg  (IP: $FINAL_SOLIS_IP  serial: $FINAL_SOLIS_SERIAL)"
        generated=1
    else
        echo "WARNING: no Solis inverter with valid serial found – config not generated" >&2
    fi

    if [[ -n "$FINAL_DEYE_IP" ]]; then
        src="$SCRIPT_DIR/deye/config.cfg.example"
        dst="$SCRIPT_DIR/deye/config.cfg"
        DEYE_SED_ARGS=(
            -e "s|^inverter_ip = .*|inverter_ip = $FINAL_DEYE_IP|"
            -e "s|^inverter_sn = .*|inverter_sn = $FINAL_DEYE_SN|"
        )
        [[ -n "$RATED_POWER_KW" ]] && DEYE_SED_ARGS+=(-e "s|^inverter_power_kw = .*|inverter_power_kw = $RATED_POWER_KW|")
        # No --mppt-count here: Deye auto-detects MPPT count from a hardware
        # register (see deye-monitor.py), there's no config key to override.
        [[ -n "$SELLING_ENABLED" ]] && DEYE_SED_ARGS+=(-e "s|^selling_enabled = .*|selling_enabled = $SELLING_ENABLED|")
        sed "${DEYE_SED_ARGS[@]}" "$src" > "$dst"
        echo "Generated deye/config.cfg   (IP: $FINAL_DEYE_IP  SN: $FINAL_DEYE_SN)"
        generated=1
    else
        echo "WARNING: no Deye inverter with valid SN found – config not generated" >&2
    fi

    (( generated )) || echo "No configs were generated."
fi
