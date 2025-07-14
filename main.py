import requests
import pandas as pd
import time
from datetime import datetime, timezone, timedelta

# === CONFIGURATION ===
SYMBOL        = "SOLUSDT"
INTERVAL      = "30m"
CAPITAL_INIT  = 100.0

ATR_PERIOD    = 14
STEP_MULT     = 1.6
TP_MULT       = 1.5
MIN_STEP_PCT  = 0.002
MAX_STEP_PCT  = 0.007
MIN_TP_PCT    = 0.002
MAX_TP_PCT    = 0.01

LEVELS        = 6
ORDER_RISK    = 0.06
STOP_GRID     = 0.04
RESET_PERIOD  = timedelta(hours=12)
RESET_THRESH  = 0.015
FEE_PCT       = 0.0004
LEVERAGE      = 10

# State variables
equity = CAPITAL_INIT
capital = CAPITAL_INIT
pivot = None
pivot_ts = None
open_trade = None

wins = 0
losses = 0

# === FETCH LATEST DATA ===
def fetch_latest_klines(symbol=SYMBOL, interval=INTERVAL, lookback=ATR_PERIOD+2):
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
    global pivot, pivot_ts, open_trade, equity, wins, losses

    now, high, low, close, atr = row.ts, row.high, row.low, row.close, row.atr

    # Initialize pivot on first run
    if pivot is None:
        pivot, pivot_ts = close, now

    # Compute adaptive steps
    step_pct = min(max((atr / close) * STEP_MULT, MIN_STEP_PCT), MAX_STEP_PCT)
    tp_pct   = min(max((atr / close) * TP_MULT, MIN_TP_PCT), MAX_TP_PCT)
    next_buy = [pivot * (1 - step_pct * (i + 1)) for i in range(LEVELS)]

    # Reset pivot if needed
    if (now - pivot_ts >= RESET_PERIOD) or abs(close - pivot) / pivot >= RESET_THRESH:
        if open_trade:
            exit_price = close
            qty = open_trade["qty"]
            pnl = (exit_price - open_trade["entry"]) * qty
            fee = FEE_PCT * qty * exit_price
            equity += pnl - fee
            if pnl > 0: wins += 1
            else: losses += 1
            open_trade = None
        pivot, pivot_ts = close, now
        return

    # Entry condition
    if open_trade is None and next_buy and low <= next_buy[0]:
        entry = next_buy[0]
        if entry == 0:
            return
        risk_usdt = capital * ORDER_RISK
        qty = (risk_usdt * LEVERAGE) / entry
        tp = entry * (1 + tp_pct)
        fee = FEE_PCT * qty * entry
        equity -= fee
        open_trade = {"entry": entry, "qty": qty, "tp": tp}
        print(f"[{now}] 📈 Entry at {entry:.3f}, TP {tp:.3f}, Qty {qty:.4f}")

    # TP or Stop
    if open_trade:
        # Take profit
        if high >= open_trade["tp"]:
            exit_price = open_trade["tp"]
            qty = open_trade["qty"]
            pnl = (exit_price - open_trade["entry"]) * qty
            fee = FEE_PCT * qty * exit_price
            equity += pnl - fee
            wins += 1
            print(f"[{now}] ✅ TP hit: {pnl - fee:.2f} USDT")
            open_trade = None
        # Stop-grid
        elif close <= pivot * (1 - STOP_GRID):
            exit_price = close
            qty = open_trade["qty"]
            pnl = (exit_price - open_trade["entry"]) * qty
            fee = FEE_PCT * qty * exit_price
            equity += pnl - fee
            if pnl > 0: wins += 1
            else: losses += 1
            print(f"[{now}] ❌ Stop hit: {pnl - fee:.2f} USDT")
            open_trade = None
            pivot, pivot_ts = close, now

# === LIVE LOOP ===
if __name__ == "__main__":
    print("🔁 Starting Adaptive Grid Live Paper Bot in loop...")
    while True:
        try:
            df = fetch_latest_klines()
            process_bar(df.iloc[-1])
            total_trades = wins + losses
            winrate = (wins / total_trades * 100) if total_trades > 0 else 0
            print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z')}] Equity: {equity:.2f} | Trades: {total_trades} | Winrate: {winrate:.1f}%")
        except Exception as e:
            print(f"❌ Error: {e}")

        # Wait until next 30m candle close
        now = datetime.now(timezone.utc)
        next_run = (now + timedelta(minutes=30)).replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
        sleep_secs = (next_run - now).total_seconds()
        time.sleep(max(sleep_secs, 0))
