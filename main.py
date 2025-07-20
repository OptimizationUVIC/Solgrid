import requests
import pandas as pd
import time
from datetime import datetime, timedelta, timezone

# === CONFIGURATION ===
SYMBOL = "SOLUSDT"
INTERVAL = "30m"
CAPITAL_INIT = 100.0
LEVERAGE = 5
ATR_PERIOD = 14
FEE_PCT = 0.0004
RESET_PERIOD = timedelta(hours=12)
RESET_THRESH = 0.015
LEVELS = 5

# === OPTIMAL PARAMS ===
PARAMS = {
    "step_mult": 1.1957217972768404,
    "tp_mult": 0.9252048829165564,
    "min_step_pct": 0.0024893546034633696,
    "max_step_pct": 0.008319494220199349,
    "min_tp_pct": 0.0032564091699870644,
    "max_tp_pct": 0.011950150883347594,
    "order_risk": 0.0996849332361371,
    "stop_grid": 0.017766093436744096,
}

# === STATE ===
equity = capital = CAPITAL_INIT
pivot = pivot_ts = None
open_trade = None
wins = losses = 0
last_processed_ts = None

# === FETCH LATEST CANDLES ===
def fetch_latest_klines(symbol=SYMBOL, interval=INTERVAL, lookback=ATR_PERIOD + 2):
    url = "https://fapi.binance.com/fapi/v1/klines"
    end = int(time.time() * 1000)
    start = end - lookback * 30 * 60 * 1000
    params = {"symbol": symbol, "interval": interval, "startTime": start, "limit": lookback}
    data = requests.get(url, params=params).json()
    df = pd.DataFrame(data, columns=[
        "ts", "open", "high", "low", "close", "volume",
        "ct", "qav", "nt", "tb", "tq", "ig"
    ])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)
    df = df[["ts", "open", "high", "low", "close"]]
    df["atr"] = (df["high"] - df["low"]).rolling(ATR_PERIOD).mean()
    return df.dropna()

# === PROCESS LAST BAR ===
def process_bar(row):
    global pivot, pivot_ts, open_trade, equity, wins, losses, capital

    now, high, low, close, atr = row.ts, row.high, row.low, row.close, row.atr

    # Init pivot
    if pivot is None:
        pivot, pivot_ts = close, now

    # Dynamic step/tp
    step_pct = min(max((atr / close) * PARAMS["step_mult"], PARAMS["min_step_pct"]), PARAMS["max_step_pct"])
    tp_pct   = min(max((atr / close) * PARAMS["tp_mult"],   PARAMS["min_tp_pct"]), PARAMS["max_tp_pct"])
    next_buy = [pivot * (1 - step_pct * (i + 1)) for i in range(LEVELS)]

    # Reset pivot (time or distance)
    if (now - pivot_ts >= RESET_PERIOD) or abs(close - pivot) / pivot >= RESET_THRESH:
        if open_trade:
            qty = open_trade["qty"]
            pnl = (close - open_trade["entry"]) * qty
            fee = FEE_PCT * qty * close
            equity += pnl - fee
            if pnl > 0:
                wins += 1
            else:
                losses += 1
            print(f"[{now}] 🔁 Pivot reset. ➡️ Closed trade | PnL: {pnl - fee:.2f} USDT")
            open_trade = None

        pivot = close
        pivot_ts = now
        capital = equity  # Optional: dynamic capital
        return

    # ENTRY
    if open_trade is None and low <= next_buy[0]:
        entry = next_buy[0]
        risk_usdt = capital * PARAMS["order_risk"]
        qty = (risk_usdt * LEVERAGE) / entry
        tp = entry * (1 + tp_pct)
        fee = FEE_PCT * qty * entry
        equity -= fee
        open_trade = {
            "entry": entry,
            "qty": qty,
            "tp": tp,
            "entry_ts": now
        }
        print(f"[{now}] 🟢 Entry at {entry:.3f} | TP: {tp:.3f} | Qty: {qty:.4f}")
        return

    # TP HIT
    if open_trade and high >= open_trade["tp"]:
        exit_price = open_trade["tp"]
        qty = open_trade["qty"]
        pnl = (exit_price - open_trade["entry"]) * qty
        fee = FEE_PCT * qty * exit_price
        equity += pnl - fee
        wins += 1
        open_trade = None
        print(f"[{now}] ✅ TP hit! +{pnl - fee:.2f} USDT")
        return

    # STOP GRID
    if open_trade and close <= pivot * (1 - PARAMS["stop_grid"]):
        exit_price = close
        qty = open_trade["qty"]
        pnl = (exit_price - open_trade["entry"]) * qty
        fee = FEE_PCT * qty * exit_price
        equity += pnl - fee
        if pnl > 0:
            wins += 1
        else:
            losses += 1
        open_trade = None
        print(f"[{now}] ❌ STOP_GRID hit. PnL: {pnl - fee:.2f} USDT")

# === LIVE LOOP ===
if __name__ == "__main__":
    print("🔁 Starting Live Adaptive Grid Paper Trading Bot...")
    while True:
        try:
            df = fetch_latest_klines()
            last_bar = df.iloc[-1]
            bar_ts = last_bar.ts

            if last_processed_ts is None or bar_ts > last_processed_ts:
                process_bar(last_bar)
                last_processed_ts = bar_ts
                total_trades = wins + losses
                winrate = (wins / total_trades * 100) if total_trades > 0 else 0
                print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}] 💼 Equity: {equity:.2f} | Trades: {total_trades} | ✅ Winrate: {winrate:.1f}%\n")

            time.sleep(10)

        except Exception as e:
            print(f"❌ Error: {e}")
            time.sleep(15)
