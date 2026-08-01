#!/usr/bin/env bash
# Futarchy Defender — one-command setup.
# Brings up Gateway, picks a target proposal, imports YOUR wallet locally,
# wires the strategy, and starts Hummingbot.
#
# Usage:
#   ./setup.sh                 # asks you for the proposal + target
#   ./setup.sh <playbook>      # loads a preset from playbooks/<playbook>.conf (e.g. ./setup.sh umbra-004)
set -euo pipefail
cd "$(dirname "$0")"

GW=http://localhost:15888
CONF=conf/scripts/conf_futarchy_twap_defense.yml
PLAYBOOK="${1:-}"

say()  { printf "\n\033[1;36m%s\033[0m\n" "$1"; }
err()  { printf "\n\033[1;31m%s\033[0m\n" "$1" >&2; }

command -v docker >/dev/null || { err "Docker is not installed. Install Docker Desktop first: https://www.docker.com/products/docker-desktop/"; exit 1; }
docker compose version >/dev/null 2>&1 || { err "Docker Compose v2 is required (comes with Docker Desktop)."; exit 1; }

# 1. Secrets (generated once, kept local in .env)
if [ ! -f .env ]; then
  say "Generating local secrets (.env)…"
  {
    echo "GATEWAY_PASSPHRASE=$(openssl rand -hex 16)"
    echo "CONFIG_PASSWORD=$(openssl rand -hex 16)"
  } > .env
fi

# 2. Pick the target proposal — from a playbook preset, or interactively
if [ -n "$PLAYBOOK" ]; then
  [ -f "playbooks/$PLAYBOOK.conf" ] || { err "No playbook 'playbooks/$PLAYBOOK.conf'. See the playbooks/ folder."; exit 1; }
  # shellcheck disable=SC1090
  . "playbooks/$PLAYBOOK.conf"   # sets PROPOSAL, TARGET, and (optionally) NAME
  say "Playbook: ${NAME:-$PLAYBOOK}  →  defend ${TARGET}"
  echo "  Proposal: $PROPOSAL"
else
  say "Target proposal"
  echo "Find the proposal on metadao.fi and copy its address from the URL."
  read -rp "  MetaDAO proposal address: " PROPOSAL
  read -rp "  Defend which outcome? [FAIL/PASS]: " TARGET
fi
PROPOSAL="${PROPOSAL:?proposal address required}"
TARGET=$(printf '%s' "${TARGET:-FAIL}" | tr '[:lower:]' '[:upper:]')
[ "$TARGET" = "FAIL" ] || [ "$TARGET" = "PASS" ] || { err "Target must be FAIL or PASS."; exit 1; }

# 3. Start Gateway and wait until healthy
say "Starting Gateway…"
docker compose up -d gateway
printf "Waiting for Gateway to be ready"
for i in $(seq 1 40); do
  if curl -s -m 3 "$GW/" 2>/dev/null | grep -q ok; then ready=1; break; fi
  printf "."; sleep 2
done
printf "\n"
[ "${ready:-}" = "1" ] || { err "Gateway did not start. Check: docker compose logs gateway"; exit 1; }

# 4. Solana RPC endpoint (public by default; a private one avoids rate limits)
SOLANA_CFG=gateway-conf/chains/solana/mainnet-beta.yml
if [ ! -f .rpc_done ]; then
  say "Solana RPC endpoint (recommended, optional)"
  echo "The free public RPC works but can be slow or rate-limited. For a smoother run,"
  echo "paste a Helius API key (free at helius.dev) OR a full custom RPC URL."
  read -rp "  Helius API key or RPC URL [blank = use public RPC]: " RPC_IN
  if [ -n "${RPC_IN:-}" ] && [ -f "$SOLANA_CFG" ]; then
    case "$RPC_IN" in
      http*) NODE_URL="$RPC_IN" ;;
      *)     NODE_URL="https://mainnet.helius-rpc.com/?api-key=$RPC_IN" ;;
    esac
    python3 - "$SOLANA_CFG" "$NODE_URL" <<'PY'
import sys, re
path, url = sys.argv[1:3]
s = open(path).read()
s = re.sub(r"^nodeURL:.*$", f"nodeURL: {url}", s, flags=re.M)
open(path, "w").write(s)
PY
    say "RPC set. Restarting Gateway…"
    docker compose restart gateway >/dev/null
    printf "Waiting for Gateway"
    for i in $(seq 1 40); do curl -s -m 3 "$GW/" 2>/dev/null | grep -q ok && break; printf "."; sleep 2; done
    printf "\n"
  fi
  touch .rpc_done
fi

# 5. Import YOUR wallet (stays local, encrypted inside Gateway)
say "Import your Solana wallet"
echo "Your private key is sent ONLY to the Gateway running on THIS machine and is stored"
echo "encrypted. It never leaves your computer. (Base58 string, e.g. from Phantom > Export.)"
if [ -f .wallet_done ]; then
  echo "A wallet is already imported (delete .wallet_done to redo). Skipping."
else
  printf "\nPaste your Solana private key (input hidden): "
  read -rs PRIVKEY; printf "\n"
  [ -n "$PRIVKEY" ] || { err "No key entered."; exit 1; }
  RESP=$(curl -s -m 20 -X POST "$GW/wallet/add" -H 'Content-Type: application/json' \
    -d "{\"chain\":\"solana\",\"privateKey\":\"$PRIVKEY\",\"setDefault\":true}")
  unset PRIVKEY
  WALLET=$(printf '%s' "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('address',''))" 2>/dev/null || true)
  [ -n "$WALLET" ] || { err "Wallet import failed. Gateway said: $RESP"; exit 1; }
  printf '%s' "$WALLET" > .wallet_done
  say "Imported wallet: $WALLET (set as default)"
fi

# 6. Budget
say "How much are you willing to commit to the defense?"
echo "Enter token amounts, or 0 to use everything currently in the wallet."
read -rp "  Max base token (the DAO token) to commit [0 = all]: " BASE_BUDGET
read -rp "  Max quote token (e.g. USDC) to commit      [0 = all]: " QUOTE_BUDGET
BASE_BUDGET=${BASE_BUDGET:-0}; QUOTE_BUDGET=${QUOTE_BUDGET:-0}

# 7. Write the strategy config (wallet + dao are auto-discovered at runtime)
python3 - "$CONF" "$PROPOSAL" "$TARGET" "$BASE_BUDGET" "$QUOTE_BUDGET" <<'PY'
import sys, re
path, proposal, target, base, quote = sys.argv[1:6]
s = open(path).read()
s = s.replace("PROPOSAL_PLACEHOLDER", proposal).replace("TARGET_PLACEHOLDER", target)
s = re.sub(r"^base_budget:.*$",  f"base_budget: {base}",  s, flags=re.M)
s = re.sub(r"^quote_budget:.*$", f"quote_budget: {quote}", s, flags=re.M)
open(path, "w").write(s)
print(f"Config written: defend {target}, proposal {proposal[:8]}…, budget base={base} quote={quote}")
PY

# 8. Start the Hummingbot container (stays idle; you open the client to run the bot)
say "Starting Hummingbot…"
docker compose up -d hummingbot

PW=$(grep '^CONFIG_PASSWORD=' .env | cut -d= -f2)

cat <<EOF

$(printf "\033[1;32m✓ Setup complete.\033[0m")

$(printf "\033[1;36mStart the defense — open the Hummingbot client:\033[0m")
  docker exec -it futarchy-hummingbot ./bin/hummingbot_quickstart.py

  • When it asks for a password, paste:   $PW
  • At the >>> prompt, run:
      start --script futarchy_twap_defense.py --conf conf_futarchy_twap_defense.yml
      status
  • To leave it running, detach with:  Ctrl-P then Ctrl-Q
    (do NOT type "exit" or press Ctrl-C — that stops the bot)

Stop everything:   docker compose down
Gateway API docs:  http://localhost:15888/docs

The bot waits for the TWAP window, watches the margin, and only trades when the
proposal drifts against your target. It spends nothing while you're safely ahead.

(Advanced: to run the bot detached so it survives closing the terminal, use the
 hbot CLI instead — see "hbot" in README.md.)
EOF
