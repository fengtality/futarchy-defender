import math
import os
import time
from decimal import Decimal
from typing import Dict, Optional

from pydantic import Field

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import MarketDict
from hummingbot.core.gateway.gateway_http_client import GatewayHttpClient
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase

METADAO = "connectors/metadao/futarchy"
DUST = Decimal("1")


class FutarchyTwapDefenseConfig(StrategyV2ConfigBase):
    """
    Reactive, budget-paced TWAP defense for any MetaDAO futarchy proposal.

    You set a BUDGET (how much UMBRA / USDC you'll commit) and a target outcome.
    The bot watches the on-chain TWAP margin vs the pass threshold (gateway
    computes it) and only spends when the decision metric drifts within
    `safety_margin_pct` of flipping against you. It does NOT push on a schedule
    (kollan: don't turn a $50k defense into a $1.5M one).

    Budget pacing: the window is sliced into `deploy_interval_min` slots; each
    slot's allowance = remaining budget / remaining slots. Calm slots roll their
    allowance forward, so ammo always lasts to the close and is never dumped at
    once. Reactive trigger decides *whether* to deploy a slot; pacing decides
    *how much*.

    target_direction = FAIL -> keep the metric below the pass line
                     = PASS -> keep the metric above the pass line
    """
    script_file_name: str = Field(default_factory=lambda: os.path.basename(__file__))

    # --- Target ---
    network: str = Field("mainnet-beta")
    dao: str = Field("BLkBSE96kQys7SrMioKxeMiVbeo4Ckk2Y4n1JphKxYnv", json_schema_extra={
        "prompt": "DAO pool address", "prompt_on_new": True})
    proposal: str = Field("8sysa3XPrvKPmUA4qoZCn9h4vp7Mb45Ynezg542nui8Q", json_schema_extra={
        "prompt": "Proposal address", "prompt_on_new": True})
    target_direction: str = Field("FAIL", json_schema_extra={
        "prompt": "Outcome to defend: PASS or FAIL", "prompt_on_new": True})

    # --- Budget (how much to commit; 0 = all available in wallet) ---
    base_budget: Decimal = Field(Decimal("2000"), json_schema_extra={
        "prompt": "Max base token (e.g. UMBRA) to commit (0 = all in wallet)", "prompt_on_new": True})
    quote_budget: Decimal = Field(Decimal("0"), json_schema_extra={
        "prompt": "Max quote token (e.g. USDC) to commit (0 = all in wallet)", "prompt_on_new": True})

    # --- Pacing ---
    deploy_interval_min: int = Field(30, json_schema_extra={
        "prompt": "Deploy interval in minutes (budget is paced across window slots of this size)",
        "prompt_on_new": True})
    response_gap_sec: int = Field(60, json_schema_extra={
        "prompt": "Min seconds between trades (>=60 to hit fresh TWAP observations)", "prompt_on_new": False})

    # --- Trigger ---
    safety_margin_pct: Decimal = Field(Decimal("0.5"), json_schema_extra={
        "prompt": "React when the metric is within this % of the pass line", "prompt_on_new": True})
    use_last_obs: bool = Field(True, json_schema_extra={
        "prompt": "React on instantaneous sampled margin (True) vs realized TWAP (False)", "prompt_on_new": False})

    # --- Guardrails ---
    slippage_pct: Decimal = Field(Decimal("2.0"))
    max_buy_price: Decimal = Field(Decimal("0"), json_schema_extra={
        "prompt": "Never buy the winning side above this price (0 = no cap)", "prompt_on_new": True})
    poll_interval_sec: int = Field(20)
    dry_run: bool = Field(False, json_schema_extra={
        "prompt": "Dry run (log intended trades, send nothing)", "prompt_on_new": True})

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets


class FutarchyTwapDefense(StrategyV2Base):
    def __init__(self, connectors: Dict[str, ConnectorBase], config: FutarchyTwapDefenseConfig):
        super().__init__(connectors, config)
        self.config = config
        self._target = config.target_direction.upper()
        self._sell_market = "PASS" if self._target == "FAIL" else "FAIL"   # dump the losing side
        self._buy_market = self._target                                    # pump the winning side

        self._busy = False
        self._next_poll = 0.0
        self._last_response_ts = 0.0

        self._info: Optional[dict] = None
        self._balances: Optional[dict] = None
        self._wallet: Optional[str] = None  # auto-discovered from gateway's default solana wallet
        self._phase = "INIT"
        self._last_action = "none yet"

        # Budget resolved on first balance read (0 -> use wallet balance)
        self._base_budget: Optional[Decimal] = None
        self._quote_budget: Optional[Decimal] = None

        # Session accounting (in-memory; a restart resets these)
        self._base_used = Decimal("0")       # underlying base consumed (losing-side base sold)
        self._quote_used = Decimal("0")      # underlying quote consumed (winning-side buys)
        self._proceeds_quote = Decimal("0")  # conditional quote received from sells
        self._acquired_base = Decimal("0")   # winning-side base bought
        self._responses = 0
        self._first_margin: Optional[Decimal] = None

    # -------------------------------------------------------------- clock

    def on_tick(self):
        if self._busy or time.time() < self._next_poll:
            return
        self._busy = True
        safe_ensure_future(self._poll())

    async def _poll(self):
        try:
            await self._check_and_react()
        except Exception as e:
            self.logger().error(f"poll error: {e}", exc_info=True)
        finally:
            self._next_poll = time.time() + self.config.poll_interval_sec
            self._busy = False

    # -------------------------------------------------------------- core

    async def _check_and_react(self):
        gw = GatewayHttpClient.get_instance()
        info = await gw.api_request("get", f"{METADAO}/proposal-info", {
            "network": self.config.network, "proposal": self.config.proposal,
        })
        if not info or "twap" not in info:
            raise RuntimeError(f"bad proposal-info: {info}")
        self._info = info

        if info.get("status") != "pending":
            self._phase = f"DONE ({info.get('status')})"
            self._next_poll = time.time() + 10 ** 9
            return

        twap = info.get("twap") or {}
        now = time.time()
        starts_at = twap.get("twapStartsAt", 0)
        ends_at = twap.get("twapEndsAt", info.get("tradingEndsAt", 0))
        window_open = twap.get("windowOpen", False) or now >= starts_at

        if self.config.use_last_obs or not twap.get("windowOpen"):
            metric = Decimal(str(twap.get("lastObsMarginVsThresholdPct", -99)))
            basis = "sampled"
        else:
            metric = Decimal(str(twap.get("marginVsThresholdPct", -99)))
            basis = "realized"

        # threatened = metric drifting against our target
        if self._target == "FAIL":
            threatened = metric >= -self.config.safety_margin_pct
        else:
            threatened = metric <= self.config.safety_margin_pct

        if not window_open:
            self._phase = f"WATCHING (opens in {self._eta(starts_at - now)}, {basis} {metric:+.2f}%)"
            return
        if now >= ends_at:
            self._phase = "DONE (TWAP window closed)"
            self._next_poll = now + 10 ** 9
            return
        if not threatened:
            self._phase = f"WATCHING ({basis} {metric:+.2f}%, {self._target} safe)"
            return
        if now - self._last_response_ts < self.config.response_gap_sec:
            self._phase = f"THREAT {metric:+.2f}% - cooling to next observation"
            return

        # --- respond ---
        self._phase = f"RESPONDING ({basis} {metric:+.2f}%)"
        balances = await self._balances_call(gw)
        self._balances = balances
        self._resolve_budgets(balances)
        if self._first_margin is None:
            self._first_margin = metric

        base_slot, quote_slot = self._slot_allowances(now, ends_at)
        acted = await self._respond(gw, balances, base_slot, quote_slot)
        if acted:
            self._last_response_ts = now
            self._responses += 1

    def _resolve_budgets(self, bal: Dict[str, Decimal]):
        if self._base_budget is None:
            self._base_budget = self.config.base_budget if self.config.base_budget > 0 else bal["base"]
        if self._quote_budget is None:
            self._quote_budget = self.config.quote_budget if self.config.quote_budget > 0 else bal["quote"]

    def _base_remaining(self) -> Decimal:
        return max(Decimal("0"), (self._base_budget or Decimal("0")) - self._base_used)

    def _quote_remaining(self) -> Decimal:
        return max(Decimal("0"), (self._quote_budget or Decimal("0")) - self._quote_used)

    def _slot_allowances(self, now: float, ends_at: float):
        # Remaining budget spread across remaining window slots; calm slots roll forward.
        slot_sec = max(60, self.config.deploy_interval_min * 60)
        slots_left = max(1, math.ceil(max(0.0, ends_at - now) / slot_sec))
        return self._base_remaining() / slots_left, self._quote_remaining() / slots_left

    # -------------------------------------------------------------- levers

    def _sell_base_key(self) -> str:
        return "pass_base" if self._sell_market == "PASS" else "fail_base"

    def _buy_quote_key(self) -> str:
        return "pass_quote" if self._buy_market == "PASS" else "fail_quote"

    async def _respond(self, gw, bal, base_slot: Decimal, quote_slot: Decimal) -> bool:
        # Primary: dump the losing side (funded by base budget).
        size = min(base_slot, self._base_remaining())
        if size >= DUST:
            await self._ensure_conditional(gw, bal, "base", self._sell_base_key(), size)
            size = min(size, bal[self._sell_base_key()]).quantize(Decimal("0.000001"))
            if size >= DUST:
                if self.config.dry_run:
                    self._note(f"[DRY] SELL {size} {self._sell_market}-base (slot {base_slot:.1f})")
                else:
                    r = await self._swap(gw, self._sell_market, "SELL", size)
                    self._proceeds_quote += Decimal(str(r["data"]["amountOut"]))
                    self._note(f"SOLD {size} {self._sell_market}-base -> {r['data']['amountOut']:.2f} quote "
                               f"({r['signature'][:12]}...)")
                self._base_used += size
                return True

        # Secondary: pump the winning side (funded by quote budget).
        q = min(quote_slot, self._quote_remaining())
        if q >= DUST:
            price = Decimal(str(self._info[f"{self._buy_market.lower()}Pool"]["price"]))
            if self.config.max_buy_price > 0 and price > self.config.max_buy_price:
                self._note(f"skip buy: {self._buy_market} price {price:.4f} > cap {self.config.max_buy_price}")
                return False
            await self._ensure_conditional(gw, bal, "quote", self._buy_quote_key(), q)
            q = min(q, bal[self._buy_quote_key()])
            headroom = (Decimal("100") - self.config.slippage_pct - Decimal("1.5")) / Decimal("100")
            base_out = (q * headroom / price).quantize(Decimal("0.000001"))
            if base_out >= DUST:
                if self.config.dry_run:
                    self._note(f"[DRY] BUY {base_out} {self._buy_market}-base (~{q:.1f} quote, slot {quote_slot:.1f})")
                    self._quote_used += q
                    return True
                r = await self._swap(gw, self._buy_market, "BUY", base_out)
                paid = Decimal(str(r["data"]["amountIn"]))
                self._quote_used += paid
                self._acquired_base += base_out
                self._note(f"BOUGHT {base_out} {self._buy_market}-base for {paid:.2f} quote ({r['signature'][:12]}...)")
                return True

        self._note(f"THREAT but budget exhausted / no ammo (need {self._sell_market}-base or {self._buy_market}-quote)")
        return False

    async def _ensure_conditional(self, gw, bal, asset: str, cond_key: str, need: Decimal):
        # Split underlying into conditionals if the needed conditional leg is short.
        if bal[cond_key] >= need:
            return
        want = min(bal[asset], need - bal[cond_key])
        if want < DUST:
            return
        await self._split(gw, asset, want)
        if asset == "base":
            bal["pass_base"] += want
            bal["fail_base"] += want
        else:
            bal["pass_quote"] += want
            bal["fail_quote"] += want
        bal[asset] -= want

    # -------------------------------------------------------------- gateway calls

    async def _default_wallet(self, gw) -> str:
        # Gateway's default solana wallet (set when you imported it). No config needed.
        if self._wallet is None:
            cfg = await gw.api_request("get", "config", {"namespace": "solana"})
            self._wallet = (cfg or {}).get("defaultWallet") or ""
            if not self._wallet:
                raise RuntimeError("No default Solana wallet in Gateway. Import one (setup.sh) first.")
        return self._wallet

    async def _balances_call(self, gw) -> Dict[str, Decimal]:
        owner = await self._default_wallet(gw)
        resp = await gw.api_request("get", f"{METADAO}/balance", {
            "network": self.config.network, "dao": self.config.dao,
            "proposal": self.config.proposal, "ownerAddress": owner,
        })
        b = resp.get("balances", {})

        def bal(k):
            v = b.get(k)
            return Decimal(str(v["balance"])) if v else Decimal("0")

        return {
            "base": bal("base"), "quote": bal("quote"),
            "pass_base": bal("passBase"), "pass_quote": bal("passQuote"),
            "fail_base": bal("failBase"), "fail_quote": bal("failQuote"),
        }

    async def _swap(self, gw, market: str, side: str, amount: Decimal) -> dict:
        r = await gw.api_request("post", f"{METADAO}/execute-conditional-swap", {
            "network": self.config.network, "dao": self.config.dao, "proposal": self.config.proposal,
            "market": market, "side": side, "amount": float(amount),
            "slippagePct": float(self.config.slippage_pct),
        })
        if not r or r.get("status") != 1:
            raise RuntimeError(f"{side} {market} {amount} failed: {r}")
        return r

    async def _split(self, gw, asset: str, amount: Decimal):
        amount = amount.quantize(Decimal("0.000001"))
        if self.config.dry_run:
            self._note(f"[DRY] split {amount} {asset}")
            return
        r = await gw.api_request("post", f"{METADAO}/split-tokens", {
            "network": self.config.network, "dao": self.config.dao, "proposal": self.config.proposal,
            "asset": asset, "amount": float(amount),
        })
        if not r or r.get("status") != 1:
            raise RuntimeError(f"split {asset} failed: {r}")
        self._note(f"split {amount} {asset} ({r['signature'][:12]}...)")

    # -------------------------------------------------------------- display

    def _note(self, msg: str):
        self._last_action = msg
        self.logger().info(msg)

    @staticmethod
    def _eta(seconds: float) -> str:
        seconds = int(max(0, seconds))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"

    def format_status(self) -> str:
        info = self._info or {}
        twap = info.get("twap") or {}
        bsym = info.get("baseSymbol", "base")
        qsym = info.get("quoteSymbol", "quote")
        L = [f"Futarchy defense [{self._target}] - {self._phase}", f"last: {self._last_action}"]

        # The fight
        if twap:
            now = time.time()
            ends = twap.get("twapEndsAt", info.get("tradingEndsAt", 0))
            win = "OPEN" if (twap.get("windowOpen") or now >= twap.get("twapStartsAt", 0)) else "not open"
            when = f"{self._eta(ends - now)} left" if win == "OPEN" else f"opens {self._eta(twap.get('twapStartsAt', 0) - now)}"
            outcome = "PASS" if twap.get("attackerWinning") else "FAIL"
            defended = "defended" if outcome == self._target else "AT RISK"
            moved = ""
            if self._first_margin is not None:
                cur = Decimal(str(twap.get("lastObsMarginVsThresholdPct", 0)))
                delta = cur - self._first_margin
                moved = f" | moved {(-delta if self._target=='FAIL' else delta):+.2f}% since first response"
            L.append(f"Window: {win} · {when}")
            L.append(f"Margin: realized {twap.get('marginVsThresholdPct',0):+.2f}% | "
                     f"sampled {twap.get('lastObsMarginVsThresholdPct',0):+.2f}% | "
                     f"threshold +{twap.get('thresholdPct',0)}% -> outcome-now {outcome} ({defended}){moved}")
        if info:
            p, f = info["passPool"]["price"], info["failPool"]["price"]
            L.append(f"Prices: pass ${p:.4f} fail ${f:.4f} (spread {((p/f-1)*100):+.2f}%)")

        # Budget + pacing
        if self._base_budget is not None:
            L.append(f"Budget: {bsym} {self._base_used:.1f}/{self._base_budget:.1f} used "
                     f"({self._base_remaining():.1f} left) · "
                     f"{qsym} {self._quote_used:.1f}/{self._quote_budget:.1f} ({self._quote_remaining():.1f} left)")
            if twap:
                bslot, qslot = self._slot_allowances(time.time(), twap.get("twapEndsAt", info.get("tradingEndsAt", 0)))
                runway = "ok" if self._base_remaining() + self._quote_remaining() > 0 else "EXHAUSTED"
                L.append(f"Pacing: slot allowance {bslot:.1f} {bsym} / {qslot:.1f} {qsym} · runway {runway}")
        L.append(f"Volume: {self._responses} responses · sold {self._base_used:.1f} {self._sell_market}-base · "
                 f"bought {self._acquired_base:.1f} {self._buy_market}-base")

        # Payoff matrix of current position
        b = self._balances
        if b:
            fail_umbra = b["base"] + b["fail_base"]
            fail_usdc = b["quote"] + b["fail_quote"]
            pass_umbra = b["base"] + b["pass_base"]
            pass_usdc = b["quote"] + b["pass_quote"]
            L.append("Payoff of position now:")
            L.append(f"  if FAILS  -> {fail_umbra:.0f} {bsym} + ${fail_usdc:.0f} redeemable")
            L.append(f"  if PASSES -> {pass_umbra:.0f} {bsym} + ${pass_usdc:.0f} redeemable")
        return "\n".join(L)
