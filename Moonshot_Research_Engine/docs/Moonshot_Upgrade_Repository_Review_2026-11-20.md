# Repository review for the Moonshot scanner upgrade

Checked 20 November 2026. This review compares six upstream projects and selects concrete engineering patterns for our research/reporting application. It does not claim these projects predict moonshots, and it does not import their order execution, wallet, or account features.

## Verified repository references

Commit dates are the default-branch HEAD observed during this review, not promises of future maintenance. GitHub metadata and commit endpoints were read directly; no third-party repository was executed or installed for this review.

| Project | Exact observed revision | Commit date | License at reviewed source | Decision |
|---|---|---|---|---|
| [CCXT](https://github.com/ccxt/ccxt) | [bc7dff2c61f00292e07d2034a749026f3fb3a0d2](https://github.com/ccxt/ccxt/commit/bc7dff2c61f00292e07d2034a749026f3fb3a0d2) | 19 Nov 2026, 18:26 UTC | MIT | Adapter/schema inspiration; optional future public-data dependency, not installed by this review |
| [Freqtrade](https://github.com/freqtrade/freqtrade) | [a025e7fb3b3acf0e90fd86a9f597aea5b528e1b7](https://github.com/freqtrade/freqtrade/commit/a025e7fb3b3acf0e90fd86a9f597aea5b528e1b7) | 19 Nov 2026, 20:14 UTC | GPL-3.0 | Validation methodology only; no source copied or runtime dependency |
| [Cryptofeed](https://github.com/bmoscon/cryptofeed) | [c8fd69b08849a66d1819859062d5ebf18c2271df](https://github.com/bmoscon/cryptofeed/commit/c8fd69b08849a66d1819859062d5ebf18c2271df) | 18 Nov 2026, 20:15 UTC | AGPL-3.0-or-later plus attribution term | Feed-integrity inspiration only; do not assume an older permissive license applies |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | [ef8e0ff9cfa664c55c4ef44ac247218ebeaffbf7](https://github.com/nautechsystems/nautilus_trader/commit/ef8e0ff9cfa664c55c4ef44ac247218ebeaffbf7) | 20 Nov 2026, 02:39 UTC | LGPL-3.0 | Architecture/replay inspiration; no trading engine embedded |
| [Hummingbot](https://github.com/hummingbot/hummingbot) | [3351adad0a09f6a57a202219d3e52fe794672421](https://github.com/hummingbot/hummingbot/commit/3351adad0a09f6a57a202219d3e52fe794672421) | 18 Nov 2026, 13:29 UTC | Apache-2.0 | Order-book integrity and connector test inspiration; no execution components imported |
| [Telethon — maintained upstream](https://codeberg.org/Lonami/Telethon) | [22e39ce3ee6382e5068cbc14a999935f1716526c](https://codeberg.org/Lonami/Telethon/commit/22e39ce3ee6382e5068cbc14a999935f1716526c) | 17 Nov 2026, 19:42 UTC | MIT | Existing Rose push watcher uses this library; account authorization and end-to-end activation remain separate |

All five GitHub repositories were non-archived at observation. Telethon's old [GitHub repository](https://github.com/LonamiWebs/Telethon) was archived on 21 February 2026 and explicitly points to Codeberg. Codeberg metadata was live and non-archived, with the reviewed commit bumping version 1.46. Its stable documentation still displayed 1.45.0; API compatibility must therefore be tested against the installed version.

Cryptofeed deserves special attention: GitHub's license metadata returned `NOASSERTION`, while the pinned [actual LICENSE](https://raw.githubusercontent.com/bmoscon/cryptofeed/c8fd69b08849a66d1819859062d5ebf18c2271df/LICENSE) clearly specifies AGPL and an additional attribution term. Its reviewed README requires Python 3.13+, unlike the present Python 3.11 environment. We do not vendor this code. The review records observed licenses rather than offering a legal interpretation of a proposed redistribution.

## What each project contributes

### CCXT: explicit exchange/product adapters and candle semantics

CCXT separates public market-data methods from private account/trading methods. Its manual warns that the latest OHLCV candle can remain incomplete until the interval closes. The useful pattern is a common adapter contract, explicit venue/product identifiers, bounded rate handling, and preservation of raw source semantics. [CCXT manual](https://github.com/ccxt/ccxt/wiki/manual).

For our scanner: normalize candles to open/close times, base volume, quote turnover, and `is_closed`; keep spot and perpetual data distinct; identify an unsupported market as a source gap. Do not silently substitute another venue's candles into an existing series. The current upgrade can implement a small public REST adapter directly, so installing the whole library is optional rather than a prerequisite.

### Freqtrade: test for lookahead and disclose fill assumptions

Freqtrade documents how full-dataframe calculations can accidentally let future candles influence past signals and offers an analysis method to detect changed indicators/entries. Its backtesting documentation makes execution assumptions explicit. These are useful review criteria for our independent backtester. [Lookahead analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/), [backtesting assumptions](https://www.freqtrade.io/en/stable/backtesting/).

For our scanner: use completed-bar inputs, fill only after a signal existed, include fees/slippage, resolve ambiguous same-candle stop/target touches conservatively, start the equity curve at zero, and reserve later periods for evaluation. Replay a prefix of data and check that the historical decision does not change when future bars are appended. No Freqtrade source or strategy is copied.

### Cryptofeed: keep event time separate from receipt time

The reviewed feed contract distinguishes venue event timestamps from receipt timestamps, exposes closed candles, identifies taker side, and preserves book sequence/checksum metadata where available. [Pinned README](https://raw.githubusercontent.com/bmoscon/cryptofeed/c8fd69b08849a66d1819859062d5ebf18c2271df/README.md), [pinned feed implementation](https://github.com/bmoscon/cryptofeed/blob/c8fd69b08849a66d1819859062d5ebf18c2271df/cryptofeed/feed.py).

For our scanner: preserve `observed_at` and `fetched_at` separately, never present fetch time as evidence that an old observation is current, and make missing timestamps explicit. A REST trade sample is not a continuous CVD series. A snapshot imbalance is not a verified liquidation tape. New streaming features must demonstrate continuity and gap recovery before their data may pass readiness gates.

### NautilusTrader: separate ingestion, decision and durable recovery

NautilusTrader documents adapters, data engines, messaging, lifecycle boundaries and state recovery. Its event-sourcing design orders replay by durable sequence rather than guessing ordering from timestamps. [Architecture](https://nautilustrader.io/docs/latest/concepts/architecture/), [event sourcing](https://nautilustrader.io/docs/latest/concepts/event_sourcing/).

For our scanner: ingestion writes a versioned observation/job, analysis produces a derived report, and delivery consumes a durable outbox. A web request must not be the only thing keeping the watcher alive. Restart recovery and idempotency are required. We can implement these patterns with a small SQLite-backed worker without embedding a Rust trading stack. No Nautilus source is copied.

### Hummingbot: a depth number needs an integrity contract

Hummingbot's connector architecture describes applying book snapshots/deltas in sequence, checking message IDs, and falling back to snapshots to repair missed changes. The separation between public order-book data and user/order streams is also relevant. [Connector architecture](https://hummingbot.org/connectors/connectors/architecture/), [pinned order-book tracker](https://github.com/hummingbot/hummingbot/blob/3351adad0a09f6a57a202219d3e52fe794672421/hummingbot/core/data_type/order_book_tracker.py).

For our scanner: label snapshot age and depth coverage, compute executable slippage for a stated notional, reject crossed/invalid books, and do not call a disconnected snapshot stream a live local book. If a future WebSocket book loses sequence continuity, it must resynchronize before a trade-readiness result can use it. Wallet/account/trade integrations remain outside this research application.

### Telethon: event-driven Rose reception with bounded handlers

Telethon exposes new/edit events, reconnect behavior, and missed-update catchup. Its client docs warn that slow sequential handlers can accumulate an unbounded queue. [Client documentation](https://docs.telethon.dev/en/stable/modules/client.html), [pinned MIT license](https://codeberg.org/Lonami/Telethon/raw/commit/22e39ce3ee6382e5068cbc14a999935f1716526c/LICENSE).

For our scanner: handlers should quickly persist source events, deduplicate by channel/message/version, then allow bounded analysis workers to do slow market-data work. Register handlers before catchup. Preserve the channel identity and restart watermark. The existing local `rose_monitor/push_watch.py` is the concrete integration point, but code availability is not proof that a Telegram session or delivery channel is active.

## Architecture comparison and concrete application

| Concern | Source inspiration | Our required behavior | Acceptance check |
|---|---|---|---|
| Public exchange data | CCXT | Stable normalized adapter; venue/product and source errors remain visible | Unsupported spot/perp cannot silently produce a mixed series |
| Token identity | Native registry plus primary project/exchange identity sources | Chain + contract, never aggregate all symbol-search hits | SOL/EVAA impostor fixtures excluded; unknown remains UNKNOWN |
| Closed-bar evidence | CCXT, Cryptofeed | Confirmations use completed candles only | Developing-bar changes do not alter a past confirmed decision |
| Honest strategy evaluation | Freqtrade methodology | No future leakage; realistic fills/costs; untouched evaluation period | Prefix replay invariant; first losing trade causes drawdown |
| Versioned events/recovery | Nautilus architecture | Durable job + derived report + outbox; sequence/idempotency | Restart after receipt does not lose or double-send an event |
| Depth and flow | Hummingbot, Cryptofeed | Snapshot/stream distinction; age and coverage; loss-of-sync invalidates stream | Out-of-order delta, crossed book and stale snapshot are rejected |
| Rose trigger | Telethon | New/edit events enqueue quickly; restart catchup | New post, edit, duplicate, disconnect/restart and offline delivery tested |
| Session reports | Python zoneinfo, named London/NY zones | UTC storage; AMT first/WIB second; independently shifting DST | November, March mismatch and weekend regressions |
| Probability claims | Statistical evaluation, not a copied score | Heuristic ranking separate from calibrated probability | No next-call percentage without documented out-of-sample calibration |

## Integration boundary

This is selective integration of engineering patterns, with independently written code, not a bundle of six trading bots. No upstream framework was cloned, executed, or installed by this review. The code written for session handling uses the Python standard library only.

The reviewed projects do not solve verified token identity, undisclosed unlocks, project fundamentals, source access, hosting availability, or predictive edge on our behalf. Those require their own data contracts and tests. Automated trading remains disabled; market-data API keys and exchange account permissions are not required for the public-data path.

Implemented in this research subtask:

- `moonshot_scanner/sessions.py`: `session_context(now=None)` and `next_session_runs(now=None, count=3)`.
- `moonshot_scanner/tests/test_sessions.py`: 11 passing regressions.
- Correct November mapping: 07:45 London = 11:45 AMT = 14:45 WIB.
- Correct March 2027 handling: US changes before UK; no manual permanent UTC offset.
- Analytical crypto windows remain distinct from NYSE cash open; holiday/macro events are explicitly unverified.
- No permanent favorable-hour or up/down score is generated.

The other scanner modules are being implemented/reviewed separately. Completion, deployment and live monitoring must be reported from their own test evidence, not inferred from this repository comparison.

## Primary scheduling references

- [IANA timezone database](https://www.iana.org/time-zones)
- [UK clock changes](https://www.gov.uk/when-do-the-clocks-change)
- [US DST rules](https://www.nist.gov/pml/time-and-frequency-division/popular-links/daylight-saving-time-dst)
- [NYSE hours and calendars](https://www.nyse.com/trade/hours-calendars)

Local metadata evidence: `work/scanner_repo_research/repository_metadata.json`, `telethon_metadata.txt`, `telethon_commits.txt`, and `telethon_license.txt`. These contain public source observations only, not authentication/session secrets.
