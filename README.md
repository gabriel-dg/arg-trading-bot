# arg-trading-bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

English | [Español](README.es.md)

An educational, broker-agnostic trading bot for the Argentine market (BYMA stocks and
CEDEARs). It runs in **paper mode by default**: a deterministic synthetic market, no
network, no account, no credentials.

This project is shared for people who want to get into algorithmic trading on the
Argentine market and learn how a bot of this kind is put together: broker adapters,
order reconciliation, market sessions, risk limits and an example strategy. It
publishes no trading results and makes no claim that the strategy works; it is a
starting point to read, run, question and improve.

> **Not financial advice. Use at your own risk.** This is a learning project. Nothing in
> this repository is a recommendation to buy or sell any security. Trading involves
> risk of loss; past or simulated results do not guarantee future results. You are
> solely responsible for anything you do with this code, especially in live mode.
>
> **Not affiliated with, endorsed by or sponsored by InvertirOnline (IOL), BYMA or any
> broker.** IOL is mentioned only to describe the first broker adapter. All trademarks
> belong to their owners.

## Quickstart (paper mode, under 5 minutes)

Requires Python 3.10+.

```bash
git clone https://github.com/gabriel-dg/arg-trading-bot.git
cd arg-trading-bot
python -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

argbot paper --out paper-run      # 3 simulated sessions, about a minute
```

You get a JSON report (orders, fills, open positions, marked equity) and the full
durable state in `paper-run/`. Try other seeds and lengths:

```bash
argbot paper --out paper-run-2 --days 5 --seed 42
argbot watch-positions --state-dir paper-run --once
argbot monitor --state-dir paper-run --once --dry-run --as-of-heartbeat
argbot check-config --config configs/examples/strategy.example.json
pytest -q                         # offline test suite
```

Paper fills are optimistic (a limit order fills in full at its limit as soon as the
book reaches it). Use paper mode to understand the mechanics, not to estimate returns.

## What is inside

```
argbot/
  brokers/
    base.py        Broker + MarketData protocols, OrderView, AccountView
    paper.py       SyntheticExchange, PaperBroker, PaperMarket (default)
    iol/           IOL adapter: auth, client, order/account readers, market data, budget
  execution.py     durable order ledger, reconciliation before every send, never replays
  strategy/
    rules.py       pure entry and stop rules (the example strategy)
    candidate.py   sessions, quotes, risk bounds, entries, stops, recovery
    daily.py       daily indicators (EMA, ATR, drawdown from recent high)
  market.py        broker-neutral validation of quotes and daily history
  exchange.py      BCBA holiday calendar and helpers to count trading sessions
  sessions.py      BYMA trading days (reviewed holiday calendar)
  lab/             offline research: replay and compare profiles over CSV data
  watch_positions.py  read-only terminal positions dashboard (local state only)
  agents/          optional technical monitor (off by default, observe-only)
  live.py          gated live runner (IOL)
configs/examples/  example strategy configuration and an example ticker list
examples/          synthetic datasets for the lab
tests/             offline tests (synthetic fixtures, placeholder symbols)
```

## Start here

To see how the bot is put together, read these files in this order:

1. `argbot/brokers/base.py`: the `Broker` and `MarketData` contracts, and the canonical quote shape.
2. `argbot/brokers/paper.py`: the default implementation, an in-process synthetic market with no network.
3. `argbot/execution.py`: the durable order ledger. It reconciles the account before every send and never replays a submission.
4. `argbot/strategy/rules.py`: the pure entry and stop rules of the example strategy, with no broker I/O.
5. `argbot/strategy/candidate.py`: the session loop that wires those rules to quotes, risk bounds, entries, stops and recovery.
6. `argbot/cli.py` and `argbot/paper_session.py`: how a paper run starts. `argbot/live.py` is the gated live path.

`argbot/brokers/iol/` is the first real broker adapter. It is easier to read after the contracts and the paper path are clear.

## The example strategy

The configured strategy is one example among many you can build by changing parameters or writing new rules. Every number in
`configs/examples/strategy.example.json` is a generic illustration, not a tuned or
recommended value.

The example is a swing, buy-the-pullback strategy:

1. **Candidates**: stocks whose last close is at least `caida_umbral` (drawdown threshold) below their high of
   the last `caida_lookback` (drawdown lookback) sessions, ranked by the size of that drawdown.
   The shortlist is built by `filtro_candidatos` (candidate filter).
2. **Entry**: only in fixed entry windows (`entry_windows`, before `entry_cutoff`), when
   the price sits in the lower part of the day's range (`mec_rango_dia_max`, maximum day-range position)
   and at or under the day's VWAP (`mec_vwap_max`, maximum VWAP deviation). Passive limit buys at the bid are optional (`compra_pasiva`, passive buy).
   `vwap_filtro_pct` (VWAP filter) can also drop names already extended over VWAP when the shortlist is built.
3. **Sizing and risk**: at most `max_capital_pct` of capital per position, at most
   `max_concurrent` positions, a total exposure cap, a daily loss brake, an optional
   sector cap and a regime factor (`regimen_umbral`, regime threshold; `regimen_factor`, regime factor)
   that shrinks new positions when few local stocks are in a daily uptrend (CEDEARs are left out of that breadth).
4. **Exits**: a protective stop at `stop_atr_mult` daily ATRs under the entry, then a
   chandelier trailing stop, a cap (`trail_giveback_max`) on how far the price may fall back from the
   position's peak, a stop that rises once the price has moved a set distance above the
   entry (`lock_ganador_umbral`, winner-lock threshold; `lock_ganador_frac`, winner-lock fraction),
   optional stop confirmations over consecutive polls (`stop_confirm_polls`), and
   an optional "phase 1" (`stop_fase1_ruedas`, phase-1 sessions) where only the protective stop runs for the first sessions.

Config keys are in Spanish where they come from the original trading vocabulary; the others stay in English. A translation appears in parentheses the first time a Spanish key is used, and in the tables below.

### Operating keys

| Key | English meaning | What it does |
| --- | --- | --- |
| `schema` | schema version | Must be `1`. Identifies this configuration shape. |
| `status` | publication status | The example is `candidate_not_authorized_for_live`. `synthetic` is also accepted (paper mode sets it). |
| `broker_order_timezone` | broker order timezone | Timezone used to interpret naive timestamps on broker order details. Only `America/Argentina/Buenos_Aires` is accepted. |
| `account_id` | account identity | Must match the identity the broker adapter reports, and keys the account lease. `null` in the example; required for live, filled by paper mode. |
| `external_positions` | external positions | Optional. Holdings in the account that the bot does not manage (`{"SYMBOL": quantity}`), so reconciliation accepts them. Read when the state folder is created. |
| `assigned_capital` | assigned capital | Capital used as the base for sizing and the exposure cap. `null` in the example; required for live, filled by paper mode. |
| `fee_reserve` | fee reserve | Fraction reserved on every buy for commission, market fees and VAT. Set it to your broker's real total cost. |
| `max_exposure_pct` | maximum exposure | Cap on total exposure as a fraction of the sizing capital. |
| `max_daily_loss_pct` | maximum daily loss | Daily loss brake. New entries stop when marked equity falls this fraction below the day's baseline. |
| `universe` | universe | Symbols the bot may trade. |
| `cedears` | CEDEARs | CEDEAR symbols in the universe. They are left out of the regime breadth measure. |
| `poll_seconds` | poll interval | Seconds between regular rounds. |
| `session_end` | session end | End of the bot's session (ART). Accepted values: `17:00` or `18:00`. If omitted, the end is `17:00`. |
| `entry_windows` | entry windows | Times (ART) when new entries may be considered, each before `entry_cutoff`. |
| `entry_cutoff` | entry cutoff | No new buys after this time (ART). |
| `max_quote_age_seconds` | maximum quote age | Quotes older than this, or from "the future", are rejected for new entries. |
| `managed_quote_age_seconds` | managed quote age | Looser age limit when managing open positions (stops). Must sit between `max_quote_age_seconds` and `poll_seconds`. |
| `max_spread_pct` | maximum spread | Wide spreads block entries, never exits. |
| `max_scale_change_pct` | maximum scale change | If the last price differs from the previous close by more than this, the quote is treated as a possible unadjusted split. |
| `http_session_budget` | HTTP session budget | Maximum HTTP requests per session, including reads, writes and authentication. |
| `http_management_reserve` | HTTP management reserve | Part of that budget held back so ordinary market-data requests stop first, leaving room for orders, account reads and priority quotes. |

### Strategy keys

These live under the `strategy` object.

| Key | English meaning | What it does |
| --- | --- | --- |
| `max_capital_pct` | maximum capital per position | Maximum share of the day's capital for each new position, before the regime factor. |
| `max_concurrent` | maximum concurrent positions | Maximum open positions (plus in-flight buys) at the same time. |
| `sizing_overshoot_max` | sizing overshoot | The published candidate accepts only `1.0`, which means size is rounded down to whole shares. |
| `filtro_candidatos` | candidate filter | How the shortlist is built. The engine can rank by drawdown (`caida`), require a daily uptrend (`bull`), or take the universe as-is (`ninguno`). The published validator only accepts `caida`. |
| `caida_lookback` | drawdown lookback | Sessions used for the reference high. |
| `caida_umbral` | drawdown threshold | Minimum (negative) drawdown of the last close from that high for a symbol to qualify. |
| `mec_rango_dia_max` | maximum day-range position | At entry, the price must sit this far or less through the day's [low, high] range (`0.40` is the lower 40%). |
| `mec_vwap_max` | maximum VWAP deviation | At entry, the price must be at or under VWAP times `(1 + this)`. |
| `vwap_filtro_pct` | VWAP filter | When the shortlist is built, symbols already this far above VWAP are dropped. `<= 0` disables it. It is not applied in the last entry window. |
| `regimen_umbral` | regime threshold | If the share of local stocks in a daily uptrend is below this, new positions are shrunk. |
| `regimen_factor` | regime factor | Size multiplier used when breadth is below `regimen_umbral`. `1.0` would leave the size unchanged. |
| `trail_stop_pct` | trailing stop (percent) | Fallback trailing stop, as a fraction of price, when there is no daily ATR. |
| `trail_atr_mult` | trailing stop (ATR multiple) | Chandelier trailing stop: the peak since entry minus this many daily ATRs. |
| `stop_atr_mult` | protective stop (ATR multiple) | Protective stop: the entry minus this many daily ATRs. |
| `trail_giveback_max` | maximum give-back from peak | The stop is never further than this fraction below the position's peak. |
| `lock_ganador_umbral` | winner-lock threshold | Once the peak is at least this fraction above the entry, the winner lock engages. |
| `lock_ganador_frac` | winner-lock fraction | Fraction of (peak minus entry) that the stop then keeps. |
| `stop_confirm_polls` | stop confirmation polls | Consecutive polls under the stop before a sale is sent. |
| `stop_fase1_ruedas` | phase-1 sessions | First sessions after entry during which only the protective stop runs. `0` turns this off. |
| `compra_pasiva` | passive buy | If true, limit buys are placed at the bid and do not cross the spread. |
| `buy_timeout_min` | buy timeout | Minutes after which an unfilled buy is cancelled. |
| `sell_timeout_min` | sell timeout | Minutes after which an unfilled sell is cancelled. |
| `max_trades_dia` | maximum trades per day | Once this many closes have happened today, new entries stop. |
| `cooldown_loss_min` | loss cooldown | Minutes a symbol is paused after a losing close. |

### Argentine market details it handles

- **Trading hours**: the session opens at 10:30 ART and ends at `session_end`, which
  accepts only `17:00` or `18:00`. The example uses `18:00`, BYMA's current close for
  24 h stocks and CEDEARs (check BYMA's schedule). The 10:30 open and the two allowed
  closes are fixed in the code (`argbot/strategy/candidate.py`, `argbot/live.py`), so a
  schedule change means editing code. Buys are cancelled 15 minutes before the end; stop
  sales keep working until the end. With `17:00`, nothing watches stops or open orders
  between 17:00 and 18:00 even though the market is still trading.
- **Settlement**: orders are sent at 24 h (T+1). Contado inmediato (CI) is not used. The
  ledger understands that the broker can record a sale at 48 h or 72 h and that the cash
  arrives later.
- **Fees and taxes**: `fee_reserve` reserves commission, market fees and VAT on every buy.
  Set it to your broker's real total cost. Taxes (income tax on gains, Bienes Personales)
  are not modelled.
- **Lots and units**: whole shares only (lot size 1 for stocks and CEDEARs); quotes with
  other lot sizes are rejected.
- **Liquidity**: wide spreads (`max_spread_pct`) and thin trading
  (`entry_liquidity`, trades per minute) block entries, never exits. `entry_liquidity` is
  optional and is not set in the example configuration.
- **Tickers**: BYMA symbols such as local stocks and CEDEARs (`cedears`).
- **Holidays**: the calendar in `argbot/exchange.py` and `argbot/sessions.py` covers
  **2026 only** (holidays plus closures announced during the year). Update it by hand
  every year from BYMA's official calendar, including non-working days set for tourism.
  Live mode refuses to start on a day outside a reviewed year, so from 1 January 2027 it
  will not start until the 2027 holidays are added.
- **Data hygiene**: quotes older than `max_quote_age_seconds` or from "the future"
  are rejected; daily history needs 35 valid sessions.
- **Corporate actions**: live mode reads IOL's unadjusted daily history. A split shows up
  as a jump between the quote and the last close (`max_scale_change_pct`); it is
  **detected and blocked, not adjusted**: that symbol's quotes are rejected, so it gets no
  entries and its stop is not evaluated on the inconsistent price. Cash dividends and
  CEDEAR ratio changes are not adjusted either and can distort the drawdown, the ATR and
  the stops around the ex-date.

## Adding a broker (ALyC)

Implement two small interfaces from `argbot/brokers/base.py`:

- `Broker`: `submit(intent) -> broker_id`, `order(broker_id) -> OrderView`,
  `cancel(broker_id)`, `account() -> AccountView`. Report **confirmed** fills and fees
  only; never infer a fill from an accepted order.
- `MarketData`: `quote(symbol)` in the canonical payload shape documented in `base.py`,
  and `history(symbols, session_date, previous_session)`.

`argbot/brokers/paper.py` is the smallest complete example; `argbot/brokers/iol/` is a
real one. Contributions of other adapters are welcome (see [CONTRIBUTING.md](CONTRIBUTING.md)).
Adapters must respect each broker's API terms.

## Offline lab

```bash
argbot lab compare --data examples/synthetic.csv \
  --configs configs/lab/01_reference.json configs/lab/08_combined.json \
  --events examples/synthetic_events.jsonl --out runs/lab-demo
argbot lab validate-settings
argbot lab import-market --help   # convert your own tick CSVs
```

The bundled datasets are synthetic (fictional companies and events) and measure
nothing about real markets.

The lab is a **separate, simplified model**, not the live strategy, and has no parity
with it. `lab compare` uses a fixed session schedule set in `argbot/lab/engine.py` and
ignores the strategy's `entry_windows` and `session_end`; `configs/bot_settings.json`
accepts entry times only up to 16:40. Costs differ too: about 0.42% per side in the lab
(0.30% commission plus 0.05% market fee, plus VAT), 0.6% in the paper broker and a 0.9%
`fee_reserve` in the example strategy. Use it to compare ideas with each other, not to
predict what the live bot would do.

## Positions dashboard (read-only)

`argbot watch-positions` displays a terminal table of cash, open positions, stops,
and daily valuation from a local `execution-state.json` file:

```bash
argbot watch-positions --state-dir paper-run --once
argbot watch-positions --state-dir state/live --config configs/local/my.json
```

It runs only when invoked and is strictly read-only: it never writes files,
never acquires the account lease, and never calls any broker or LLM. `--config`
defaults to the example strategy (fine for `paper-run`); for a live or custom
state folder, pass the same `--config` the bot ran with so the daily brake,
winner-lock threshold and fee reserve match. Displayed prices are whatever the bot
last recorded in that state folder (simulated prices for paper runs, not live quotes
and not financial advice).

## Technical monitor (optional, off by default)

`argbot monitor` is an observe-only health check for a running bot. It reads the local
`execution-state.json`, computes deterministic signals (stale heartbeat, reconciliation
not confirmed, unresolved orders, delayed data, HTTP budget, premature stop and others)
and asks a model, through the Claude Code CLI, for a short technical diagnosis in Spanish.
It never places, cancels or changes orders, never contacts the broker and never restarts
the bot. Reports go to `runs/monitor/<day>/` (gitignored): `latest-report.txt`,
`alerts.jsonl` for anything that is not ok, and one folder per check.

It is off by default. To opt in:

1. Install Claude Code and sign in, so `claude` is on your PATH. The monitor runs on your
   own Claude Code login and plan, and its usage counts against them.
2. Copy `configs/examples/monitor.example.json` to `configs/local/monitor.json` and set
   `"enabled": true`. The other values are the reviewed policy (model `claude-sonnet-5-5`,
   effort `low`, one check every 900 s from 10:30 ART, at most 31 checks, 120 s timeout,
   `observe_only`); the monitor refuses to run if any of them changes.
3. Run it next to the bot, with the same `--config` the bot uses:

```bash
argbot monitor --state-dir state/live --config configs/local/my.json --policy configs/local/monitor.json
argbot monitor --state-dir paper-run --once --dry-run --as-of-heartbeat   # preview, no model call
```

`--once` makes a single check now. `--as-of-heartbeat` observes a finished paper run at its
own last heartbeat. `--dry-run` writes `snapshot.json` (exactly what would be sent) and
calls no model; it works without opting in.

**What leaves your computer.** Only when enabled and not in `--dry-run`, each check sends
one prompt to Anthropic through the Claude Code CLI: fixed instructions plus a technical
summary with the observation time, session date and end, the time new incidents are
counted from, phase, stop time, whether the close was normal and whether a scheduled stop
was recorded, status code, heartbeat and last-cycle ages, poll interval, pause flags,
entry-block code, ages of held quotes and of holdings valued at their live bid (without
symbols), whether valuation is available and how long a data gap has lasted, whether cycle
telemetry is ok, side, state and age of active orders, reconciliation result, reason code
and age, HTTP request counters, counts of exclusion codes, incident codes with their times
and their count, the opening flag with counts of opening incident codes, a fixed note on
the limits of the source and the deterministic signals. It never sends the account id,
credentials, symbols, quantities, prices, cash, equity or PnL. Free-text errors are
replaced by a fixed list of codes (`redacted_diagnostic` otherwise). A verified normal
close is reported without a model call. What you send is processed under your own terms
with Anthropic.

**How the model is boxed in.** The CLI runs from an empty temporary folder with no tools,
MCP servers, settings, slash commands or saved session, and receives only a short list of
environment variables (PATH, HOME, temporary and Windows system folders,
`CLAUDE_CONFIG_DIR`): broker credentials and any `ANTHROPIC_API_KEY` are not passed. An
answer is accepted only from the reviewed model, with the expected JSON shape and no tool
use. The model cannot lower the severity of a deterministic signal, and a failed check is
reported as critical instead of as a healthy bot.

**Limits.** It reads recorded state only: it does not prove the process is alive between
checks and does not query the broker. After the session end, a bot that died without
recording its stop shows only as an unverified close (a warning). There is no sound or
desktop notification; read `latest-report.txt`. It is a diagnostic aid, not a supervisor,
and not financial advice.

## Going live

Live mode is off unless you deliberately turn it on, and it is your responsibility.

0. Check that your broker's terms allow automated trading through its API.
1. Copy `configs/examples/strategy.example.json` to `configs/local/` (gitignored), set
   your own universe, `assigned_capital` and every parameter.
2. Set `account_id` to the identity the IOL adapter reports for your account:
   `IOL:inversion_Argentina_Pesos:<your IOL account number>`. If it does not match,
   `argbot live` stops before sending anything and says so.
3. If the account holds securities the bot must not manage, list them in
   `external_positions` (`{"SYMBOL": quantity}`); otherwise reconciliation fails. It is
   read only when the state folder is created.
4. Put `IOL_USERNAME` and `IOL_PASSWORD` in the process environment (for example with
   `export` or `$env:`). The bot reads those variables itself; it does not load a `.env`
   file. Tokens stay in memory only, unless you pass `token_path` to `IOLAuth` (a
   plaintext JSON cache; on Windows, `chmod 0600` has no effect). Live mode always uses
   IOL's production API; this project has no sandbox mode.
5. Start it and type the confirmation phrase when asked. Without both, nothing is sent.

   ```bash
   LIVE_TRADING=true argbot live --config configs/local/my.json --state-dir state/live
   ```

   ```powershell
   $env:LIVE_TRADING = "true"; argbot live --config configs/local/my.json --state-dir state/live
   ```

The bot reconciles the account before every order and stops sending when anything
disagrees. Never run another tool that trades the same account at the same time.

**Stops live inside the bot process.** The bot places no stop orders at the broker, and
there is no supervisor or automatic restart (the state records a `heartbeat_at` time; the
optional `argbot monitor` can report a stale one, but nothing restarts the bot). If the process, the
computer or the connection stops, open positions have no stop until the bot runs again.
Watch it yourself.

## Feedback and community

Feedback in Spanish or English is welcome:

- [Discussions](https://github.com/gabriel-dg/arg-trading-bot/discussions) for ideas,
  strategy discussions and questions.
- [Issues](https://github.com/gabriel-dg/arg-trading-bot/issues) for bugs
  (*Bug report*), strategy proposals (*Strategy proposal*) and questions (*Question*).
- Security problems: see [SECURITY.md](SECURITY.md).

Never share credentials, account numbers, balances or positions in public.

## License

MIT, see [LICENSE](LICENSE). Third-party notes in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
