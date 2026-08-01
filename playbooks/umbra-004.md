# Playbook: UMBRA-004 — defend FAIL

**Proposal:** [UMBRA-004](https://www.metadao.fi/projects/umbra/proposal/8sysa3XPrvKPmUA4qoZCn9h4vp7Mb45Ynezg542nui8Q)
`8sysa3XPrvKPmUA4qoZCn9h4vp7Mb45Ynezg542nui8Q`
**Target:** `FAIL` · **Base token:** UMBRA · **Quote:** USDC

## Run it

```bash
./setup.sh umbra-004
```
This preloads the proposal and `target=FAIL`; the wizard still asks for your RPC,
wallet, and budget. Then start the defense as in the main [README](../README.md).

## Why FAIL

The thesis: if UMBRA-004 **passes** it drains the treasury and UMBRA goes to ~0, so
the pass-market price is fundamentally overvalued. Selling pass-UMBRA is the correct
trade on its own — the defense just makes that pressure count toward the vote.

## How the bot plays it (target = FAIL)

The tool watches the on-chain TWAP margin and only acts when the proposal drifts
toward **passing** (within `safety_margin_pct` of the +3% line). When it does, it
uses two levers, paced across the voting window:

- **Sell pass-UMBRA** in the PASS market → drags the pass price down. (Funded by your
  UMBRA; the bot splits underlying UMBRA into pass/fail tokens as needed.)
- **Buy fail-UMBRA** with fail-USDC in the FAIL market → pushes the fail price up.
  (Funded by your USDC — the thinner FAIL pool moves more per dollar, so USDC is the
  stronger lever. Optional: works with UMBRA alone.)

## Payoff

| If the proposal… | You hold |
|---|---|
| **Fails** (goal) | fail-UMBRA (your split UMBRA + any fail-UMBRA bought below spot) redeems 1:1 to UMBRA |
| **Passes** | pass-USDC from the pass-side sells redeems 1:1 to USDC — you exited the dying token near market before the drain |

You come out ahead of doing nothing in **both** outcomes.

## Suggested settings

- **Budget:** commit what you're willing to risk. USDC arms the stronger FAIL-buy
  lever; even a few hundred dollars helps because the FAIL pool is thin.
- **`safety_margin_pct` 0.5, `deploy_interval_min` 30** (defaults) are a good start.
  Tighten the cushion or shrink the interval to defend more aggressively.
- Set a **custom RPC** (Helius) in setup — the polling is light but a private RPC is
  smoother than the public one.

## Notes

- ~$1–2k of flow nudges the TWAP but can't hold it alone against a determined
  counterparty — the defense works best when several holders run it in the same
  direction. Don't turn a small defense into a bidding war; the reactive design
  spends only when you're actually being pushed.
- The proposal passes if the PASS TWAP exceeds the FAIL TWAP by the threshold (+3%).
  Watch the **DECISION** table in `status` for the live margin.
