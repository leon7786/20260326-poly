# A9 / dashboard-v1

This folder contains A9's current dashboard website package for the 15-minute Polymarket crypto paper-trading system.

## Folder ownership

- `15min/A9/` is A9's workspace inside this repo.
- `15min/A9/dashboard-v1/` stores the current dashboard implementation snapshot.

## Files

- `polymarket_dashboard_5011.js`
  - current Node.js dashboard server used for the bot cockpit
- `README.md`
  - this overview document

## What this dashboard is

This is not a generic market dashboard.
It is an operator-facing **bot cockpit** for a 15-minute crypto Polymarket trading system.

The site is designed to answer these questions quickly:

1. Is the bot online?
2. What live Polymarket contracts is it tracking right now?
3. What recent trades and activity happened?
4. Why did the strategy pass or refuse a setup?
5. What is the nearest setup to the next trade?

## Current UI layout

The current dashboard structure includes these major sections:

### 1. Top KPI strip

Shows at-a-glance status such as:

- Bot ONLINE / OFFLINE
- Current mode
- Paper NAV
- Realized PnL
- Last Fill

### 2. Trade Blotter

Displays real paper trading fills only.

Typical items shown:

- trade time
- symbol
- market / contract
- side
- stake
- shares / price
- balance
- realized PnL

### 3. Activity feed

Shows non-fill runtime events, for example:

- status reports
- execution attempts
- decision activity
- early-exit style signals

This makes the dashboard useful even when there are few fills.

### 4. Matched markets

Shows the live Polymarket contracts currently being watched.

Fields include:

- symbol
- contract question
- end time
- liquidity
- 24h volume

### 5. No-trade decisions

Shows why the bot did **not** trade.

Typical fields:

- direction
- estimated win probability
- round quality
- age since last flip
- blockers

### 6. 7-symbol round board

Tracks the 7 configured symbols:

- BTC
- ETH
- SOL
- XRP
- DOGE
- HYPE
- BNB

Displayed round data includes:

- open price
- move bps
- flip count
- last flip time
- flip timeline
- momentum 5 / 15 / 60
- source spread
- source count
- decision state

### 7. First trade watch

Highlights the nearest candidate to becoming eligible.

This section is meant to show:

- current strongest setup
- estimated probability
- ask / lag / flips / quality
- which blockers still remain

## Time zone

The UI is normalized to:

- `GMT+8 / Asia/Shanghai`

Trade time and status timestamps are displayed in that time zone for operator clarity.

## How the dashboard gets data

The dashboard uses two main data paths.

### A. Runtime files written by the bot

Primary files currently used:

- `polymarket_crypto_paperbot_multi_live_status.json`
- `polymarket_crypto_paperbot_multi_live_summary.json`
- `polymarket_crypto_paperbot_multi_live_ledger.jsonl`
- `polymarket_crypto_paperbot_multi_live_signals.jsonl`
- `polymarket_crypto_paperbot_multi_live_rounds.jsonl`
- `polymarket_crypto_paperbot_multi_live_flips.jsonl`

These files drive:

- bot online/offline state
- live status snapshot
- paper NAV and realized pnl
- trade blotter
- blockers / decisions
- rounds and flips

### B. Live WSS data for market/watchlist display

The dashboard also opens real-time market feeds.

#### Polymarket

CLOB market websocket:

- `wss://ws-subscriptions-clob.polymarket.com/ws/market`

Used for:

- live YES / NO book state
- best ask tracking
- matched market display context

#### Binance

Current dashboard watchlist feed:

- `wss://stream.binance.com:9443/ws/<symbol>@bookTicker`

#### Coinbase

Current dashboard watchlist feed:

- `wss://advanced-trade-ws.coinbase.com`

#### OKX

Current dashboard watchlist feed:

- `wss://ws.okx.com:8443/ws/v5/public`

#### Optional auxiliary display feeds present in dashboard code

- Kraken WSS
- Bybit WSS

These auxiliary feeds are display-side helpers, not the core trading source of truth.

## How Polymarket market matching works

The dashboard and bot need fresh short-horizon Polymarket contracts.

High-level flow:

1. discover current live markets
2. resolve current YES / NO token IDs
3. subscribe those token IDs on the Polymarket CLOB websocket
4. track live book state
5. display current matched contracts in the dashboard

## Trading strategy overview

The dashboard is fed by a 15-minute crypto paper-trading strategy.

### Universe

The current tracked symbols are:

- BTC
- ETH
- SOL
- XRP
- DOGE
- HYPE
- BNB

### Strategy idea

The strategy is a short-horizon directional system.

It uses:

- multi-source CEX prices as leading indicators
- live Polymarket pricing / book state
- round-level structure and momentum features
- quality filters before entry

### Core logic blocks in the bot

The bot currently uses these important components:

- `findLiveRoundMarkets`
  - discovers current short-horizon Polymarket contracts

- `LeaderCompositeTracker`
  - combines CEX leader prices
  - tracks source count and source spread
  - tracks short-horizon momentum

- `RoundTracker`
  - records 15-minute round structure
  - open / close / high / low
  - flip count and flip timestamps
  - source-quality stats

- `estimateWinProb`
  - estimates probability from lag / momentum / agreement / round quality

- `evaluateMarket`
  - computes whether a market is eligible

- `maybeOpenTrades`
  - opens paper trades only when thresholds pass

- `maybeSettleTrades`
  - settles paper trades and writes ledger events

## WSS architecture summary

### Current direction

The system is being pushed toward a stronger **WSS-first** architecture.

### CEX side

The current intent is:

- Binance → real-time WSS market feed
- OKX → real-time WSS market feed
- Coinbase → real-time WSS market feed

The goal is to use real-time bid/ask-derived pricing instead of mixing inconsistent last / ask snapshots.

### Polymarket side

- real-time book data is handled through CLOB WSS
- current market / token discovery still requires metadata lookup because token IDs rotate across short-horizon rounds

Important limitation:

- short-horizon Polymarket token IDs are not static
- therefore token discovery cannot be purely websocket-only yet
- current market metadata still has to be refreshed dynamically

## Current operational model

Current VPS services:

- `polymarket-dashboard-5011.service`
  - serves the dashboard website on port `5011`

- `polymarket-paperbot-multi.service`
  - runs the continuous paper-trading bot loop

## Local usage

Typical dashboard entry point:

```bash
node polymarket_dashboard_5011.js
```

Production deployment is usually handled through systemd.

## Design principles

- no fake fills
- no fake online state
- no stale unrelated ledger fallback pretending to be current
- operator-first clarity
- trade-detail-first UI
- strong visibility into blockers / no-trade reasons

## Current purpose of this repo snapshot

This repo copy is meant to preserve the current dashboard implementation and explain:

- what the website contains
- how the live data is connected
- how the trading system thinks and operates
- what the runtime services are

## Intended future direction

Possible future upgrades beyond `dashboard-v1`:

- richer trade drill-down
- click-to-expand trade explanations
- stronger cumulative NAV view
- clearer source-health diagnostics
- better activity classification
- more explicit risk / execution summary
