#!/usr/bin/env bash
# UMBRA-004 defense — one-command setup.
# Brings up Gateway, imports YOUR wallet locally, wires the strategy, starts Hummingbot.
set -euo pipefail
cd "$(dirname "$0")"

GW=http://localhost:15888
CONF=conf/scripts/conf_futarchy_twap_defense.yml

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

# 2. Start Gateway and wait until healthy
say "Starting Gateway…"
docker compose up -d gateway
printf "Waiting for Gateway to be ready"
for i in $(seq 1 40); do
  if curl -s -m 3 "$GW/" 2>/dev/null | grep -q ok; then ready=1; break; fi
  printf "."; sleep 2
done
printf "\n"
[ "${ready:-}" = "1" ] || { err "Gateway did not start. Check: docker compose logs gateway"; exit 1; }

# 3. Import YOUR wallet (stays local, encrypted inside Gateway)
say "Import your Solana wallet"
echo "Your private key is sent ONLY to the Gateway running on THIS machine and is stored"
echo "encrypted. It never leaves your computer. (Base58 string, e.g. from Phantom > Export.)"
if [ -f .wallet_done ]; then
  echo "A wallet is already imported (delete .wallet_done to redo). Skipping."
  WALLET=$(cat .wallet_done)
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

# 4. Budget
say "How much are you willing to commit to the defense?"
echo "Enter amounts, or 0 to use everything currently in the wallet."
read -rp "  Max UMBRA to commit [0 = all]: " BASE_BUDGET
read -rp "  Max USDC to commit  [0 = all]: " QUOTE_BUDGET
BASE_BUDGET=${BASE_BUDGET:-0}; QUOTE_BUDGET=${QUOTE_BUDGET:-0}

# 5. Write the budget into the strategy config (wallet is auto-discovered from Gateway's default)
python3 - "$CONF" "$BASE_BUDGET" "$QUOTE_BUDGET" <<'PY'
import sys, re
path, base, quote = sys.argv[1:4]
s = open(path).read()
s = re.sub(r"^base_budget:.*$",  f"base_budget: {base}",  s, flags=re.M)
s = re.sub(r"^quote_budget:.*$", f"quote_budget: {quote}", s, flags=re.M)
open(path, "w").write(s)
print(f"Config written: target FAIL, budget UMBRA={base} USDC={quote}")
PY

# 6. Start the Hummingbot container (stays idle; you drive it with hbot)
say "Starting Hummingbot…"
docker compose up -d hummingbot

cat <<EOF

$(printf "\033[1;32m✓ Setup complete.\033[0m")

Start the defense:
  docker exec umbra-hummingbot hbot start conf_futarchy_twap_defense.yml

Monitor it:
  docker exec umbra-hummingbot hbot status     # live board: margin, budget, payoff
  docker exec umbra-hummingbot hbot logs -f     # stream logs (Ctrl-C to stop watching)

Stop / change settings:
  docker exec umbra-hummingbot hbot stop --force   # stop the bot (containers stay up)
  # edit conf/scripts/conf_futarchy_twap_defense.yml, then hbot start again
  docker compose down                              # stop everything

Gateway API docs:  http://localhost:15888/docs

The bot waits for the TWAP window, watches the margin, and only trades when the
proposal drifts toward passing. It spends nothing while you're safely ahead.
EOF
