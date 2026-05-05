"""
Trade Bot – AI Crypto Trading Backend
Coinbase Advanced Trade + Ollama + News + Learning System
"""
import os, json, time, requests, sqlite3, logging, threading, re
from datetime import datetime, timezone, timedelta
from typing import Optional
from pathlib import Path

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError: pass

try:
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend
    HAS_JWT = True
except ImportError:
    HAS_JWT = False

try:
    import feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False

from flask import Flask, jsonify, request
from flask_cors import CORS

# ─── Settings ─────────────────────────────────────────────────────────────────
SETTINGS_FILE = Path(__file__).parent / "settings.json"

DEFAULTS = {
    "paper_trading":       True,
    "paper_balance":       100.0,
    "trading_budget_usd":  100.0,
    "trade_amount_pct":    0.05,
    "min_confidence":      0.58,
    "poll_interval_sec":   30,
    "ollama_host":         "http://localhost:11434",
    "ollama_model":        "qwen2.5:14b",
    "pairs": [
        "BTC-USD","ETH-USD","SOL-USD","XRP-USD","DOGE-USD",
        "ADA-USD","AVAX-USD","LINK-USD","LTC-USD"
    ],
    "coinbase_api_key":    "",
    "coinbase_api_secret": "",
    "news_enabled":        True,
    "news_interval_sec":   300,
    "learning_enabled":    True,
    "outcome_eval_min":    10,
    "auto_start_bot":      False,
    "open_at_login":       False,
    "take_profit_pct":     2.0,
    "stop_loss_pct":       1.5,
    "shadow_paper_enabled": True,
    "shadow_paper_balance": 500.0,
}

def load_settings() -> dict:
    s = dict(DEFAULTS)
    if SETTINGS_FILE.exists():
        try: s.update(json.loads(SETTINGS_FILE.read_text()))
        except: pass
    for key, env in [
        ("paper_trading",       "PAPER_TRADING"),
        ("ollama_model",        "OLLAMA_MODEL"),
        ("ollama_host",         "OLLAMA_HOST"),
        ("coinbase_api_key",    "COINBASE_API_KEY"),
        ("coinbase_api_secret", "COINBASE_API_SECRET"),
    ]:
        val = os.getenv(env)
        if val:
            s[key] = val.lower() == "true" if key == "paper_trading" else val
    return s

def save_settings(s: dict):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))

cfg = load_settings()

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("tradebot")

COINBASE_BASE = "https://api.coinbase.com"
app = Flask(__name__)
CORS(app)

# ─── Database ─────────────────────────────────────────────────────────────────
DB_PATH = str(Path(__file__).parent / "tradebot.db")

def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, pair TEXT, side TEXT, price REAL, amount REAL,
        usd_value REAL, ai_reasoning TEXT, paper INTEGER DEFAULT 1, pnl REAL DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, pair TEXT, signal TEXT, confidence REAL,
        reasoning TEXT, price REAL, indicators TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS paper_portfolio (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usd_balance REAL DEFAULT 100.0,
        holdings TEXT DEFAULT '{}'
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS outcomes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER, pair TEXT, signal TEXT,
        entry_price REAL, check_price REAL, pnl_pct REAL,
        outcome TEXT, indicators TEXT, evaluated_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS news (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, title TEXT, source TEXT, url TEXT, sentiment TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pair TEXT UNIQUE,
        entry_price REAL,
        amount REAL,
        usd_invested REAL,
        opened_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS shadow_positions (
        pair TEXT PRIMARY KEY,
        entry_price REAL,
        amount REAL,
        usd_invested REAL,
        opened_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS shadow_portfolio (
        id INTEGER PRIMARY KEY,
        balance REAL DEFAULT 500.0,
        total_trades INTEGER DEFAULT 0,
        total_pnl REAL DEFAULT 0.0
    )""")
    conn.commit()
    row = conn.execute("SELECT COUNT(*) FROM paper_portfolio").fetchone()
    if row[0] == 0:
        conn.execute("INSERT INTO paper_portfolio (usd_balance, holdings) VALUES (?,?)",
                     (cfg["paper_balance"], "{}"))
        conn.commit()
    row = conn.execute("SELECT COUNT(*) FROM shadow_portfolio").fetchone()
    if row[0] == 0:
        conn.execute("INSERT INTO shadow_portfolio (balance, total_trades, total_pnl) VALUES (?,0,0.0)",
                     (cfg.get("shadow_paper_balance", 500.0),))
        conn.commit()
    conn.close()

def db():
    # isolation_level=None = autocommit: each statement commits immediately,
    # preventing threads from holding implicit write transactions open concurrently.
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn

# ─── Coinbase Auth ─────────────────────────────────────────────────────────────
def cb_headers(method: str, path: str) -> dict:
    key    = cfg["coinbase_api_key"]
    secret = cfg["coinbase_api_secret"]
    if not key or not HAS_JWT:
        return {"Content-Type": "application/json"}
    pem = secret.replace("\\n", "\n")
    try:
        private_key = serialization.load_pem_private_key(
            pem.encode(), password=None, backend=default_backend()
        )
        payload = {
            "sub": key, "iss": "cdp",
            "nbf": int(time.time()), "exp": int(time.time()) + 120,
            "uri": f"{method.upper()} api.coinbase.com{path}",
        }
        token = jwt.encode(
            payload, private_key, algorithm="ES256",
            headers={"kid": key, "nonce": str(int(time.time() * 1000))}
        )
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    except Exception as e:
        log.error(f"JWT error: {e}")
        return {"Content-Type": "application/json"}

# ─── Market Data ──────────────────────────────────────────────────────────────
def get_price(pair: str) -> Optional[float]:
    try:
        r = requests.get(f"{COINBASE_BASE}/v2/prices/{pair}/spot", timeout=5)
        r.raise_for_status()
        return float(r.json()["data"]["amount"])
    except Exception as e:
        log.error(f"Price failed {pair}: {e}"); return None

def get_candles(pair: str, granularity: str = "ONE_HOUR", limit: int = 48) -> list:
    try:
        path = f"/api/v3/brokerage/products/{pair}/candles"
        end = int(time.time()); start = end - (limit * 3600)
        r = requests.get(
            COINBASE_BASE + path,
            params={"start": start, "end": end, "granularity": granularity},
            headers=cb_headers("GET", path), timeout=10
        )
        r.raise_for_status()
        return sorted(r.json().get("candles", []), key=lambda x: x["start"])
    except Exception as e:
        log.error(f"Candles failed {pair}: {e}"); return []

def get_balance() -> dict:
    if not cfg["coinbase_api_key"]:
        return {"USD": cfg.get("paper_balance", 100.0)}
    try:
        path = "/v2/accounts"
        r = requests.get(COINBASE_BASE + path, headers=cb_headers("GET", path), timeout=10)
        r.raise_for_status()
        balances = {}
        for acct in r.json().get("data", []):
            bal = float(acct["balance"]["amount"])
            if bal > 0:
                balances[acct["balance"]["currency"]] = bal
        return balances
    except Exception as e:
        log.error(f"Balance failed: {e}"); return {}

def get_live_coin_balance(coin: str) -> float:
    """Get actual balance of a coin from Coinbase."""
    balances = get_balance()
    val = balances.get(coin, 0.0)
    if val > 0:
        return float(val)
    # Fallback: paginate through all accounts
    try:
        path = "/v2/accounts"
        r = requests.get(COINBASE_BASE + path, headers=cb_headers("GET", path),
                         params={"limit": 100}, timeout=10)
        r.raise_for_status()
        for acct in r.json().get("data", []):
            curr = acct.get("balance", {}).get("currency") or acct.get("currency", "")
            if curr == coin:
                return float(acct["balance"]["amount"])
    except Exception as e:
        log.error(f"Coin balance fallback failed {coin}: {e}")
    return 0.0

# ─── Technical Indicators ──────────────────────────────────────────────────────
def compute_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1: return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0: return 100.0
    return 100 - (100 / (1 + ag / al))

def compute_ema(closes: list, period: int) -> float:
    if len(closes) < period: return closes[-1] if closes else 0
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]: ema = p * k + ema * (1 - k)
    return ema

def compute_macd(closes: list) -> tuple:
    e12 = compute_ema(closes, 12); e26 = compute_ema(closes, 26)
    macd = e12 - e26
    return macd, macd * 0.8

def build_market_summary(pair: str) -> dict:
    candles = get_candles(pair)
    price   = get_price(pair)
    if not candles or not price:
        return {"pair": pair, "price": price, "error": "No data"}
    closes = [float(c["close"]) for c in candles]
    highs  = [float(c["high"])  for c in candles]
    lows   = [float(c["low"])   for c in candles]
    vols   = [float(c["volume"]) for c in candles]
    rsi         = compute_rsi(closes)
    ema20       = compute_ema(closes, 20)
    ema50       = compute_ema(closes, min(50, len(closes)))
    macd, msig  = compute_macd(closes)
    avg_vol     = sum(vols[-24:]) / 24 if vols else 0
    pct_change  = ((closes[-1] - closes[-24]) / closes[-24] * 100) if len(closes) >= 24 else 0
    return {
        "pair": pair, "price": price,
        "rsi": round(rsi, 2), "ema20": round(ema20, 2), "ema50": round(ema50, 2),
        "macd": round(macd, 2), "macd_signal": round(msig, 2),
        "24h_high": max(highs[-24:]) if highs else price,
        "24h_low":  min(lows[-24:])  if lows  else price,
        "24h_change_pct": round(pct_change, 2),
        "volume_vs_avg":  round(vols[-1] / avg_vol, 2) if avg_vol else 1.0,
        "candles_count":  len(candles),
        "closes": closes[-24:],
    }

# ─── News ─────────────────────────────────────────────────────────────────────
news_cache = []
news_lock  = threading.Lock()

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://bitcoinmagazine.com/.rss/full/",
]

BULLISH_WORDS = {"surge","rally","bullish","gains","rise","ath","adopt","etf","institutional",
                 "record","approval","launch","partnership","upgrade","halving","inflows","buy"}
BEARISH_WORDS = {"crash","hack","ban","bearish","drop","lawsuit","fud","scam","fraud",
                 "breach","vulnerability","warning","fear","selloff","plunge","seized","arrested"}

def sentiment(text: str) -> str:
    words = set(re.sub(r'[^a-z\s]', '', text.lower()).split())
    bull = len(words & BULLISH_WORDS); bear = len(words & BEARISH_WORDS)
    return "bullish" if bull > bear else "bearish" if bear > bull else "neutral"

def fetch_news():
    if not HAS_FEEDPARSER or not cfg.get("news_enabled", True): return []
    items = []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:6]:
                title = e.get("title", "")
                items.append({
                    "title":     title,
                    "source":    feed.feed.get("title", "News"),
                    "url":       e.get("link", ""),
                    "timestamp": e.get("published", datetime.now(timezone.utc).isoformat()),
                    "sentiment": sentiment(title),
                })
        except Exception as e:
            log.warning(f"Feed error {url}: {e}")
    return items[:18]

def news_loop():
    while True:
        items = fetch_news()
        if items:
            with news_lock:
                news_cache.clear(); news_cache.extend(items)
            try:
                conn = db()
                conn.execute("DELETE FROM news")
                for item in items:
                    conn.execute(
                        "INSERT INTO news (timestamp,title,source,url,sentiment) VALUES (?,?,?,?,?)",
                        (item["timestamp"], item["title"], item["source"], item["url"], item["sentiment"])
                    )
                conn.commit(); conn.close()
            except Exception: pass
        time.sleep(cfg.get("news_interval_sec", 300))

def get_news_context() -> str:
    with news_lock: items = list(news_cache)
    if not items: return ""
    lines = "\n".join(f"- [{item['sentiment'].upper()}] {item['title']}" for item in items[:6])
    return f"\n\nRecent crypto news:\n{lines}"

# ─── Learning System ──────────────────────────────────────────────────────────
def get_learning_context() -> str:
    if not cfg.get("learning_enabled", True): return ""
    conn = db()
    rows = conn.execute(
        "SELECT signal, outcome, pnl_pct, pair, indicators FROM outcomes ORDER BY id DESC LIMIT 120"
    ).fetchall()
    conn.close()
    if len(rows) < 3: return ""

    total = len(rows)
    wins  = sum(1 for r in rows if r[1] == "WIN")
    wr    = wins / total * 100

    # Per-pair win rates (only pairs with ≥3 evaluations)
    pair_stats: dict = {}
    for sig, outcome, pnl, pair, ind_json in rows:
        pair_stats.setdefault(pair, {"w": 0, "t": 0, "pnl": []})
        pair_stats[pair]["t"] += 1
        pair_stats[pair]["pnl"].append(pnl or 0)
        if outcome == "WIN": pair_stats[pair]["w"] += 1

    pair_lines = []
    for pair, v in sorted(pair_stats.items(), key=lambda x: x[1]["w"]/max(x[1]["t"],1), reverse=True):
        if v["t"] >= 3:
            pwr = round(v["w"] / v["t"] * 100)
            avg_pnl = round(sum(v["pnl"]) / len(v["pnl"]), 2)
            pair_lines.append(f"{pair} {pwr}% ({avg_pnl:+.2f}% avg)")

    # RSI pattern analysis: which RSI buckets led to wins
    rsi_buckets: dict = {}
    for sig, outcome, pnl, pair, ind_json in rows:
        if sig != "BUY" or not ind_json: continue
        try:
            ind = json.loads(ind_json)
            rsi = ind.get("rsi", 0)
            bucket = f"RSI {int(rsi//10)*10}-{int(rsi//10)*10+10}"
            rsi_buckets.setdefault(bucket, {"w": 0, "t": 0})
            rsi_buckets[bucket]["t"] += 1
            if outcome == "WIN": rsi_buckets[bucket]["w"] += 1
        except Exception: pass

    rsi_insights = []
    for bucket, v in sorted(rsi_buckets.items()):
        if v["t"] >= 3:
            pwr = round(v["w"] / v["t"] * 100)
            rsi_insights.append(f"{bucket}→{pwr}%wins")

    ctx = f"\n\nLearning ({total} evals, {wr:.0f}% overall):"
    if pair_lines:
        ctx += f"\nPer-pair: {' | '.join(pair_lines[:6])}"
    if rsi_insights:
        ctx += f"\nRSI accuracy on BUY: {' | '.join(rsi_insights)}"
    ctx += "\nFavor pairs/conditions with high win rates. Avoid repeating losing patterns."
    return ctx

def log_trade_outcome(pair: str, signal: str, entry_price: float,
                      exit_price: float, pnl_pct: float, indicators_json: str):
    """Record a realized trade outcome for learning (called on TP/SL close)."""
    outcome = "WIN" if pnl_pct > 0 else "LOSS"
    conn = db()
    conn.execute(
        """INSERT INTO outcomes
           (signal_id,pair,signal,entry_price,check_price,pnl_pct,outcome,indicators,evaluated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (None, pair, signal, entry_price, exit_price, round(pnl_pct, 3),
         outcome, indicators_json, datetime.now(timezone.utc).isoformat())
    )
    conn.commit(); conn.close()

def evaluate_outcomes():
    if not cfg.get("learning_enabled", True): return
    delay = cfg.get("outcome_eval_min", 20) * 60
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=delay)).isoformat()
    conn = db()
    pending = conn.execute(
        """SELECT id, pair, signal, price, indicators FROM signals
           WHERE timestamp < ? AND id NOT IN (SELECT signal_id FROM outcomes WHERE signal_id IS NOT NULL)
           ORDER BY id DESC LIMIT 30""",
        (cutoff,)
    ).fetchall()
    for row in pending:
        sig_id, pair, signal, entry_price, ind_json = row
        if not entry_price: continue
        current = get_price(pair)
        if not current: continue
        pnl = ((current - entry_price) / entry_price * 100)
        if signal == "SELL": pnl = -pnl
        outcome = "WIN" if pnl > 0.5 else "LOSS" if pnl < -0.5 else "NEUTRAL"
        conn.execute(
            """INSERT INTO outcomes
               (signal_id,pair,signal,entry_price,check_price,pnl_pct,outcome,indicators,evaluated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (sig_id, pair, signal, entry_price, current, round(pnl, 3),
             outcome, ind_json, datetime.now(timezone.utc).isoformat())
        )
    conn.commit(); conn.close()

# ─── Paper Portfolio ──────────────────────────────────────────────────────────
portfolio_lock = threading.Lock()

def get_paper_portfolio() -> dict:
    conn = db()
    row = conn.execute("SELECT usd_balance, holdings FROM paper_portfolio LIMIT 1").fetchone()
    conn.close()
    if not row: return {"usd_balance": cfg["paper_balance"], "holdings": {}}
    return {"usd_balance": row[0], "holdings": json.loads(row[1])}

def update_paper_portfolio(usd: float, holdings: dict):
    conn = db()
    conn.execute("UPDATE paper_portfolio SET usd_balance=?, holdings=?", (usd, json.dumps(holdings)))
    conn.commit(); conn.close()

def get_paper_value() -> float:
    pf = get_paper_portfolio()
    total = pf["usd_balance"]
    for coin, amt in pf["holdings"].items():
        if amt > 0:
            p = get_price(f"{coin}-USD")
            if p: total += amt * p
    return total

def paper_buy(pair: str, usd_amount: float, price: float) -> dict:
    with portfolio_lock:
        pf = get_paper_portfolio()
        usd_amount = min(usd_amount, pf["usd_balance"])
        if usd_amount < 0.5: return {"error": "Insufficient USD"}
        coin = pair.replace("-USD", "")
        amt  = usd_amount / price
        pf["usd_balance"] -= usd_amount
        pf["holdings"][coin] = pf["holdings"].get(coin, 0) + amt
        update_paper_portfolio(pf["usd_balance"], pf["holdings"])
        return {"paper": True, "side": "BUY", "price": price, "usd": usd_amount, "amount": amt}

def paper_sell(pair: str, price: float) -> dict:
    with portfolio_lock:
        pf = get_paper_portfolio()
        coin = pair.replace("-USD", "")
        amt  = pf["holdings"].get(coin, 0)
        if amt <= 0: return {"error": "No holdings"}
        usd = amt * price
        pf["usd_balance"] += usd
        pf["holdings"][coin] = 0
        update_paper_portfolio(pf["usd_balance"], pf["holdings"])
        return {"paper": True, "side": "SELL", "price": price, "usd": usd, "amount": amt}

# ─── Position Tracking ────────────────────────────────────────────────────────
def get_position(pair: str) -> Optional[dict]:
    conn = db()
    row = conn.execute(
        "SELECT pair, entry_price, amount, usd_invested, opened_at FROM positions WHERE pair=?", (pair,)
    ).fetchone()
    conn.close()
    if not row: return None
    return dict(zip(["pair","entry_price","amount","usd_invested","opened_at"], row))

def open_position(pair: str, price: float, amount: float, usd_invested: float):
    conn = db()
    conn.execute(
        """INSERT INTO positions (pair, entry_price, amount, usd_invested, opened_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(pair) DO UPDATE SET
             entry_price=(entry_price*amount + excluded.entry_price*excluded.amount)/(amount+excluded.amount),
             amount=amount+excluded.amount,
             usd_invested=usd_invested+excluded.usd_invested""",
        (pair, price, amount, usd_invested, datetime.now(timezone.utc).isoformat())
    )
    conn.commit(); conn.close()

def close_position(pair: str):
    conn = db()
    conn.execute("DELETE FROM positions WHERE pair=?", (pair,))
    conn.commit(); conn.close()

def get_total_invested() -> float:
    """Sum of usd_invested across all currently open positions."""
    conn = db()
    row = conn.execute("SELECT COALESCE(SUM(usd_invested), 0) FROM positions").fetchone()
    conn.close()
    return float(row[0])

# ─── Shadow Paper (learning engine) ───────────────────────────────────────────
def get_shadow_state() -> dict:
    conn = db()
    row = conn.execute("SELECT balance, total_trades, total_pnl FROM shadow_portfolio LIMIT 1").fetchone()
    conn.close()
    if not row: return {"balance": 500.0, "total_trades": 0, "total_pnl": 0.0}
    return {"balance": row[0], "total_trades": row[1], "total_pnl": row[2]}

def get_shadow_position(pair: str) -> Optional[dict]:
    conn = db()
    row = conn.execute(
        "SELECT pair, entry_price, amount, usd_invested FROM shadow_positions WHERE pair=?", (pair,)
    ).fetchone()
    conn.close()
    if not row: return None
    return dict(zip(["pair","entry_price","amount","usd_invested"], row))

def shadow_trade(pair: str, signal: str, price: float, ind_json: str):
    """Execute a shadow paper trade. Outcomes feed the shared learning system."""
    if not cfg.get("shadow_paper_enabled", True): return
    usd_per_trade = max(5.0, cfg.get("shadow_paper_balance", 500.0) * 0.04)

    if signal == "BUY":
        if get_shadow_position(pair): return  # already in this pair
        state = get_shadow_state()
        spend = min(usd_per_trade, state["balance"])
        if spend < 1.0: return
        amount = spend / price
        conn = db()
        conn.execute(
            "INSERT OR REPLACE INTO shadow_positions (pair,entry_price,amount,usd_invested,opened_at) VALUES (?,?,?,?,?)",
            (pair, price, amount, spend, datetime.now(timezone.utc).isoformat())
        )
        conn.execute("UPDATE shadow_portfolio SET balance = balance - ?", (spend,))
        conn.commit(); conn.close()

    elif signal == "SELL":
        pos = get_shadow_position(pair)
        if not pos: return
        revenue = pos["amount"] * price
        pnl_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
        pnl_usd = revenue - pos["usd_invested"]
        conn = db()
        conn.execute("DELETE FROM shadow_positions WHERE pair=?", (pair,))
        conn.execute(
            "UPDATE shadow_portfolio SET balance=balance+?, total_trades=total_trades+1, total_pnl=total_pnl+?",
            (revenue, pnl_usd)
        )
        conn.commit(); conn.close()
        # Log to shared outcomes table so real AI learns from this
        outcome = "WIN" if pnl_pct > 0 else "LOSS"
        log_trade_outcome(pair, "BUY", pos["entry_price"], price, pnl_pct,
                          ind_json if ind_json and ind_json != "{}" else json.dumps({"source": "shadow"}))
        log.info(f"Shadow {pair}: SELL {pnl_pct:+.2f}% — {outcome}")

def check_shadow_tp_sl(pair: str) -> bool:
    """Return True if shadow position should be closed."""
    pos = get_shadow_position(pair)
    if not pos: return False
    current = get_price(pair)
    if not current: return False
    pct = (current - pos["entry_price"]) / pos["entry_price"] * 100
    return pct >= cfg.get("take_profit_pct", 1.0) or pct <= -cfg.get("stop_loss_pct", 1.5)

def check_tp_sl(pair: str) -> Optional[str]:
    """Return 'SELL' if take-profit or stop-loss triggered, else None."""
    pos = get_position(pair)
    if not pos or not pos["entry_price"]: return None
    current = get_price(pair)
    if not current: return None
    pct = (current - pos["entry_price"]) / pos["entry_price"] * 100
    tp = cfg.get("take_profit_pct", 3.0)
    sl = cfg.get("stop_loss_pct",  2.0)
    if pct >= tp:
        log.info(f"{pair}: TAKE PROFIT triggered +{pct:.2f}% (threshold +{tp}%)")
        return "SELL"
    if pct <= -sl:
        log.info(f"{pair}: STOP LOSS triggered {pct:.2f}% (threshold -{sl}%)")
        return "SELL"
    return None

def get_position_context(pair: str) -> str:
    pos = get_position(pair)
    if not pos: return ""
    current = get_price(pair)
    if not current: return ""
    pct = (current - pos["entry_price"]) / pos["entry_price"] * 100
    direction = "up" if pct >= 0 else "down"
    return (f"\n\nOpen position: bought {pair} at ${pos['entry_price']:.4f}, "
            f"currently {direction} {abs(pct):.2f}% (${current:.4f}). "
            f"Take profit at +{cfg.get('take_profit_pct',3.0)}%, stop loss at -{cfg.get('stop_loss_pct',2.0)}%.")

def rebuild_positions_from_history():
    """Reconstruct open positions from trade history on startup."""
    conn = db()
    trades = conn.execute(
        "SELECT pair, side, price, amount, usd_value, timestamp FROM trades ORDER BY timestamp ASC"
    ).fetchall()
    conn.close()

    positions = {}
    for pair, side, price, amount, usd_value, ts in trades:
        if side == "BUY":
            if pair not in positions:
                positions[pair] = {"entry_price": price, "amount": amount, "usd_invested": usd_value or 0}
            else:
                existing = positions[pair]
                total_amt = existing["amount"] + amount
                existing["entry_price"] = (existing["entry_price"] * existing["amount"] + price * amount) / total_amt
                existing["amount"] = total_amt
                existing["usd_invested"] += usd_value or 0
        elif side == "SELL":
            positions.pop(pair, None)

    conn = db()
    for pair, pos in positions.items():
        if pos["amount"] > 0:
            # Only insert if not already tracked
            existing = conn.execute("SELECT id FROM positions WHERE pair=?", (pair,)).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO positions (pair, entry_price, amount, usd_invested, opened_at) VALUES (?,?,?,?,?)",
                    (pair, pos["entry_price"], pos["amount"], pos["usd_invested"],
                     datetime.now(timezone.utc).isoformat())
                )
                log.info(f"Rebuilt position: {pair} @ ${pos['entry_price']:.4f} x {pos['amount']:.6f}")
    conn.commit(); conn.close()

def place_order(pair: str, side: str, usd_amount: float) -> dict:
    price = get_price(pair)
    if not price: return {"error": "No price"}
    if cfg["paper_trading"]:
        return paper_buy(pair, usd_amount, price) if side == "BUY" else paper_sell(pair, price)

    path = "/api/v3/brokerage/orders"

    if side == "BUY":
        if usd_amount < 1.0:
            return {"error": "Below minimum order size ($1)"}
        # quote_size = how many USD to spend
        order_config = {"market_market_ioc": {"quote_size": str(round(usd_amount, 2))}}
    else:
        # SELL: need base_size = actual crypto amount held on Coinbase
        coin = pair.replace("-USD", "")
        base_amt = get_live_coin_balance(coin)
        if base_amt <= 0:
            return {"error": f"No {coin} balance to sell"}
        # Sell full position, rounded to 8 decimal places
        order_config = {"market_market_ioc": {"base_size": str(round(base_amt, 8))}}

    body = json.dumps({
        "client_order_id": f"tb_{int(time.time())}",
        "product_id": pair,
        "side": side.upper(),
        "order_configuration": order_config,
    })
    try:
        r = requests.post(COINBASE_BASE + path, headers=cb_headers("POST", path), data=body, timeout=10)
        r.raise_for_status()
        resp = r.json()
        log.info(f"LIVE ORDER {side} {pair} — response: {r.text[:300]}")
        # Coinbase returns {"success": false, "error_response": {...}} on failure
        if not resp.get("success", True):
            err = resp.get("error_response", resp.get("failure_reason", "Order rejected by Coinbase"))
            log.error(f"Order rejected {side} {pair}: {err}")
            return {"error": str(err)}
        return resp
    except Exception as e:
        log.error(f"Live order failed {side} {pair}: {e}")
        return {"error": str(e)}

# ─── AI Decision Engine ───────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a disciplined, profit-focused crypto trading AI. Your only goal is to make money. Capital preservation comes first.

Rules:
- Signal: BUY, SELL, or HOLD. Confidence 0.0–1.0 reflects conviction in your signal regardless of direction.
- For BUY/SELL: confidence = how strongly the data supports the trade (0.6+ to act).
- For HOLD: confidence = how clearly the data says "do nothing" (0.7 = clearly mixed, 0.9 = strong reason to wait).
- RSI < 35 = oversold (BUY lean). RSI > 68 = overbought (SELL lean).
- EMA20 > EMA50 = uptrend. MACD crossing above signal = bullish momentum.
- Volume spike (>1.5x avg) confirms moves. Low volume = skepticism.
- Bullish news: raise BUY confidence. Hacks/bans/FUD: lower confidence or SELL.
- If you hold a position: issue SELL when momentum reverses, not just at TP/SL.
- If your past win rate on a pair is low: be more selective (raise confidence bar).
- If your past win rate on a pair is high: be more aggressive on clear setups.
- HOLD is the right answer when signals conflict. Never force a trade.
- Realize profits. A held gain is not a gain. Incomplete SELL = lost opportunity.

Respond ONLY in JSON: {"signal":"BUY|SELL|HOLD","confidence":0.75,"reasoning":"2 sentences max"}"""

def ai_decision(market_data: dict) -> dict:
    prompt = f"""Market snapshot for {market_data['pair']}:

Price:       ${market_data.get('price', 'N/A')}
RSI (14):    {market_data.get('rsi', 'N/A')}
EMA20/50:    {market_data.get('ema20', 'N/A')} / {market_data.get('ema50', 'N/A')}
MACD:        {market_data.get('macd', 'N/A')} (signal {market_data.get('macd_signal', 'N/A')})
24h Change:  {market_data.get('24h_change_pct', 'N/A')}%
24h Hi/Lo:   ${market_data.get('24h_high', 'N/A')} / ${market_data.get('24h_low', 'N/A')}
Volume Ratio:{market_data.get('volume_vs_avg', 'N/A')}x
{get_news_context()}{get_learning_context()}{get_position_context(market_data['pair'])}

Give your JSON signal."""
    try:
        r = requests.post(
            f"{cfg['ollama_host']}/api/generate",
            json={"model": cfg["ollama_model"], "prompt": prompt,
                  "system": SYSTEM_PROMPT, "stream": False},
            timeout=45
        )
        r.raise_for_status()
        raw = r.json().get("response", "{}")
        s = raw.find("{"); e = raw.rfind("}") + 1
        if s >= 0 and e > s:
            result = json.loads(raw[s:e])
            return {
                "signal":     result.get("signal", "HOLD").upper(),
                "confidence": float(result.get("confidence", 0.5)),
                "reasoning":  result.get("reasoning", ""),
            }
    except Exception as e:
        log.error(f"Ollama failed: {e}")
    # Rule-based fallback
    rsi  = market_data.get("rsi", 50)
    e20  = market_data.get("ema20", 0); e50 = market_data.get("ema50", 0)
    macd = market_data.get("macd", 0);  ms  = market_data.get("macd_signal", 0)
    bull = sum([rsi < 40, e20 > e50, macd > ms])
    bear = sum([rsi > 65, e20 < e50, macd < ms])
    if bull >= 2: return {"signal": "BUY",  "confidence": 0.61, "reasoning": "Rule-based: bullish confluence."}
    if bear >= 2: return {"signal": "SELL", "confidence": 0.61, "reasoning": "Rule-based: bearish confluence."}
    return         {"signal": "HOLD", "confidence": 0.70, "reasoning": "Rule-based: mixed signals."}

# ─── Trading Loop ──────────────────────────────────────────────────────────────
bot_running = False
bot_thread  = None
bot_status  = {"running": False, "last_signal": None, "pairs": list(cfg["pairs"])}

def trading_loop():
    global bot_status
    log.info("Trading loop started")
    tick = 0
    while bot_running:
        tick += 1
        if tick % 5 == 0:
            try: evaluate_outcomes()
            except Exception as e: log.error(f"Eval error: {e}")
        for pair in list(bot_status["pairs"]):
            if not bot_running: break
            try:
                # ── Shadow paper TP/SL (always runs, free of live budget) ─────
                if check_shadow_tp_sl(pair):
                    p = get_price(pair)
                    if p: shadow_trade(pair, "SELL", p, None)

                # ── Live TP/SL check before AI ────────────────────────────────
                tp_sl = check_tp_sl(pair)
                if tp_sl == "SELL":
                    price = get_price(pair)
                    pos   = get_position(pair)
                    result = place_order(pair, "SELL", 0)
                    if "error" not in result and price and pos:
                        amt     = pos["amount"]
                        usd_val = amt * price
                        entry   = pos["entry_price"]
                        pnl_pct = (price - entry) / entry * 100
                        reason  = "Take-profit" if pnl_pct > 0 else "Stop-loss"
                        now_ts  = datetime.now(timezone.utc).isoformat()
                        conn = db()
                        conn.execute(
                            "INSERT INTO trades (timestamp,pair,side,price,amount,usd_value,ai_reasoning,paper) VALUES (?,?,?,?,?,?,?,?)",
                            (now_ts, pair, "SELL", price, amt, usd_val,
                             f"Auto: {reason} {pnl_pct:+.2f}%",
                             1 if cfg["paper_trading"] else 0)
                        )
                        # Also write a signal record so the dashboard feed shows it
                        conn.execute(
                            "INSERT INTO signals (timestamp,pair,signal,confidence,reasoning,price,indicators) VALUES (?,?,?,?,?,?,?)",
                            (now_ts, pair, "SELL", 0.99,
                             f"{reason} triggered: {pnl_pct:+.2f}% vs entry ${entry:.4f}",
                             price, None)
                        )
                        conn.commit(); conn.close()
                        ind_snap = json.dumps({"entry_price": entry, "exit_price": price,
                                               "pnl_pct": round(pnl_pct, 3), "trigger": reason})
                        log_trade_outcome(pair, "BUY", entry, price, pnl_pct, ind_snap)
                        close_position(pair)
                        bot_status["last_signal"] = {
                            "signal": "SELL", "pair": pair, "price": price,
                            "confidence": 0.99,
                            "reasoning": f"{reason} triggered: {pnl_pct:+.2f}%",
                            "time": now_ts,
                        }
                        log.info(f"{pair}: {reason} SELL @ ${price} ({pnl_pct:+.2f}%)")
                    continue

                market = build_market_summary(pair)
                if market.get("error"): continue
                decision = ai_decision(market)
                ind_json = json.dumps({
                    "rsi": market.get("rsi"), "ema20": market.get("ema20"),
                    "ema50": market.get("ema50"), "macd": market.get("macd"),
                })
                log.info(f"{pair}: {decision['signal']} conf={decision['confidence']:.2f}")

                # ── Skip BUY if already holding ───────────────────────────────
                if decision["signal"] == "BUY" and get_position(pair):
                    log.info(f"{pair}: already holding, skipping BUY")
                    bot_status["last_signal"] = {
                        **decision, "signal": "HOLD", "pair": pair,
                        "price": market.get("price"),
                        "time": datetime.now().isoformat()
                    }
                    continue

                # ── Budget cap: don't invest beyond trading_budget_usd ────────
                budget = cfg.get("trading_budget_usd", 100.0)
                if decision["signal"] == "BUY":
                    invested  = get_total_invested()
                    remaining = budget - invested
                    if remaining < 1.0:
                        log.info(f"Budget cap: ${invested:.2f}/${budget:.2f} invested, skipping {pair} BUY")
                        continue

                # ── Skip AI SELL if we have no open position ─────────────────
                if decision["signal"] == "SELL" and not get_position(pair):
                    continue

                # Write signal immediately then close — avoids holding a write
                # lock open across place_order / shadow_trade calls below
                now_ts = datetime.now(timezone.utc).isoformat()
                conn = db()
                try:
                    conn.execute(
                        "INSERT INTO signals (timestamp,pair,signal,confidence,reasoning,price,indicators) VALUES (?,?,?,?,?,?,?)",
                        (now_ts, pair, decision["signal"], decision["confidence"],
                         decision["reasoning"], market.get("price"), ind_json)
                    )
                    conn.commit()
                finally:
                    conn.close()

                min_conf = cfg.get("min_confidence", 0.58)
                if decision["signal"] in ("BUY", "SELL") and decision["confidence"] >= min_conf:
                    if cfg["paper_trading"]:
                        pf        = get_paper_portfolio()
                        usd_avail = pf.get("usd_balance", 0)
                    else:
                        base_avail = budget
                        if decision["signal"] == "BUY":
                            base_avail = min(budget, budget - get_total_invested())
                        usd_avail = base_avail
                    usd_amt = min(
                        max(1.0, usd_avail * cfg.get("trade_amount_pct", 0.05)),
                        usd_avail
                    )
                    if usd_amt >= 1.0:
                        result = place_order(pair, decision["signal"], usd_amt)
                        if "error" not in result:
                            price = market.get("price", 0)
                            amt   = usd_amt / price if price else 0
                            conn2 = db()
                            try:
                                conn2.execute(
                                    "INSERT INTO trades (timestamp,pair,side,price,amount,usd_value,ai_reasoning,paper) VALUES (?,?,?,?,?,?,?,?)",
                                    (datetime.now(timezone.utc).isoformat(), pair,
                                     decision["signal"], price, amt, usd_amt,
                                     decision["reasoning"], 1 if cfg["paper_trading"] else 0)
                                )
                                conn2.commit()
                            finally:
                                conn2.close()
                            if decision["signal"] == "BUY":
                                open_position(pair, price, amt, usd_amt)
                            elif decision["signal"] == "SELL":
                                pos = get_position(pair)
                                if pos:
                                    pnl_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
                                    log_trade_outcome(pair, "BUY", pos["entry_price"], price, pnl_pct, ind_json)
                                close_position(pair)

                # ── Shadow paper trades ALL signals — no confidence gate ────
                if decision["signal"] in ("BUY", "SELL"):
                    try: shadow_trade(pair, decision["signal"], market.get("price", 0), ind_json)
                    except Exception as se: log.error(f"Shadow trade error {pair}: {se}")

                bot_status["last_signal"] = {
                    **decision, "pair": pair,
                    "price": market.get("price"),
                    "time": datetime.now().isoformat()
                }
            except Exception as e:
                log.error(f"Loop error {pair}: {e}")
        time.sleep(cfg.get("poll_interval_sec", 60))

# ─── Flask Routes ──────────────────────────────────────────────────────────────
@app.route("/api/status")
def api_status():
    return jsonify({
        "running":       bot_running,
        "paper_trading": cfg["paper_trading"],
        "model":         cfg["ollama_model"],
        "pairs":         bot_status["pairs"],
        "last_signal":   bot_status.get("last_signal"),
        "api_connected": bool(cfg["coinbase_api_key"]),
    })

@app.route("/api/portfolio")
def api_portfolio():
    if cfg["paper_trading"]:
        pf    = get_paper_portfolio()
        total = get_paper_value()
        start = cfg.get("paper_balance", 100.0)
        pnl   = total - start
        return jsonify({
            "paper":             True,
            "usd_balance":       round(pf["usd_balance"], 2),
            "holdings":          pf["holdings"],
            "total_value":       round(total, 2),
            "starting_balance":  start,
            "pnl":               round(pnl, 2),
            "pnl_pct":           round(pnl / start * 100, 2) if start else 0,
        })
    balances = get_balance()
    prices   = {}
    total    = balances.get("USD", 0)
    for c in balances:
        if c != "USD":
            p = get_price(f"{c}-USD")
            if p: prices[c] = p; total += balances[c] * p
    return jsonify({"paper": False, "balances": balances, "prices": prices, "total_usd": round(total, 2)})

@app.route("/api/balance")
def api_balance():
    balances = get_balance()
    prices = {}; total = balances.get("USD", 0)
    for c in balances:
        if c != "USD":
            p = get_price(f"{c}-USD")
            if p: prices[c] = p; total += balances[c] * p
    return jsonify({"balances": balances, "prices": prices, "total_usd": round(total, 2)})

@app.route("/api/prices")
def api_prices():
    pairs = request.args.get("pairs", ",".join(cfg["pairs"])).split(",")
    result = {}
    for pair in pairs:
        p = get_price(pair.strip())
        if p: result[pair.strip()] = p
    return jsonify(result)

@app.route("/api/market/<pair>")
def api_market(pair):
    return jsonify(build_market_summary(pair.upper()))

@app.route("/api/trades")
def api_trades():
    limit = int(request.args.get("limit", 50))
    conn  = db()
    rows  = conn.execute("SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    cols  = ["id","timestamp","pair","side","price","amount","usd_value","ai_reasoning","paper","pnl"]
    return jsonify([dict(zip(cols, r)) for r in rows])

@app.route("/api/performance")
def api_performance():
    """Accurate P&L: win rate only from closed round-trips, net P&L = realized + unrealized."""
    conn = db()
    rows = conn.execute("SELECT * FROM trades ORDER BY timestamp ASC").fetchall()
    conn.close()
    cols = ["id","timestamp","pair","side","price","amount","usd_value","ai_reasoning","paper","pnl"]
    trades = [dict(zip(cols, r)) for r in rows]

    # Track open buy batches per pair to match against sells
    open_buys = {}   # pair -> list of {price, amount, usd_value}
    wins = 0; losses = 0
    realized_pnl = 0.0
    per_pair = {}
    enriched = []

    for t in trades:
        pair = t["pair"]
        if pair not in per_pair:
            per_pair[pair] = {"spent": 0, "received": 0, "realized_pnl": 0,
                              "unrealized_pnl": 0, "trades": 0, "wins": 0, "losses": 0}

        if t["side"] == "BUY":
            per_pair[pair]["spent"]  += t["usd_value"] or 0
            per_pair[pair]["trades"] += 1
            open_buys.setdefault(pair, []).append({
                "price": t["price"], "amount": t["amount"] or 0,
                "usd_value": t["usd_value"] or 0
            })
            # P&L for open BUYs is calculated after all trades are processed
            enriched.append({**t, "current_price": None, "pnl_usd": 0.0, "pnl_pct": 0.0, "outcome": "OPEN"})

        elif t["side"] == "SELL":
            sell_revenue = t["usd_value"] or 0
            per_pair[pair]["received"] += sell_revenue
            buy_cost = sum(b["usd_value"] for b in open_buys.get(pair, []))
            if buy_cost > 0:
                trade_pnl = sell_revenue - buy_cost
                trade_pct = trade_pnl / buy_cost * 100
                realized_pnl += trade_pnl
                per_pair[pair]["realized_pnl"] += trade_pnl
                outcome = "WIN" if trade_pnl > 0 else "LOSS"
                if trade_pnl > 0: wins += 1
                else: losses += 1
                per_pair[pair]["wins" if trade_pnl > 0 else "losses"] += 1
                open_buys[pair] = []
            else:
                trade_pnl = 0.0; trade_pct = 0.0; outcome = "CLOSED"
            enriched.append({**t, "current_price": t["price"],
                             "pnl_usd": round(trade_pnl, 4),
                             "pnl_pct": round(trade_pct, 2), "outcome": outcome})

    # Fill in current prices + unrealized P&L for open BUYs
    price_cache = {}
    unrealized_pnl = 0.0
    for i, t in enumerate(enriched):
        if t["outcome"] != "OPEN": continue
        pair = t["pair"]
        if pair not in price_cache:
            price_cache[pair] = get_price(pair)
        current = price_cache[pair]
        if current and t["price"]:
            pnl_usd = (current - t["price"]) * (t["amount"] or 0)
            pnl_pct = (current - t["price"]) / t["price"] * 100
            enriched[i] = {**t, "current_price": current,
                           "pnl_usd": round(pnl_usd, 4), "pnl_pct": round(pnl_pct, 2)}
            unrealized_pnl += pnl_usd
            per_pair[pair]["unrealized_pnl"] = round(
                per_pair[pair].get("unrealized_pnl", 0) + pnl_usd, 4)

    net_pnl = realized_pnl + unrealized_pnl
    total_evaluated = wins + losses

    return jsonify({
        "trades":         list(reversed(enriched)),
        "total_trades":   len(trades),
        "total_spent":    round(sum(t["usd_value"] or 0 for t in trades if t["side"] == "BUY"), 2),
        "total_received": round(sum(t["usd_value"] or 0 for t in trades if t["side"] == "SELL"), 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "realized_pnl":   round(realized_pnl, 2),
        "net_pnl":        round(net_pnl, 2),
        "win_rate":       round(wins / total_evaluated * 100, 1) if total_evaluated else None,
        "wins":           wins,
        "losses":         losses,
        "per_pair":       per_pair,
    })

@app.route("/api/signals")
def api_signals():
    limit = int(request.args.get("limit", 25))
    conn  = db()
    rows  = conn.execute("SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    cols  = ["id","timestamp","pair","signal","confidence","reasoning","price","indicators"]
    return jsonify([dict(zip(cols, r)) for r in rows])

@app.route("/api/news")
def api_news():
    conn = db()
    rows = conn.execute(
        "SELECT timestamp,title,source,url,sentiment FROM news ORDER BY id DESC LIMIT 20"
    ).fetchall()
    conn.close()
    items = [dict(zip(["timestamp","title","source","url","sentiment"], r)) for r in rows]
    if not items:
        with news_lock: items = list(news_cache)[:20]
    return jsonify(items)

@app.route("/api/learning")
def api_learning():
    conn = db()
    rows = conn.execute(
        "SELECT signal, outcome, pnl_pct, pair FROM outcomes ORDER BY id DESC LIMIT 100"
    ).fetchall()
    conn.close()
    if not rows:
        return jsonify({"total": 0, "win_rate": 0, "by_signal": {}, "by_pair": {}})
    total = len(rows)
    wins  = sum(1 for r in rows if r[1] == "WIN")
    by_signal = {}; by_pair = {}
    for sig, outcome, pnl, pair in rows:
        by_signal.setdefault(sig, {"win": 0, "loss": 0, "neutral": 0, "pnls": []})
        by_signal[sig][outcome.lower()] += 1
        by_signal[sig]["pnls"].append(pnl or 0)
        by_pair.setdefault(pair, {"win": 0, "total": 0})
        by_pair[pair]["total"] += 1
        if outcome == "WIN": by_pair[pair]["win"] += 1
    for k in by_signal:
        pnls = by_signal[k].pop("pnls")
        by_signal[k]["avg_pnl"] = round(sum(pnls) / len(pnls), 3) if pnls else 0
    return jsonify({
        "total": total, "wins": wins,
        "win_rate": round(wins / total * 100, 1),
        "by_signal": by_signal,
        "by_pair": {
            k: {**v, "win_rate": round(v["win"] / v["total"] * 100, 1)}
            for k, v in by_pair.items()
        },
    })

@app.route("/api/bot/sell/<pair>", methods=["POST"])
def api_force_sell(pair):
    pair = pair.upper()
    price = get_price(pair)
    pos   = get_position(pair)
    if not pos:
        return jsonify({"error": f"No position for {pair}"})
    result = place_order(pair, "SELL", 0)
    if "error" in result:
        return jsonify(result)
    amt     = pos["amount"]
    usd_val = amt * price
    entry   = pos["entry_price"]
    pnl_pct = (price - entry) / entry * 100
    reason  = "Manual sell" if abs(pnl_pct) < cfg.get("take_profit_pct", 0.5) else ("Take-profit" if pnl_pct > 0 else "Stop-loss")
    now_ts  = datetime.now(timezone.utc).isoformat()
    conn = db()
    conn.execute(
        "INSERT INTO trades (timestamp,pair,side,price,amount,usd_value,ai_reasoning,paper) VALUES (?,?,?,?,?,?,?,?)",
        (now_ts, pair, "SELL", price, amt, usd_val, f"{reason}: {pnl_pct:+.2f}%", 0)
    )
    conn.execute(
        "INSERT INTO signals (timestamp,pair,signal,confidence,reasoning,price,indicators) VALUES (?,?,?,?,?,?,?)",
        (now_ts, pair, "SELL", 0.99, f"{reason}: {pnl_pct:+.2f}% vs entry ${entry:.4f}", price, None)
    )
    conn.commit(); conn.close()
    ind_snap = json.dumps({"entry_price": entry, "exit_price": price, "pnl_pct": round(pnl_pct,3)})
    log_trade_outcome(pair, "BUY", entry, price, pnl_pct, ind_snap)
    close_position(pair)
    # Also close shadow position if one exists, so dashboard stays in sync
    if get_shadow_position(pair):
        shadow_trade(pair, "SELL", price, ind_snap)
    log.info(f"Force SELL {pair} @ ${price} ({pnl_pct:+.2f}%)")
    return jsonify({"status": "sold", "pair": pair, "price": price,
                    "pnl_pct": round(pnl_pct,2), "usd_value": round(usd_val,2)})

@app.route("/api/bot/start", methods=["POST"])
def api_start():
    global bot_running, bot_thread
    if not bot_running:
        bot_running = True; bot_status["running"] = True
        bot_thread = threading.Thread(target=trading_loop, daemon=True)
        bot_thread.start()
    return jsonify({"status": "started"})

@app.route("/api/bot/stop", methods=["POST"])
def api_stop():
    global bot_running
    bot_running = False; bot_status["running"] = False
    return jsonify({"status": "stopped"})

@app.route("/api/bot/analyze", methods=["POST"])
def api_analyze():
    pair     = (request.json or {}).get("pair", "BTC-USD").upper()
    market   = build_market_summary(pair)
    decision = ai_decision(market)
    ind_json = json.dumps({
        "rsi": market.get("rsi"), "ema20": market.get("ema20"),
        "ema50": market.get("ema50"), "macd": market.get("macd"),
    })
    conn = db()
    conn.execute(
        "INSERT INTO signals (timestamp,pair,signal,confidence,reasoning,price,indicators) VALUES (?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), pair,
         decision["signal"], decision["confidence"],
         decision["reasoning"], market.get("price"), ind_json)
    )
    conn.close()
    return jsonify({"market": market, "decision": decision})

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    global cfg, bot_status
    if request.method == "POST":
        data = request.json or {}
        cfg.update(data)
        bot_status["pairs"] = cfg["pairs"]
        save_settings(cfg)
        return jsonify({"status": "saved"})
    safe_cfg = {k: ("***" if k in ("coinbase_api_key","coinbase_api_secret") and v else v)
                for k, v in cfg.items()}
    safe_cfg["coinbase_key_set"]    = bool(cfg.get("coinbase_api_key"))
    safe_cfg["coinbase_secret_set"] = bool(cfg.get("coinbase_api_secret"))
    return jsonify(safe_cfg)

@app.route("/api/shadow")
def api_shadow():
    state = get_shadow_state()
    conn = db()
    positions = conn.execute(
        "SELECT pair, entry_price, amount, usd_invested FROM shadow_positions"
    ).fetchall()
    conn.close()
    pos_list = []
    for pair, entry, amount, invested in positions:
        current = get_price(pair)
        pct = (current - entry) / entry * 100 if current else 0
        pos_list.append({
            "pair": pair, "entry_price": entry, "amount": amount,
            "usd_invested": invested, "current_price": current,
            "pnl_pct": round(pct, 2),
        })
    # Outcomes count from shadow trades (all outcomes feed learning)
    conn = db()
    outcome_rows = conn.execute(
        "SELECT outcome, COUNT(*) FROM outcomes GROUP BY outcome"
    ).fetchall()
    conn.close()
    outcomes = {r[0]: r[1] for r in outcome_rows}
    total_evals = sum(outcomes.values())
    wins = outcomes.get("WIN", 0)
    return jsonify({
        "balance":        round(state["balance"], 2),
        "starting":       cfg.get("shadow_paper_balance", 500.0),
        "total_pnl":      round(state["total_pnl"], 2),
        "total_trades":   state["total_trades"],
        "open_positions": pos_list,
        "total_outcomes": total_evals,
        "win_rate":       round(wins / total_evals * 100, 1) if total_evals else None,
        "take_profit_pct": cfg.get("take_profit_pct", 2.0),
        "stop_loss_pct":   cfg.get("stop_loss_pct", 1.5),
    })

@app.route("/api/positions")
def api_positions():
    conn = db()
    rows = conn.execute(
        "SELECT pair, entry_price, amount, usd_invested, opened_at FROM positions"
    ).fetchall()
    conn.close()
    cols = ["pair","entry_price","amount","usd_invested","opened_at"]
    result = []
    for row in rows:
        pos = dict(zip(cols, row))
        current = get_price(pos["pair"])
        if current:
            pct = (current - pos["entry_price"]) / pos["entry_price"] * 100
            pos["current_price"] = current
            pos["pnl_pct"] = round(pct, 2)
            pos["pnl_usd"] = round((current - pos["entry_price"]) * pos["amount"], 4)
        result.append(pos)
    return jsonify(result)

@app.route("/api/settings/reset-portfolio", methods=["POST"])
def api_reset_portfolio():
    bal = float((request.json or {}).get("balance", cfg.get("paper_balance", 100.0)))
    cfg["paper_balance"] = bal
    save_settings(cfg)
    conn = db()
    conn.execute("UPDATE paper_portfolio SET usd_balance=?, holdings=?", (bal, "{}"))
    conn.commit(); conn.close()
    return jsonify({"status": "reset", "balance": bal})

if __name__ == "__main__":
    init_db()
    rebuild_positions_from_history()
    log.info(f"Trade Bot starting — paper={cfg['paper_trading']} model={cfg['ollama_model']} pairs={len(cfg['pairs'])}")
    if cfg.get("news_enabled", True):
        threading.Thread(target=news_loop, daemon=True).start()
    if cfg.get("auto_start_bot", False):
        bot_running = True; bot_status["running"] = True
        threading.Thread(target=trading_loop, daemon=True).start()
    app.run(host="127.0.0.1", port=7432, debug=False)
