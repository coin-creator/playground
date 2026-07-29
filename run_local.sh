#!/usr/bin/env bash
#
# run_local.sh — full FWA wallet audit, from a machine with normal internet.
#
#   ./run_local.sh                          # audit the default wallet
#   ./run_local.sh 0xSomeOtherWallet        # audit a different wallet
#   ./run_local.sh --csv ./exports          # offline, from Etherscan CSV exports
#
# Sets up an isolated venv, validates your Etherscan key before doing any real
# work, pulls the ledger, then reads vault contract state for the two figures
# no transfer feed can show (backing ETH, unclaimed rewards).
#
set -euo pipefail

WALLET="0x5984bb82F11171cb1DC2287E2A6935c44D491538"
CSV_DIR=""
VAULT=""
ETH_PRICE=""
FWA_PRICE=""
ETH_BALANCE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --csv)         CSV_DIR="$2"; shift 2 ;;
    --vault)       VAULT="$2"; shift 2 ;;
    --eth-price)   ETH_PRICE="$2"; shift 2 ;;
    --fwa-price)   FWA_PRICE="$2"; shift 2 ;;
    --eth-balance) ETH_BALANCE="$2"; shift 2 ;;
    -h|--help)     sed -n '2,12p' "$0"; exit 0 ;;
    0x*)           WALLET="$1"; shift ;;
    *)             echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

cd "$(dirname "$0")"
OUTDIR="./reports/$(date +%Y-%m-%d_%H%M%S)"
mkdir -p "$OUTDIR"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[1;33m    %s\033[0m\n' "$1"; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------- python + deps
command -v python3 >/dev/null || die "python3 not found. Install Python 3.9+."
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
say "Python $PYV"

if [[ ! -d .venv ]]; then
  say "Creating virtualenv (.venv)"
  python3 -m venv .venv || die "could not create venv — on Debian/Ubuntu: apt install python3-venv"
fi
PY=./.venv/bin/python
say "Installing dependencies"
$PY -m pip install -q --upgrade pip
$PY -m pip install -q -r requirements.txt || die "dependency install failed"

# ------------------------------------------------------------------------- key
if [[ -z "${ETHERSCAN_API_KEY:-}" && -f .env ]]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
fi

if [[ -z "$CSV_DIR" ]]; then
  if [[ -z "${ETHERSCAN_API_KEY:-}" ]]; then
    echo
    echo "An Etherscan API key is needed (free: https://etherscan.io/apis)."
    read -rsp "Paste key (hidden, not echoed): " ETHERSCAN_API_KEY
    echo
    export ETHERSCAN_API_KEY
    read -rp "Save it to .env for next time? [y/N] " SAVE
    if [[ "${SAVE:-}" =~ ^[Yy]$ ]]; then
      umask 077
      echo "ETHERSCAN_API_KEY=$ETHERSCAN_API_KEY" > .env
      chmod 600 .env
      warn "written to .env (chmod 600, git-ignored)"
    fi
  fi
  export ETHERSCAN_API_KEY

  # Validate before the real pull, so a bad key fails in one second, not
  # halfway through several thousand paginated records.
  say "Checking Etherscan connectivity and key"
  $PY - <<'PYCHK' || die "Etherscan preflight failed — see message above."
import os, sys, requests
k = os.environ["ETHERSCAN_API_KEY"]
try:
    r = requests.get("https://api.etherscan.io/v2/api", timeout=25, params={
        "chainid": 1, "module": "account", "action": "balance",
        "address": "0x0000000000000000000000000000000000000000",
        "tag": "latest", "apikey": k})
except requests.RequestException as e:
    sys.exit(f"    cannot reach api.etherscan.io: {e}")
if r.status_code != 200:
    sys.exit(f"    HTTP {r.status_code} from Etherscan")
d = r.json()
msg = str(d.get("result", "")) + " " + str(d.get("message", ""))
if "Invalid API Key" in msg:
    sys.exit("    Etherscan rejected the key as invalid.")
if "rate limit" in msg.lower():
    sys.exit("    key is rate limited right now; wait a moment and retry.")
if str(d.get("status")) != "1" and "NOTOK" in msg:
    sys.exit(f"    unexpected Etherscan response: {msg[:160]}")
print("    key OK, api.etherscan.io reachable")
PYCHK
fi

# ------------------------------------------------------------------- the ledger
say "Auditing $WALLET"
AUDIT_ARGS=("$WALLET" "--out" "$OUTDIR/fwa_audit.xlsx" "--json" "$OUTDIR/summary.json")
[[ -n "$CSV_DIR"     ]] && AUDIT_ARGS+=("--from-csv" "$CSV_DIR")
[[ -n "$ETH_PRICE"   ]] && AUDIT_ARGS+=("--eth-price" "$ETH_PRICE")
[[ -n "$FWA_PRICE"   ]] && AUDIT_ARGS+=("--fwa-price" "$FWA_PRICE")
[[ -n "$ETH_BALANCE" ]] && AUDIT_ARGS+=("--eth-balance" "$ETH_BALANCE")

$PY fwa_audit.py "${AUDIT_ARGS[@]}" | tee "$OUTDIR/report.txt"

# ------------------------------------------------------- vault state (contracts)
if [[ -z "$CSV_DIR" ]]; then
  if [[ -z "$VAULT" ]]; then
    VAULT=$($PY -c "
import json
d = json.load(open('$OUTDIR/summary.json'))
print(d.get('vault_candidate') or '')" 2>/dev/null || echo "")
  fi

  if [[ -n "$VAULT" ]]; then
    say "Reading vault contract state: $VAULT"
    warn "this address was inferred from volume ranking — verify it at"
    warn "https://etherscan.io/address/$VAULT"
    $PY fwa_probe.py "$WALLET" --vault "$VAULT" \
        --json "$OUTDIR/vault_state.json" 2>&1 | tee "$OUTDIR/vault_state.txt" || \
        warn "vault probe failed; the ledger above is still valid."
  else
    warn "No vault candidate identified — every counterparty matched a known"
    warn "DEX/marketplace. If you have spun on FWA, pass --vault <address>."
  fi
else
  say "Offline mode: skipping vault reads (they need network access)"
  warn "Read backing ETH and unclaimed rewards off https://www.fwa.fun instead."
fi

# ------------------------------------------------------------------------ done
say "Done. Output in $OUTDIR"
ls -1 "$OUTDIR" | sed 's/^/    /'
cat <<EOF

  report.txt        full console accounting
  fwa_audit.xlsx    Summary / Counterparties / NFT Events / Held NFTs
  summary.json      machine-readable totals
  vault_state.txt   heuristic contract reads (verify signatures before relying)

  Reminder — your true position is:

      NET REALISED (report.txt)
    + backing ETH in active deposits      (vault_state or fwa.fun)
    + unclaimed rewards                   (vault_state or fwa.fun)
    + market value of NFTs held
    + FWA held

EOF
