#!/bin/bash
# Trade Bot - Quick Setup Script for macOS
set -e

echo ""
echo "╔════════════════════════════════════╗"
echo "║      TRADE BOT  —  Setup           ║"
echo "╚════════════════════════════════════╝"
echo ""

# ── Check dependencies ──────────────────────────────────────────────────────
echo "→ Checking dependencies..."

if ! command -v python3 &>/dev/null; then
  echo "✗ Python 3 not found. Install from: https://www.python.org/"
  exit 1
fi
echo "  ✓ Python3: $(python3 --version)"

if ! command -v node &>/dev/null; then
  echo "✗ Node.js not found. Install from: https://nodejs.org/"
  exit 1
fi
echo "  ✓ Node: $(node --version)"

# ── Backend setup ────────────────────────────────────────────────────────────
echo ""
echo "→ Setting up Python backend..."
cd "$(dirname "$0")/backend"

if [ ! -d "venv" ]; then
  python3 -m venv venv
fi
./venv/bin/pip install -q -r requirements.txt
echo "  ✓ Backend dependencies installed"

# ── Ollama check ─────────────────────────────────────────────────────────────
echo ""
echo "→ Checking Ollama..."
if command -v ollama &>/dev/null; then
  echo "  ✓ Ollama found: $(ollama --version 2>/dev/null || echo 'installed')"
  
  # Check if llama3.2 is pulled
  if ollama list 2>/dev/null | grep -q "llama3.2"; then
    echo "  ✓ llama3.2 model ready"
  else
    echo "  ⬇  Pulling llama3.2 (this may take a few minutes)..."
    ollama pull llama3.2
  fi
else
  echo "  ⚠  Ollama not found."
  echo "     Install it: https://ollama.com/download"
  echo "     Then run: ollama pull llama3.2"
  echo "     (Bot will use rule-based fallback until Ollama is running)"
fi

# ── Node deps ────────────────────────────────────────────────────────────────
echo ""
echo "→ Installing Node dependencies..."
cd "$(dirname "$0")"
npm install --silent
echo "  ✓ Node dependencies installed"

# ── Env file ─────────────────────────────────────────────────────────────────
if [ ! -f "backend/.env" ]; then
  echo ""
  echo "→ Creating .env config..."
  cat > backend/.env << 'EOF'
# Coinbase API Keys (leave empty for paper trading mode)
COINBASE_API_KEY=
COINBASE_API_SECRET=

# Paper trading = no real money (SAFE DEFAULT)
PAPER_TRADING=true

# Trade amount per signal (USD)
TRADE_AMOUNT_USD=10.0

# How often to check markets (seconds)
POLL_INTERVAL_SEC=60

# Ollama settings
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.2
EOF
  echo "  ✓ Created backend/.env"
fi

echo ""
echo "╔════════════════════════════════════╗"
echo "║  ✓ Setup complete!                 ║"
echo "║                                    ║"
echo "║  To start: npm start               ║"
echo "║                                    ║"
echo "║  ⚠  Paper trading is ON by default ║"
echo "║  Edit backend/.env to configure    ║"
echo "╚════════════════════════════════════╝"
echo ""
