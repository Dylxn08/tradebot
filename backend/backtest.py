#!/usr/bin/env python3
"""
TradeBot Backtesting Engine
Replays the rule-based strategy on historical Coinbase candles.

Usage:
  python backtest.py --pair BTC-USD --days 180
  python backtest.py --pair ETH-USD --days 90 --capital 5000
"""

import argparse
import sys
import time
import json
import requests
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))
from bot import (compute_rsi, compute_ema, compute_macd, compute_adx,
                 compute_bollinger, cb_headers, cfg, COINBASE_BASE, TAKER_FEE)


# ─── Historical data fetch ─────────────────────────────────────────────────────

def fetch_all_candles(pair: str, days: int, granularity: str = "ONE_HOUR") -> list:
    """Paginate backwards to fetch `days` of historical candles."""
    spc = {"ONE_MINUTE": 60, "FIVE_MINUTE": 300, "FIFTEEN_MINUTE": 900,
           "THIRTY_MINUTE": 1800, "ONE_HOUR": 3600, "SIX_HOUR": 21600,
           "ONE_DAY": 86400}.get(granularity, 3600)

    total_secs   = days * 86400
    chunk_secs   = 300 * spc   # 300 candles per request
    end_ts       = int(time.time())
    start_ts     = end_ts - total_secs
    all_candles  = []
    current_end  = end_ts

    while current_end > start_ts:
        current_start = max(start_ts, current_end - chunk_secs)
        path = f"/api/v3/brokerage/products/{pair}/candles"
        try:
            r = requests.get(
                COINBASE_BASE + path,
                params={"start": current_start, "end": current_end, "granularity": granularity},
                headers=cb_headers("GET", path),
                timeout=15,
            )
            r.raise_for_status()
            candles = r.json().get("candles", [])
            if not candles:
                break
            all_candles.extend(candles)
            pct = (1 - (current_start - start_ts) / max(total_secs, 1)) * 100
            print(f"  Fetching… {len(all_candles)} candles ({pct:.0f}% done)", end="\r", flush=True)
        except Exception as e:
            print(f"\n  Warning fetching chunk: {e}")
        current_end = current_start
        time.sleep(0.3)

    # Deduplicate + sort ascending
    seen, unique = set(), []
    for c in all_candles:
        if c["start"] not in seen:
            seen.add(c["start"]); unique.append(c)
    return sorted(unique, key=lambda x: int(x["start"]))


# ─── Strategy replay ───────────────────────────────────────────────────────────

def run_backtest(candles: list, initial_capital: float = 1000.0,
                 trade_pct: float = 0.10, sl_pct: float = 1.5,
                 partial_tp_pct: float = 3.0, trail_pct: float = 2.0) -> dict:
    """
    Replay the ADX-regime strategy on historical candles.

    Entry rules:
      TREND  (ADX > 25): MACD cross-up + EMA20 > EMA50 + RSI < 65 + volume ok
      RANGE  (ADX < 20): RSI < 35 + price at/below lower Bollinger Band
      NEUTRAL: RSI < 38 + MACD cross-up + EMA20 > EMA50

    Exit rules (multi-target trailing stop):
      -SL%        → stop loss
      +partial_tp → sell 50%, move stop to breakeven
      +5% high    → trail stop 2% below the high watermark
    """
    WARMUP = 55
    if len(candles) < WARMUP + 10:
        return {"error": f"Only {len(candles)} candles — need at least {WARMUP + 10}"}

    closes = [float(c["close"])  for c in candles]
    highs  = [float(c["high"])   for c in candles]
    lows   = [float(c["low"])    for c in candles]
    vols   = [float(c["volume"]) for c in candles]
    times  = [int(c["start"])    for c in candles]

    capital      = initial_capital
    equity_curve = [capital]
    pos          = None   # {entry, amount, usd_in, highest, stop, partial}
    trades       = []
    prev_macd    = None

    for i in range(WARMUP, len(candles)):
        c_now  = closes[i]
        h_now  = highs[i]
        sub_c  = closes[:i+1]
        sub_h  = highs[:i+1]
        sub_l  = lows[:i+1]
        sub_v  = vols[:i+1]

        rsi        = compute_rsi(sub_c)
        ema20      = compute_ema(sub_c, 20)
        ema50      = compute_ema(sub_c, min(50, len(sub_c)))
        macd, msig = compute_macd(sub_c)
        adx        = compute_adx(sub_h, sub_l, sub_c)
        _, _, bb_l = compute_bollinger(sub_c)
        avg_vol    = sum(sub_v[-24:]) / 24 if len(sub_v) >= 24 else sub_v[-1]
        vol_ratio  = sub_v[-1] / avg_vol if avg_vol else 1.0
        macd_cross = (prev_macd is not None and prev_macd < msig and macd > msig)

        regime = "TREND" if adx > 25 else ("RANGE" if adx < 20 else "NEUTRAL")

        # ── Exit logic ────────────────────────────────────────────────────────
        if pos:
            if h_now > pos["highest"]:
                pos["highest"] = h_now

            pct_now = (c_now - pos["entry"]) / pos["entry"] * 100

            if not pos["partial"]:
                if pct_now >= partial_tp_pct:
                    # Take 50%, move stop to breakeven
                    sell_amt  = pos["amount"] * 0.5
                    proceeds  = sell_amt * c_now * (1 - TAKER_FEE)
                    capital  += proceeds
                    pos["amount"]  -= sell_amt
                    pos["usd_in"]  *= 0.5
                    pos["partial"]  = True
                    pos["stop"]     = pos["entry"] * 1.001
                    trades.append({
                        "type": "PARTIAL_TP", "entry": pos["entry"], "exit": c_now,
                        "pnl_pct": round(pct_now - 2 * TAKER_FEE * 100, 3),
                        "ts": times[i], "regime": regime,
                    })
                elif c_now <= pos["stop"]:
                    proceeds = pos["amount"] * c_now * (1 - TAKER_FEE)
                    capital += proceeds
                    net_pnl = pct_now - 2 * TAKER_FEE * 100
                    trades.append({
                        "type": "STOP_LOSS", "entry": pos["entry"], "exit": c_now,
                        "pnl_pct": round(net_pnl, 3), "ts": times[i], "regime": regime,
                    })
                    pos = None
            else:
                # Trailing stop phase
                high_pct = (pos["highest"] - pos["entry"]) / pos["entry"] * 100
                if high_pct >= 5.0:
                    trail_stop = pos["highest"] * (1 - trail_pct / 100)
                    pos["stop"] = max(pos["stop"], trail_stop)
                if c_now <= pos["stop"]:
                    proceeds = pos["amount"] * c_now * (1 - TAKER_FEE)
                    capital += proceeds
                    net_pnl = pct_now - 2 * TAKER_FEE * 100
                    trades.append({
                        "type": "TRAIL_STOP", "entry": pos["entry"], "exit": c_now,
                        "pnl_pct": round(net_pnl, 3), "ts": times[i], "regime": regime,
                    })
                    pos = None

        # ── Entry logic (only when flat) ──────────────────────────────────────
        if not pos and capital > 10:
            signal = False
            if regime == "TREND":
                signal = macd_cross and ema20 > ema50 and rsi < 65 and vol_ratio > 0.8
            elif regime == "RANGE":
                signal = rsi < 35 and c_now <= bb_l * 1.015
            else:  # NEUTRAL — require stronger confluence
                signal = rsi < 38 and macd_cross and ema20 > ema50

            if signal:
                invest    = capital * trade_pct
                fee       = invest * TAKER_FEE
                coin_amt  = (invest - fee) / c_now
                capital  -= invest
                stop      = c_now * (1 - sl_pct / 100)
                pos = {"entry": c_now, "amount": coin_amt, "usd_in": invest,
                       "highest": c_now, "stop": stop, "partial": False}

        prev_macd = macd
        unrealized = pos["amount"] * c_now if pos else 0
        equity_curve.append(capital + unrealized)

    # Close any open position at end of data
    if pos:
        c_last = closes[-1]
        proceeds = pos["amount"] * c_last * (1 - TAKER_FEE)
        capital += proceeds
        net_pnl = (c_last - pos["entry"]) / pos["entry"] * 100 - 2 * TAKER_FEE * 100
        trades.append({
            "type": "END_CLOSE", "entry": pos["entry"], "exit": c_last,
            "pnl_pct": round(net_pnl, 3), "ts": times[-1], "regime": "END",
        })
        equity_curve[-1] = capital

    # ── Metrics ───────────────────────────────────────────────────────────────
    closed = [t for t in trades if t["type"] != "PARTIAL_TP"]
    wins   = [t for t in closed if t["pnl_pct"] > 0]
    losses = [t for t in closed if t["pnl_pct"] <= 0]

    total_return  = (capital - initial_capital) / initial_capital * 100
    win_rate      = len(wins) / len(closed) * 100 if closed else 0
    avg_win       = sum(t["pnl_pct"] for t in wins)   / len(wins)   if wins   else 0
    avg_loss      = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    gross_wins    = sum(t["pnl_pct"] for t in wins)
    gross_losses  = abs(sum(t["pnl_pct"] for t in losses)) or 1e-9
    profit_factor = gross_wins / gross_losses

    # Max drawdown
    peak, max_dd = equity_curve[0], 0.0
    for eq in equity_curve:
        if eq > peak: peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd: max_dd = dd

    # Annualised Sharpe (hourly returns → ×√8760)
    rets = [(equity_curve[i] - equity_curve[i-1]) / equity_curve[i-1]
            for i in range(1, len(equity_curve))]
    if len(rets) > 1:
        mu  = sum(rets) / len(rets)
        std = (sum((r - mu)**2 for r in rets) / len(rets)) ** 0.5
        sharpe = (mu / std * (8760 ** 0.5)) if std > 0 else 0.0
    else:
        sharpe = 0.0

    # Regime breakdown
    by_regime: dict = {}
    for t in closed:
        reg = t.get("regime", "?")
        by_regime.setdefault(reg, {"wins": 0, "total": 0})
        by_regime[reg]["total"] += 1
        if t["pnl_pct"] > 0: by_regime[reg]["wins"] += 1

    return {
        "total_candles":   len(candles),
        "warmup_bars":     WARMUP,
        "total_trades":    len(closed),
        "partial_takes":   len([t for t in trades if t["type"] == "PARTIAL_TP"]),
        "wins":            len(wins),
        "losses":          len(losses),
        "win_rate":        round(win_rate, 1),
        "avg_win_pct":     round(avg_win, 3),
        "avg_loss_pct":    round(avg_loss, 3),
        "profit_factor":   round(profit_factor, 2),
        "sharpe_ratio":    round(sharpe, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "total_return_pct": round(total_return, 2),
        "final_capital":   round(capital, 2),
        "initial_capital": round(initial_capital, 2),
        "by_regime":       {k: {**v, "win_rate": round(v["wins"] / v["total"] * 100, 1)}
                            for k, v in by_regime.items() if v["total"] > 0},
        "fee_drag_pct":    round(trade_pct * len(closed) * 2 * TAKER_FEE * 100, 2),
    }


def print_results(pair: str, days: int, results: dict):
    if "error" in results:
        print(f"\nError: {results['error']}")
        return
    r = results
    print(f"\n{'═'*54}")
    print(f"  BACKTEST: {pair}  ({days}d, {r['total_candles']} candles)")
    print(f"{'═'*54}")
    print(f"  Trades:          {r['total_trades']}  ({r['partial_takes']} partial TPs)")
    print(f"  Win rate:        {r['win_rate']}%  ({r['wins']}W / {r['losses']}L)")
    print(f"  Avg win:         {r['avg_win_pct']:+.2f}%   Avg loss: {r['avg_loss_pct']:+.2f}%")
    print(f"  Profit factor:   {r['profit_factor']:.2f}  (>1.5 = good)")
    print(f"  Sharpe ratio:    {r['sharpe_ratio']:.2f}  (>1.0 = good)")
    print(f"  Max drawdown:    -{r['max_drawdown_pct']:.2f}%")
    print(f"  Total return:    {r['total_return_pct']:+.2f}%")
    print(f"  Fee drag:        -{r['fee_drag_pct']:.2f}%  (0.6%×2 per trade)")
    print(f"  Capital: ${r['initial_capital']:.0f} → ${r['final_capital']:.2f}")
    if r.get("by_regime"):
        print(f"{'─'*54}")
        print("  By regime:")
        for regime, v in sorted(r["by_regime"].items()):
            print(f"    {regime:8s}  {v['total']:3d} trades  {v['win_rate']}% win rate")
    print(f"{'═'*54}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TradeBot Backtesting Engine")
    parser.add_argument("--pair",        default="BTC-USD",   help="e.g. BTC-USD")
    parser.add_argument("--days",        type=int, default=180)
    parser.add_argument("--capital",     type=float, default=1000.0)
    parser.add_argument("--granularity", default="ONE_HOUR",
                        choices=["ONE_HOUR", "SIX_HOUR", "ONE_DAY"])
    parser.add_argument("--trade-pct",   type=float, default=0.10,
                        help="Fraction of capital per trade (default 0.10 = 10%%)")
    parser.add_argument("--sl",          type=float, default=1.5,
                        help="Stop-loss %%  (default 1.5)")
    parser.add_argument("--partial-tp",  type=float, default=3.0,
                        help="Partial TP %%  (default 3.0)")
    parser.add_argument("--trail",       type=float, default=2.0,
                        help="Trailing stop %%  (default 2.0)")
    parser.add_argument("--json",        action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    pair = args.pair.upper()
    print(f"Fetching {args.days}d of {args.granularity} candles for {pair}…")
    candles = fetch_all_candles(pair, args.days, args.granularity)
    print(f"\nFetched {len(candles)} candles total")

    if len(candles) < 60:
        print("Error: Not enough candles. Check Coinbase API credentials in settings.json.")
        sys.exit(1)

    print("Running backtest…\n")
    results = run_backtest(
        candles,
        initial_capital=args.capital,
        trade_pct=args.trade_pct,
        sl_pct=args.sl,
        partial_tp_pct=args.partial_tp,
        trail_pct=args.trail,
    )

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_results(pair, args.days, results)
