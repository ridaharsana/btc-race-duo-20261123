"""Dual-sleeve bot for the make.fun 24h BTC race (agent-fun/bars-replay/v1).

One account, two proven sleeves sharing a single position slot; whichever
sleeve's entry fires first owns the position and its exit rules until flat:

- DIP sleeve (dip-buy / snapback): earns on dip-then-recover (V-shape) days --
  the regime the previous edition's live forensic identified as the winner
  (the live top 5 were all dip/bounce buyers; a pure trend rider entered
  ~400 bars late and kept +$1.10 of a +$17.63 swing). Parameters are the
  regime-tested set from the sleeve's own sweep record: dip band 0.50%..1.6%
  off the 240-bar rolling high, 2-bar low hold, >=33% recovery + close above
  a rising EMA20 + within 1.8% of the all-time high, size 93%, at most 2
  cycles (cycle 2 only after a >= +1.2% cycle 1), exits = +2.2% target /
  0.7% trailing giveback armed at +0.7% / proof-or-out 120 bars / dead-money
  300 bars / -2.5% catastrophe stop. NO tight stop: the simplest live winner
  held through a +$16 -> +$4 equity giveback.
- TREND sleeve (slow proven-trend rider): earns on clean trend days, which
  is what the dip sleeve structurally cannot catch. EMA60/240 with a 0.15%
  spread gate, 240-bar travel >= +1.5% for the first entry (+2.0% for a
  re-entry), size 95%, at most 2 entries, buffered exits (240-bar return
  <= 0, close < EMA240 - 0.6%, 2.5% trail off the peak close).

Shared: equity brakes (6% soft = flatten + 240-bar block, 10% hard =
flatten + block for good) and a $5 insurance trade at bar >= 1300 if no
order was ever sent, so the >= 1-trade race guardrail is met even on a
signal-less day.

Deterministic: no clock, no environment, no randomness; decisions use only
bars <= the current bar, orders fill at the next bar's open.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "vendor"))

# --- shared constants -------------------------------------------------------
DD_SOFT = 0.06           # equity drawdown: flatten + block DD_BLOCK bars
DD_HARD = 0.10           # equity drawdown: flatten + block for good
DD_BLOCK = 240
INSURANCE_BAR = 1300
INSURANCE_USD = 5.0
MIN_NOTIONAL_USD = 1.0
LIVE_SLIPPAGE_BPS = 200
WIDE_SLIPPAGE_BPS = 10000

# --- dip sleeve constants (regime-tested set) -------------------------------
DIP_HIGH_LOOKBACK = 240  # rolling-high window for the drawdown measure
DIP_THRESH = 0.0050      # drawdown from rolling high that arms a dip
DIP_MAX = 0.016          # deeper than this = possible collapse, do not catch
DIP_HOLD_BARS = 2        # dip low must hold this many bars (deceleration)
DIP_BOUNCE_PCT = 0.0008  # close must be at least this far above the dip low
DIP_RECOVERY_FRAC = 0.33  # ...and recovered >= this share of the dip depth
DIP_ATH_MAX_DD = 0.018   # no entries if close is > this below all-time high
DIP_EMA_SLOPE_BARS = 3   # entry needs close above EMA20, EMA20 rising
DIP_ENTRY_WINDOW = 20    # enter at most this many bars after the low
DIP_MIN_ENTRY_BAR = 90   # the tape must be developed before cycle 1
DIP_CYCLE2_MIN_GAIN = 0.012  # cycle 2 only after a cycle-1 gain >= this
DIP_ENTRY_FRAC = 0.93    # fraction of equity deployed per cycle
DIP_MAX_CYCLES = 2
DIP_PT = 0.022           # profit target from entry fill price
DIP_TRAIL_ARM = 0.007    # trailing giveback arms once peak gain >= this
DIP_TRAIL_GIVEBACK = 0.007
DIP_CATA_STOP = 0.025    # catastrophe stop from entry fill price (far)
DIP_PROOF_BARS = 120     # proof-or-out: peak gain must reach DIP_PROOF_GAIN
DIP_PROOF_GAIN = 0.006
DIP_DEAD_BARS = 300      # dead-money exit: held this long, gain < DIP_DEAD_GAIN
DIP_DEAD_GAIN = 0.005
DIP_MAX_HOLD = 600       # bars; dead-money backstop

# --- trend sleeve constants (sweep-tested set) ------------------------------
TR_EMA_FAST = 60
TR_EMA_SLOW = 240
TR_RET_WIN = 240
TR_ENTRY_FRAC = 0.95
TR_ENTRY_SPREAD = 0.0015  # EMA60/EMA240 spread gate
TR_ENTRY_RET = 0.015      # first entry: 240-bar travel >= +1.5%
TR_REENTRY_RET = 0.020    # re-entry: >= +2.0%
TR_EXIT_BUFFER = 0.006    # exit when close < EMA240 * (1 - this)
TR_TRAIL_STOP = 0.025     # exit 2.5% below the peak close
TR_MAX_ENTRIES = 2


class DuoCore:
    """Bar-by-bar dual-sleeve strategy in dollar/coin floats.

    decide(i, close, cash, qty, equity) -> list of orders, each either
    ("buy", notional_usd, slippage_bps) or ("sell_all", None, slippage_bps).
    qty is coin quantity held; equity/cash in dollars. The caller owns fills.
    Exactly one position at a time; self.sleeve names its owner.
    """

    def __init__(self):
        self.closes = []
        self.peak_equity = 0.0
        self.blocked_until = -1
        self.block_forever = False
        self.sent = 0              # orders sent (for the insurance rule)
        self.micro = False
        # shared market state
        self.ath = 0.0             # all-time high close this window
        self.ema20 = None
        self.ema20_hist = []
        self.ema60 = None
        self.ema240 = None
        # position state (sleeve is None while flat)
        self.sleeve = None
        self.entry_price = None    # approx fill price (close at decision)
        self.peak_close = None
        self.entry_bar = -1
        # dip sleeve state
        self.dip_low = None
        self.dip_high = None
        self.dip_low_bar = -1
        self.bars_since_new_low = 0
        self.dip_cycles = 0
        self.last_dip_gain = None
        self.repair_above = None   # after a losing dip cycle, price must
                                   # exceed the old entry before re-entry
        # trend sleeve state
        self.trend_entries = 0
        # shared loss discipline: two losing cycles (any sleeve) stand the
        # bot down for the rest of the window -- in chop the sleeves
        # otherwise take turns bleeding; on trend/V days the first cycles
        # usually win, so the cap rarely binds there.
        self.losses = 0

    # -- internal helpers ---------------------------------------------------
    def _reset_dip(self):
        self.dip_low = None
        self.dip_high = None
        self.dip_low_bar = -1
        self.bars_since_new_low = 0

    def _open(self, sleeve, i, close):
        self.sleeve = sleeve
        self.entry_price = close
        self.peak_close = close
        self.entry_bar = i
        self._reset_dip()

    def _close(self):
        self.sleeve = None
        self.entry_price = None
        self.peak_close = None
        self.entry_bar = -1
        self._reset_dip()

    # -- main decision ------------------------------------------------------
    def decide(self, i, close, cash, qty, equity):
        self.closes.append(close)
        if close > self.ath:
            self.ath = close
        k20 = 2.0 / (20 + 1)
        self.ema20 = close if self.ema20 is None else self.ema20 + k20 * (close - self.ema20)
        self.ema20_hist.append(self.ema20)
        k60 = 2.0 / (TR_EMA_FAST + 1)
        self.ema60 = close if self.ema60 is None else self.ema60 + k60 * (close - self.ema60)
        k240 = 2.0 / (TR_EMA_SLOW + 1)
        self.ema240 = close if self.ema240 is None else self.ema240 + k240 * (close - self.ema240)
        holding = qty > 0.0

        # Equity brakes first (shared): flatten + block on deep drawdown.
        if equity > self.peak_equity:
            self.peak_equity = equity
        peak = self.peak_equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
        if holding and dd > DD_HARD:
            self.block_forever = True
            self._close()
            self.sent += 1
            return [("sell_all", None, LIVE_SLIPPAGE_BPS)]
        if holding and dd > DD_SOFT:
            self.blocked_until = i + DD_BLOCK
            self.peak_equity = equity
            self._close()
            self.sent += 1
            return [("sell_all", None, LIVE_SLIPPAGE_BPS)]

        if holding:
            if self.peak_close is None or close > self.peak_close:
                self.peak_close = close
            if self.sleeve == "trend":
                return self._trend_exit(i, close)
            return self._dip_exit(i, close)

        return self._flat(i, close, cash, equity)

    # -- exits, routed by the owning sleeve ----------------------------------
    def _dip_exit(self, i, close):
        entry = self.entry_price if self.entry_price else close
        gain = close / entry - 1.0
        peak_gain = (self.peak_close / entry - 1.0) if entry else 0.0
        held_bars = i - self.entry_bar
        exit_now = False
        if gain >= DIP_PT:
            exit_now = True                       # profit target
        elif peak_gain >= DIP_TRAIL_ARM and close <= self.peak_close * (1.0 - DIP_TRAIL_GIVEBACK):
            exit_now = True                       # wide trailing giveback
        elif gain <= -DIP_CATA_STOP:
            exit_now = True                       # far catastrophe stop
        elif held_bars >= DIP_PROOF_BARS and peak_gain < DIP_PROOF_GAIN:
            exit_now = True                       # proof-or-out (no progress)
        elif held_bars >= DIP_DEAD_BARS and gain < DIP_DEAD_GAIN:
            exit_now = True                       # dead money (early)
        elif held_bars >= DIP_MAX_HOLD:
            exit_now = True                       # dead money (backstop)
        if exit_now:
            if gain < 0:
                self.losses += 1
            if self.sleeve == "dip":
                self.last_dip_gain = gain
                if gain < 0:
                    # Losing cycle: the trend must repair (price back above
                    # the old entry) before any dip re-entry is allowed.
                    self.repair_above = entry
            self._close()
            self.sent += 1
            return [("sell_all", None, LIVE_SLIPPAGE_BPS)]
        return []

    def _trend_exit(self, i, close):
        closes = self.closes
        ret240 = close / closes[-1 - TR_RET_WIN] - 1.0 if len(closes) > TR_RET_WIN else None
        exit_now = False
        if ret240 is not None and ret240 <= 0:
            exit_now = True
        elif len(closes) >= TR_EMA_SLOW and self.ema240 is not None \
                and close < self.ema240 * (1.0 - TR_EXIT_BUFFER):
            exit_now = True
        elif self.peak_close is not None and close <= self.peak_close * (1.0 - TR_TRAIL_STOP):
            exit_now = True
        if exit_now:
            entry = self.entry_price if self.entry_price else close
            if close / entry - 1.0 < 0:
                self.losses += 1
            self._close()
            self.sent += 1
            return [("sell_all", None, LIVE_SLIPPAGE_BPS)]
        return []

    # -- flat: entries, dip sleeve first (its entries are time-critical) -----
    def _flat(self, i, close, cash, equity):
        # Insurance first (only if nothing was ever sent): one $5 buy so a
        # >= 1-trade guardrail is met even on a signal-less day. The micro
        # position is managed by the dip exit set, like the sleeve's own.
        if self.sent == 0 and i >= INSURANCE_BAR and not self.micro:
            self.micro = True
            self.sent += 1
            self.sleeve = "ins"
            self.entry_price = None
            self.peak_close = close
            self.entry_bar = i
            self._reset_dip()
            return [("buy", INSURANCE_USD, WIDE_SLIPPAGE_BPS)]
        if self.block_forever or i < self.blocked_until:
            return []
        if self.losses >= 2:
            return []   # two losing cycles: stand down, keep the powder dry
        order = self._dip_entry(i, close, cash, equity)
        if order:
            return order
        return self._trend_entry(i, close, cash, equity)

    def _dip_entry(self, i, close, cash, equity):
        if self.dip_cycles >= DIP_MAX_CYCLES:
            return []
        if self.dip_cycles == 0 and i < DIP_MIN_ENTRY_BAR:
            return []
        if (self.dip_cycles >= 1 and self.last_dip_gain is not None
                and self.last_dip_gain < DIP_CYCLE2_MIN_GAIN):
            return []   # cycle 2 only compounds a proven swing

        # ---- dip tracking ----
        lookback = self.closes[-DIP_HIGH_LOOKBACK:]
        high = max(lookback)
        dd_from_high = close / high - 1.0
        if dd_from_high <= -DIP_THRESH and dd_from_high >= -DIP_MAX:
            if self.dip_low is None:
                self.dip_high = high
            if self.dip_low is None or close < self.dip_low:
                self.dip_low = close
                self.dip_low_bar = i
                self.bars_since_new_low = 0
            else:
                self.bars_since_new_low += 1
        elif dd_from_high < -DIP_MAX:
            # Fell through the dip zone: possible collapse. Never catch it;
            # reset and require a fresh dip later.
            self._reset_dip()
            return []
        else:
            # Recovered above the dip threshold: the dip is over unless we
            # are mid-bounce watch (dip_low set, still inside the window).
            if self.dip_low is None:
                return []
            if i - self.dip_low_bar > DIP_ENTRY_WINDOW:
                self._reset_dip()
                return []

        # ---- bounce confirmation ----
        if self.dip_low is not None:
            bars_since_low = i - self.dip_low_bar
            dip_depth = (self.dip_high - self.dip_low) if self.dip_high else 0.0
            recovered = close - self.dip_low
            ema_ok = (self.ema20 is not None and close > self.ema20
                      and len(self.ema20_hist) > DIP_EMA_SLOPE_BARS
                      and self.ema20 > self.ema20_hist[-1 - DIP_EMA_SLOPE_BARS])
            ath_ok = self.ath > 0 and close >= self.ath * (1.0 - DIP_ATH_MAX_DD)
            repair_ok = self.repair_above is None or close > self.repair_above
            if (self.bars_since_new_low >= DIP_HOLD_BARS
                    and 1 <= bars_since_low <= DIP_ENTRY_WINDOW
                    and close >= self.dip_low * (1.0 + DIP_BOUNCE_PCT)
                    and dip_depth > 0 and recovered >= DIP_RECOVERY_FRAC * dip_depth
                    and ema_ok and ath_ok and repair_ok
                    and len(self.closes) >= 2
                    and close > self.closes[-2]):
                notional = min(DIP_ENTRY_FRAC * equity, cash)
                if notional >= MIN_NOTIONAL_USD:
                    self.dip_cycles += 1
                    self.sent += 1
                    self._open("dip", i, close)
                    return [("buy", notional, LIVE_SLIPPAGE_BPS)]
        return []

    def _trend_entry(self, i, close, cash, equity):
        closes = self.closes
        if len(closes) < TR_EMA_SLOW or self.trend_entries >= TR_MAX_ENTRIES:
            return []
        if self.ema60 is None or self.ema240 is None:
            return []
        if not self.ema60 > self.ema240:
            return []
        if self.ema60 / self.ema240 - 1.0 < TR_ENTRY_SPREAD:
            return []
        if not close > self.ema240:
            return []
        ret240 = close / closes[-1 - TR_RET_WIN] - 1.0 if len(closes) > TR_RET_WIN else None
        if ret240 is None:
            return []
        needed = TR_ENTRY_RET if self.trend_entries == 0 else TR_REENTRY_RET
        if ret240 < needed:
            return []
        # Clean-tape veto: if the lookback window contains a real dip
        # (>= DIP_THRESH off its rolling high), the tape is a dip-recovery,
        # not a clean trend -- the dip sleeve owns those days. A trend entry
        # straight after a sharp recovery buys the peak before the second
        # dip (the merged bot's fast-V windows bled exactly there).
        run_high = 0.0
        worst_dd = 0.0
        for c in closes[-TR_RET_WIN:]:
            if c > run_high:
                run_high = c
            if run_high > 0:
                worst_dd = min(worst_dd, c / run_high - 1.0)
        if worst_dd <= -DIP_THRESH:
            return []
        notional = min(TR_ENTRY_FRAC * equity, cash)
        if notional >= MIN_NOTIONAL_USD:
            self.trend_entries += 1
            self.sent += 1
            self._open("trend", i, close)
            return [("buy", notional, LIVE_SLIPPAGE_BPS)]
        return []


# ---------------------------------------------------------------------------
# Harness wrapper (agent-fun/bars-replay/v1 via the vendored shim).
# Only runs when executed as a script; importing this module for a test
# driver does not start the wire loop.
# ---------------------------------------------------------------------------

_B = {}


def _on_hello(state):
    hello = state.hello
    sym_info = hello["symbols"][0]
    _B.clear()
    _B.update(
        symbol=sym_info["symbol"],
        price_scale=sym_info["price_scale"],
        qty_scale=sym_info["qty_scale"],
        quote_scale=hello["capital"]["quote_scale"],
        core=DuoCore(),
    )


def _on_batch(state, batch):
    bar = None
    for b in batch["bars"]:
        if b["symbol"] == _B["symbol"]:
            bar = b
            break
    if bar is None:
        return []
    ps, qs, cs = _B["price_scale"], _B["qty_scale"], _B["quote_scale"]
    close = int(bar["c"]) / 10 ** ps
    cash = state.cash / 10 ** cs
    equity = state.equity / 10 ** cs
    held = state.positions.get(_B["symbol"])
    qty = (held.qty / 10 ** qs) if held is not None else 0.0
    i = state.seq - state.warmup_bars
    if state.warming_up:
        # Build history only; the core needs the closes.
        _B["core"].closes.append(close)
        return []
    orders = []
    for side, amount, slip in _B["core"].decide(i, close, cash, qty, equity):
        if side == "buy":
            orders.append({"side": "buy", "symbol": _B["symbol"],
                           "notional_quote_units": int(amount * 10 ** cs),
                           "max_slippage_bps": slip})
        else:
            orders.append({"side": "sell", "symbol": _B["symbol"],
                           "qty": int(qty * 10 ** qs),
                           "max_slippage_bps": slip})
    return orders


if __name__ == "__main__":
    import makefun_bars
    makefun_bars.run(_on_batch, on_hello=_on_hello)
