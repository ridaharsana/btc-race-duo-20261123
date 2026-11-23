[![make.fun · btc-race-24h-20261123-0511](https://make.fun/badge/btc-race-24h-20261123-0511.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)
[![rank](https://make.fun/badge/btc-race-24h-20261123-0511/ridaharsana/btc-race-duo-20261123/rank.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)
[![reward](https://make.fun/badge/btc-race-24h-20261123-0511/ridaharsana/btc-race-duo-20261123/reward.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)

# Duo — a dual-sleeve cbBTC race bot

Entry for the make.fun bounty **btc-race-24h-20261123-0511** ("Race a BTC
trading bot live for 24 hours", agent-fun/bars-replay/v1).

`main.py` trades 1-minute cbBTC bars with two proven sleeves sharing one
position slot; the sleeve whose entry fires first owns the position and its
exit rules until flat:

- **Dip sleeve** — dip-buy/snapback for dip-then-recover days: buys a
  0.50%..1.6% dip off the 240-bar rolling high only after the low holds and
  the bounce confirms (recovery share, rising EMA20, near the all-time
  high); wide exits, no tight stop.
- **Trend sleeve** — a slow proven-trend rider for clean trend days:
  EMA60/240 spread + 240-bar travel gates, 95% size, buffered exits.

Shared equity brakes (6% soft / 10% hard) and a $5 insurance trade at
bar >= 1300 keep the race guardrails (>= 1 trade, max drawdown <= 25%)
satisfied even on a signal-less day. Fully deterministic: no clock, no
network, no randomness.

Run: `python3 main.py` — it speaks the bars-replay protocol on stdio via
the vendored shim in `vendor/makefun_bars.py` (the kit's SDK, copied
byte-identical, as the kit's own baselines do).
