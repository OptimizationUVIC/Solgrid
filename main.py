import requests
import pandas as pd
import time
from datetime import datetime, timezone, timedelta

# === CONFIGURATION ===
SYMBOL = "SOLUSDT"
INTERVAL = "30m"
CAPITAL_INIT = 100.0

ATR_PERIOD = 14
STEP_MULT = 1.6
TP_MULT = 1.5
MIN_STEP_PCT = 0.002
MAX_STEP_PCT = 0.007
MIN_TP_PCT = 0.002
MAX_TP_PCT = 0.01

LEVELS = 6
ORDER_RISK = 0.06
STOP_GRID = 0.02
RESET_PERIOD = timedelta(hours=12)
RESET_THRESH = 0.015
FEE_PCT = 0.0004
LEVERAGE = 10

# === STATE ===
equity = capital = CAPITAL_INIT
pivot = pivot_ts = None
open_trade = None
wins = losses = 0
last_processed_ts = None

# === FETCH DATA ===
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

# === PROCESS NEW BAR ===
def process_bar(row):
    global pivot, pivot_ts, open_trade, equity, wins, losses, capital

    now, high, low, close, atr = row.ts, row.high, row.low, row.close, row.atr

    # Init pivot
    if pivot is None:
        pivot, pivot_ts = close, now

    # Calcule step / tp dynamiques
    step_pct = min(max((atr / close) * STEP_MULT, MIN_STEP_PCT), MAX_STEP_PCT)
    tp_pct   = min(max((atr / close) * TP_MULT,   MIN_TP_PCT), MAX_TP_PCT)
    next_buy = [pivot * (1 - step_pct * (i + 1)) for i in range(LEVELS)]

    # Reset pivot (temps ou distance)
    if (now - pivot_ts >= RESET_PERIOD) or abs(close - pivot) / pivot >= RESET_THRESH:
        if open_trade:
            exit_price = close
            qty = open_trade["qty"]
            pnl = (exit_price - open_trade["entry"]) * qty
            fee = FEE_PCT * qty * exit_price
            equity += pnl - fee
            wins += int(pnl > 0)
            losses += int(pnl <= 0)
            print(f"[{now}] 🔄 Pivot reset | Closed open trade: {pnl - fee:.2f} USDT")
            open_trade = None

        pivot, pivot_ts = close, now
        capital = equity  # facultatif : capital dynamique
        return

    # ENTRY
    if open_trade is None and low <= next_buy[0]:
        entry = next_buy[0]
        risk_usdt = capital * ORDER_RISK
        qty = (risk_usdt * LEVERAGE) / entry
        tp = entry * (1 + tp_pct)
        sl = entry * (1 - 1.2 * atr / close)
        fee = FEE_PCT * qty * entry
        equity -= fee
        open_trade = {
            "entry": entry,
            "qty": qty,
            "tp": tp,
            "sl": sl,
            "entry_ts": now
        }
        print(f"[{now}] 📈 Entry at {entry:.3f}, TP {tp:.3f}, SL {sl:.3f}, Qty {qty:.4f}")
        return

    # TP / SL / TIMEOUT
    if open_trade:
        qty = open_trade["qty"]
        entry_price = open_trade["entry"]

        if high >= open_trade["tp"]:
            exit_price = open_trade["tp"]
            pnl = (exit_price - entry_price) * qty
            fee = FEE_PCT * qty * exit_price
            equity += pnl - fee
            wins += 1
            open_trade = None
            print(f"[{now}] ✅ TP hit: {pnl - fee:.2f} USDT")

        elif low <= open_trade["sl"]:
            exit_price = open_trade["sl"]
            pnl = (exit_price - entry_price) * qty
            fee = FEE_PCT * qty * exit_price
            equity += pnl - fee
            losses += 1
            open_trade = None
            print(f"[{now}] ❌ SL hit: {pnl - fee:.2f} USDT")

        elif now - open_trade["entry_ts"] > timedelta(hours=6):
            exit_price = close
            pnl = (exit_price - entry_price) * qty
            fee = FEE_PCT * qty * exit_price
            equity += pnl - fee
            wins += int(pnl > 0)
            losses += int(pnl <= 0)
            open_trade = None
            print(f"[{now}] ⏳ Timeout exit: {pnl - fee:.2f} USDT")

# === LIVE LOOP ===
if __name__ == "__main__":
    print("🔁 Starting Adaptive Grid Live Paper Bot in real-time loop...")
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
                print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] Equity: {equity:.2f} | Trades: {total_trades} | Winrate: {winrate:.1f}%")

            time.sleep(5)  # Poll every 5 seconds

        except Exception as e:
            print(f"❌ Error: {e}")
            time.sleep(10)
