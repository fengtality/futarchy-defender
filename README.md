# Futarchy Defender

A one-machine tool to help **pass or fail** any [MetaDAO](https://metadao.fi) futarchy
proposal with your own funds. It watches the on-chain TWAP margin and only trades when
the proposal drifts toward the outcome you *don't* want — spending nothing while you're
safely ahead. No trading experience or servers required.

Two Docker containers run locally: **Gateway** (signs with your wallet) and
**Hummingbot** (runs the strategy). Your private key never leaves your machine.

> **Ready-made campaigns** live in [`playbooks/`](playbooks/) — e.g.
> [UMBRA-004](playbooks/umbra-004.md), run with `./setup.sh umbra-004`.

---

## What you need

- **Docker Desktop** — https://www.docker.com/products/docker-desktop/ (install and open it once)
- **A Solana wallet** holding the proposal's **DAO token**, plus a little **SOL** for fees
  (~0.05 SOL). The **quote token** (usually USDC) is optional — it arms a second, stronger lever.
- Your wallet's **private key** as a base58 string (Phantom → Settings → Export Private Key)
- The **proposal address** you want to defend (from its metadao.fi URL) — or use a playbook.

> Trading involves risk. You commit only the budget you choose; the rest of your
> wallet is never touched.

---

## Setup (about 5 minutes)

**1. Get this folder**

```bash
git clone https://github.com/fengtality/futarchy-defender.git
cd futarchy-defender
```
(or download the ZIP from GitHub and unzip it, then `cd` into the folder)

**2. Run the setup wizard**

```bash
./setup.sh                 # asks you for the proposal + which outcome to defend
# or use a ready-made campaign preset:
./setup.sh umbra-004       # loads playbooks/umbra-004.conf (proposal + target=FAIL)
```

It will:
- generate local secrets (kept in `.env`, never shared),
- set the **target proposal** (from your answers or the playbook),
- start Gateway,
- ask for a **Helius API key or custom RPC URL** (optional — blank uses the public RPC),
- ask for your **private key** and import it (stays on your machine, encrypted),
- ask how much to commit,
- start Hummingbot.

**3. Open the Hummingbot client and start the defense**

```bash
docker exec -it futarchy-hummingbot ./bin/hummingbot_quickstart.py
```
- Enter the password when prompted — it's the `CONFIG_PASSWORD` the wizard printed
  (also in `.env`: run `grep CONFIG_PASSWORD .env`).
- At the `>>>` prompt:
  ```
  start --script futarchy_twap_defense.py --conf conf_futarchy_twap_defense.yml
  status
  ```

`status` shows the fight (TWAP margin vs the pass line), your budget and runway, and
the **payoff matrix** — what your position redeems to *if it fails* vs *if it passes*.
Example (a FAIL defense mid-window):

```text
  Futarchy Defense    target=FAIL    RESPONDING (sampled -0.30%)
  Proposal 8sysa3XP…    TWAP window: OPEN · 43h16m left
  Last action: SELL 45.00 PASS-base

  DECISION  (passes if PASS TWAP beats FAIL TWAP by +3%)
+-------------------------------+----------------------+
| Signal                        | Value                |
|-------------------------------+----------------------|
| Outcome if resolved now       | FAIL  [DEFENDED]     |
| Realized TWAP margin vs line  | -2.41%               |
| Sampled (live) margin vs line | -0.30%               |
| Moved since first response    | +1.90% in our favor  |
+-------------------------------+----------------------+

  MARKETS
+--------------+------------------+
| Market       | Price / Spread   |
|--------------+------------------|
| PASS         | $0.3160          |
| FAIL         | $0.3245          |
| pass vs fail | -2.62%           |
+--------------+------------------+

  BUDGET & PACING  (one slot every 30 min · runway: OK — lasts to close)
+---------+----------+--------+-------------+-------------+
| Asset   |   Budget |   Used |   Remaining |   This slot |
|---------+----------+--------+-------------+-------------|
| UMBRA   |     2000 |    135 |        1865 |        21.4 |
| USDC    |      300 |     42 |         258 |         3   |
+---------+----------+--------+-------------+-------------+

  ACTIVITY
+---------------------+---------+
| Metric              |   Value |
|---------------------+---------|
| Responses           |       3 |
| PASS-base sold      |     135 |
| FAIL-base bought    |     130 |
| Quote spent on buys |      42 |
+---------------------+---------+
  Recent trades:
+-----+--------+----------+--------+---------+-----------+
|   # | Side   | Market   |   Size |   Quote | Tx        |
|-----+--------+----------+--------+---------+-----------|
|   1 | SELL   | PASS     |     45 |   14.2  | 4rNT31m2… |
|   2 | BUY    | FAIL     |     60 |   18.1  | 2Pra1cY3… |
|   3 | SELL   | PASS     |     45 |   14.05 | 51qrekfr… |
+-----+--------+----------+--------+---------+-----------+

  PAYOFF OF POSITION NOW  (conditional tokens redeem per outcome)
+---------------+---------+--------+
| If proposal   |   UMBRA | USDC   |
|---------------+---------+--------|
| FAILS (goal)  |    2730 | $2     |
| PASSES        |    2465 | $260   |
+---------------+---------+--------+
```

Every trade is also logged (`TRADE #n | … | tx https://solscan.io/tx/…`), and the full
board is written to the log every `deploy_interval_min`.

**4. Leave it running / stop**

- To keep it running but step away: detach with **`Ctrl-P` then `Ctrl-Q`**. Do **not**
  type `exit` or press `Ctrl-C` — with the client the strategy runs *inside your
  session*, so those stop the bot.
- To stop everything: `docker compose down`.

### Advanced: run detached with the `hbot` CLI

The client ties the bot to your terminal session. To run it **detached** so it
survives closing the terminal (better for a multi-day window):

```bash
docker exec futarchy-hummingbot hbot start conf_futarchy_twap_defense.yml   # start detached
docker exec futarchy-hummingbot hbot status                                 # check any time
docker exec futarchy-hummingbot hbot logs -f                                 # stream logs
docker exec futarchy-hummingbot hbot stop --force                           # stop the bot
```
Only one bot runs per install — stop one method before starting the other.

---

## Playbooks

Ready-made campaigns (proposal + recommended target/settings) live in
[`playbooks/`](playbooks/). Each has a `.conf` preset and a `.md` write-up:

| Playbook | Run | Details |
|---|---|---|
| UMBRA-004 (defend FAIL) | `./setup.sh umbra-004` | [playbooks/umbra-004.md](playbooks/umbra-004.md) |

To add your own: drop a `playbooks/<name>.conf` with `NAME`, `PROPOSAL`, and `TARGET`.

---

## How it decides (plain English)

Gateway reports one number: the **margin vs the pass line** (a proposal passes if the
PASS market's time-weighted price beats the FAIL market's by a threshold, usually +3%).
Every ~20s the bot checks it:

- **You're safely ahead** → it does nothing. No fees spent.
- **The proposal drifts within your safety cushion of flipping** → it responds with
  one paced slice, then waits at least 60s (so each trade lands on a fresh TWAP
  observation) and only escalates if still threatened.

It commits only your budget, paced across the voting window so it never dumps
everything at once and always keeps ammo for the finish. Two levers, cheapest first:
**sell the losing side's token** (funded by the DAO token) and **buy the winning side's
token** (funded by the quote token). It splits underlying into conditional tokens as needed.

---

## Change settings

Edit `conf/scripts/conf_futarchy_twap_defense.yml`, then restart the bot
(`hbot stop --force` then `hbot start …`). Common knobs:

| Field | Default | Meaning |
|---|---|---|
| `proposal` | (set by setup) | MetaDAO proposal address |
| `target_direction` | `FAIL` | Outcome you defend (`PASS` or `FAIL`) |
| `base_budget` | `0` | Max DAO token to commit (`0` = all in wallet) |
| `quote_budget` | `0` | Max quote token to commit (`0` = all in wallet) |
| `deploy_interval_min` | `30` | Pacing granularity (smaller = finer laddering) |
| `safety_margin_pct` | `0.5` | React when within this % of the pass line |
| `max_buy_price` | `0` | Never buy the winning side above this price (`0` = no cap) |
| `dry_run` | `false` | Log intended trades without sending them (test mode) |

You do **not** need to set the DAO address, add tokens, or set a wallet address —
the DAO is derived from the proposal, decimals are read on-chain, symbols resolve
automatically, and the strategy uses Gateway's default wallet (the one you imported).

---

## Use your own RPC endpoint (recommended if you see rate-limit errors)

`./setup.sh` **asks** for a Helius API key or a custom RPC URL — the easiest way to
set this. To change it later, edit the config and restart Gateway.

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

- **`hbot: command not found`** — include the full prefix: `docker exec futarchy-hummingbot hbot …`.
- **Nothing happens / "WATCHING"** — that's correct when you're winning. It only
  trades when threatened.
- **`RateOracle` / rate errors in the log** — harmless background noise. The strategy
  gets every price from Gateway and ignores Hummingbot's USD-price feed.
- **Rate-limit or RPC errors** — set a custom RPC (section above).
- **Start over** — `docker compose down`, delete `gateway-conf/`, `hummingbot-data/`,
  `.wallet_done`, `.rpc_done`, then `./setup.sh` again.

---

## Disclaimer

This is open-source software provided as-is (MIT). It places real trades with your
funds on your instruction. Futarchy markets can be irrational and illiquid; you can
lose money. Trading your own conviction in a governance market is the mechanism
MetaDAO is designed around — use it responsibly and within a budget you can afford.
