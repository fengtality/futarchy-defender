# UMBRA-004 Defense

A one-machine tool to help **fail** (or pass) the MetaDAO futarchy proposal
[UMBRA-004](https://www.metadao.fi/projects/umbra/proposal/8sysa3XPrvKPmUA4qoZCn9h4vp7Mb45Ynezg542nui8Q)
with your own funds. It watches the on-chain TWAP margin and only trades when the
proposal drifts toward the outcome you *don't* want — it spends nothing while
you're safely ahead. No trading experience or servers required.

Two Docker containers run locally: **Gateway** (signs with your wallet) and
**Hummingbot** (runs the strategy). Your private key never leaves your machine.

---

## What you need

- **Docker Desktop** — https://www.docker.com/products/docker-desktop/ (install and open it once)
- **A Solana wallet** holding **UMBRA**, plus a little **SOL** for transaction fees
  (~0.05 SOL is plenty). USDC is optional — it arms a second, stronger lever.
- Your wallet's **private key** as a base58 string (Phantom → Settings → Export Private Key)

> Trading involves risk. You commit only the budget you choose; the rest of your
> wallet is never touched.

---

## Setup (about 5 minutes)

**1. Get this folder**

```bash
git clone https://github.com/OWNER/umbra-defense.git
cd umbra-defense
```
(or download the ZIP from GitHub and unzip it, then `cd` into the folder)

**2. Run the setup wizard**

```bash
./setup.sh
```

It will:
- generate local secrets (kept in `.env`, never shared),
- start Gateway,
- ask for your **private key** and import it (stays on your machine, encrypted),
- ask how much **UMBRA / USDC** to commit,
- start Hummingbot.

**3. Start the defense**

```bash
docker exec umbra-hummingbot hbot start conf_futarchy_twap_defense.yml
```

**4. Watch it**

```bash
docker exec umbra-hummingbot hbot status     # the live board (run it any time)
docker exec umbra-hummingbot hbot logs -f     # stream the log (Ctrl-C to stop watching)
```

The `status` board shows the fight (TWAP margin vs the pass line), your budget and
runway, and the **payoff matrix** — what your position redeems to *if it fails* vs
*if it passes*.

**Stop:**
```bash
docker exec umbra-hummingbot hbot stop --force   # stop the bot, leave containers up
docker compose down                              # stop everything
```

---

## How it decides (plain English)

Gateway reports one number: the **margin vs the pass line** (the proposal passes if
the PASS market's time-weighted price beats the FAIL market's by +3%). Every ~20s
the bot checks it:

- **You're safely ahead** → it does nothing. No fees spent.
- **The proposal drifts within your safety cushion of flipping** → it responds with
  one paced slice, then waits at least 60s (so each trade lands on a fresh TWAP
  observation) and only escalates if still threatened.

It commits only your budget, paced across the voting window so it never dumps
everything at once and always keeps ammo for the finish.

---

## Change settings

Edit `conf/scripts/conf_futarchy_twap_defense.yml`, then restart the bot
(`hbot stop --force` then `hbot start …`). Common knobs:

| Field | Default | Meaning |
|---|---|---|
| `target_direction` | `FAIL` | Outcome you defend (`PASS` or `FAIL`) |
| `base_budget` | `0` | Max UMBRA to commit (`0` = all in wallet) |
| `quote_budget` | `0` | Max USDC to commit (`0` = all in wallet) |
| `deploy_interval_min` | `30` | Pacing granularity (smaller = finer laddering) |
| `safety_margin_pct` | `0.5` | React when within this % of the pass line |
| `max_buy_price` | `0` | Never buy the winning side above this price (`0` = no cap) |
| `dry_run` | `false` | Log intended trades without sending them (test mode) |

To defend a **different proposal**, set `dao`, `proposal`, and `target_direction`.

You do **not** need to add tokens or a wallet address — decimals are read on-chain,
the symbol resolves automatically, and the strategy uses Gateway's default wallet
(the one you imported).

---

## Use your own RPC endpoint (recommended if you see rate-limit errors)

By default Gateway uses the free public Solana RPC, which can be slow or rate-limited.
After the first `./setup.sh` (which creates `gateway-conf/`), pick **one**:

**Option A — any custom RPC URL (simplest).** A Helius URL works directly here.
Edit `gateway-conf/chains/solana/mainnet-beta.yml`:
```yaml
nodeURL: https://mainnet.helius-rpc.com/?api-key=YOUR_HELIUS_KEY
```

**Option B — Helius as a provider.** Edit `gateway-conf/apiKeys.yml`:
```yaml
helius: 'YOUR_HELIUS_KEY'
```
and `gateway-conf/chains/solana.yml`:
```yaml
rpcProvider: helius
```

Then restart Gateway: `docker compose restart gateway`.

---

## Security

- Your private key is sent only to the Gateway container on **your** machine and
  stored encrypted. It never goes to anyone else or any server.
- Gateway is published on `127.0.0.1` only — not reachable from your network.
- `.env` (your local passphrases) and your wallet are git-ignored — never commit them.

---

## Troubleshooting

- **`hbot: command not found`** — use the full command shown above
  (`docker exec umbra-hummingbot hbot ...`); it must include `docker exec umbra-hummingbot`.
- **Nothing happens / "WATCHING"** — that's correct when you're winning. It only
  trades when threatened.
- **Rate-limit or RPC errors in the log** — set a custom RPC (section above).
- **Start over** — `docker compose down`, delete the `gateway-conf/`, `hummingbot-data/`,
  and `.wallet_done` files, then `./setup.sh` again.
