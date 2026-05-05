# Trade Bot 🤖📈

AI-powered crypto trading bot for macOS. Uses **Ollama (local LLM)** for decisions + **Coinbase Advanced Trade API**. Clean terminal-inspired UI.

---

## Features

- **AI-driven signals** via local Ollama (free, private, no API costs)
- **Technical indicators**: RSI, EMA 20/50, MACD computed from live candle data
- **Paper trading mode** by default (no real money at risk)
- **Live Coinbase prices** with 24h sparkline charts  
- **Real-time signal feed** with AI reasoning shown per trade
- **macOS native** — vibrancy effects, traffic light buttons, tray icon

---

## Quick Start

### 1. Install prerequisites

```bash
# Node.js (https://nodejs.org) — for the Electron UI
# Python 3.9+ (https://python.org)
# Ollama (https://ollama.com/download) — free local AI
```

### 2. Install Ollama model

```bash
# Lightweight but capable (2GB)
ollama pull llama3.2

# Alternatives:
# ollama pull mistral       # 4GB, better reasoning
# ollama pull phi3          # 2.3GB, fast
# ollama pull deepseek-r1   # strong at analysis
```

### 3. Run setup

```bash
chmod +x scripts/setup.sh
./scripts/setup.sh
```

### 4. Launch

```bash
npm start
```

---

## Configuration (`backend/.env`)

```bash
# ── Coinbase API Keys ──────────────────────────────────────────────────────
# Get these from: https://portal.cdp.coinbase.com/
# Required permissions: view + trade
COINBASE_API_KEY=your_key_here
COINBASE_API_SECRET=your_secret_here

# ── Safety ────────────────────────────────────────────────────────────────
# Set to false only when you're ready to trade with real money
PAPER_TRADING=true

# Amount in USD per trade signal
TRADE_AMOUNT_USD=10.0

# Market check interval (60s = 1 minute)
POLL_INTERVAL_SEC=60

# ── AI Model ──────────────────────────────────────────────────────────────
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.2   # or mistral, phi3, etc.
```

---

## How the AI Trading Works

```
Every N seconds:
  1. Fetch 48h OHLCV candles from Coinbase
  2. Compute: RSI(14), EMA(20), EMA(50), MACD
  3. Send market summary to local Ollama model
  4. Model returns: {"signal": "BUY|SELL|HOLD", "confidence": 0.0-1.0, "reasoning": "..."}
  5. If signal confidence ≥ 65%, execute trade
  6. Log everything to SQLite
```

**Fallback**: If Ollama is offline, a rule-based system kicks in (RSI + EMA crossover + MACD).

---

## Strategy Notes

The current strategy is a **trend-following + mean-reversion hybrid**:

| Signal | Conditions |
|--------|-----------|
| BUY    | RSI < 35 (oversold) + EMA20 > EMA50 (uptrend) + MACD bullish |
| SELL   | RSI > 70 (overbought) + EMA20 < EMA50 (downtrend) + MACD bearish |
| HOLD   | Mixed signals |

**To improve profitability**, consider:
- Longer time horizon (4h candles instead of 1h)
- Adding volume confirmation
- Setting stop-loss / take-profit levels
- Backtesting on historical data before going live

---

## Getting Coinbase API Keys

1. Go to [portal.cdp.coinbase.com](https://portal.cdp.coinbase.com/)
2. Create a new API key
3. Set permissions: **View** + **Trade**
4. Copy key + secret to `backend/.env`
5. Set `PAPER_TRADING=false` when ready

---

## Architecture

```
Trade Bot/
├── src/
│   ├── main.js          # Electron main process
│   └── preload.js       # Context bridge
├── frontend/
│   └── index.html       # UI (vanilla JS, no framework)
├── backend/
│   ├── bot.py           # Flask API + trading logic + Ollama
│   └── requirements.txt
└── scripts/
    └── setup.sh
```

---

## Disclaimer

Trading involves substantial risk of loss. This is experimental software. Start with paper trading. Never trade money you can't afford to lose. Past performance does not guarantee future results.
