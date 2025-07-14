import asyncio
import websockets
import json
import pandas as pd
import numpy as np
import aiohttp
from datetime import datetime, timezone
from collections import defaultdict

# === PARAMETRES ===
SYMBOLS = ["FARTCOINUSDT", "1000PEPEUSDT", "1000BONKUSDT", "WIFUSDT"]
INTERVAL = "30m"
BASE_CAPITAL = 100.0
LEVERAGE = 10
TP_PCT = 0.006
SL_PCT = 0.003
MAX_TRADES_PER_DAY = 15
RISK_PER_TRADE = 0.05
EMA_FAST = 10
EMA_SLOW = 21
ADX_WINDOW = 14
VOL_SMA_WINDOW = 20

# === GLOBAL STATE ===
capital_global = BASE_CAPITAL
trades_data = defaultdict(list)
daily_count = defaultdict(lambda: defaultdict(int))
last_report_time = None
positions = {}
last_debug_log = defaultdict(lambda: datetime.min.replace(tzinfo=timezone.utc))


# === INDICATEURS ===
def add_indicators(df):
    df = df.copy()
    df["EMA_F"] = df["close"].ewm(span=EMA_FAST).mean()
    df["EMA_S"] = df["close"].ewm(span=EMA_SLOW).mean()
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs()
    ], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(ADX_WINDOW).mean()
    up = df["high"].diff().clip(lower=0)
    down = -df["low"].diff().clip(upper=0)
    df["+DI"] = (up.rolling(ADX_WINDOW).mean() / df["ATR"]) * 100
    df["-DI"] = (down.rolling(ADX_WINDOW).mean() / df["ATR"]) * 100
    dx = (df["+DI"] - df["-DI"]).abs() / (df["+DI"] + df["-DI"].replace(0, 1)) * 100
    df["ADX"] = dx.rolling(ADX_WINDOW).mean().fillna(0)
    df["VOL_SMA"] = df["volume"].rolling(VOL_SMA_WINDOW).mean()
    return df


# === SIGNAL ===
def evaluate_signal(df):
    last = df.iloc[-1]
    reasons = []

    if last["close"] <= last["EMA_F"]:
        reasons.append("Close <= EMA_F")
    if last["ADX"] <= 20:
        reasons.append("ADX <= 20")
    if last["+DI"] <= last["-DI"]:
        reasons.append("+DI <= -DI")
    if last["volume"] <= 1.5 * last["VOL_SMA"]:
        reasons.append("Volume <= 1.5 * VOL_SMA")
    if last["close"] <= last["open"]:
        reasons.append("Red candle")

    if reasons:
        print(f"  ❌ Pas de signal - Conditions manquantes: {', '.join(reasons)}")
        return False

    print(f"  ✅ SIGNAL D'ENTRÉE - Toutes les conditions sont remplies!")
    return True


async def fetch_initial_candles(symbol):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={INTERVAL}&limit=150"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            if not isinstance(data, list):
                raise ValueError(f"Erreur récupération données {symbol} : {data}")
            candles = []
            for k in data:
                candles.append({
                    "timestamp": pd.to_datetime(int(k[0]) // 1000, unit="s"),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                })
            return candles


async def handle_stream(symbol):
    global capital_global
    uri = f"wss://fstream.binance.com/ws/{symbol.lower()}@kline_{INTERVAL}"
    candles = await fetch_initial_candles(symbol)
    print(f"📋 [{symbol}] {len(candles)} bougies historiques récupérées")

    while True:
        try:
            async with websockets.connect(uri) as ws:
                print(f"\n🔌 [{symbol}] WebSocket connecté avec succès")

                # Tâche pour envoyer des pings réguliers
                async def send_ping():
                    while True:
                        await asyncio.sleep(180)  # Ping toutes les 3 minutes
                        try:
                            await ws.ping()
                            # print(f"🏓 [{symbol}] Ping envoyé pour maintenir la connexion")
                        except:
                            break

                # Lance la tâche de ping en arrière-plan
                ping_task = asyncio.create_task(send_ping())

                try:
                    async for msg in ws:
                        try:
                            data = json.loads(msg)
                            k = data.get("k", {})
                            now = datetime.now(timezone.utc)

                            # Log de debug périodique (réduit la fréquence)
                            if (now - last_debug_log[symbol]).total_seconds() >= 1800:  # 30 minutes
                                print(
                                    f"📡 [{now.strftime('%H:%M:%S')}] {symbol} - WebSocket actif, en attente de bougies fermées...")
                                last_debug_log[symbol] = now

                            # Vérifier si la bougie est fermée
                            if not k.get('x'):
                                # Ignorer les données de la bougie en cours
                                continue

                            # Traiter la bougie fermée
                            candle = {
                                "timestamp": pd.to_datetime(int(k["t"]) // 1000, unit="s"),
                                "open": float(k.get("o")),
                                "high": float(k.get("h")),
                                "low": float(k.get("l")),
                                "close": float(k.get("c")),
                                "volume": float(k.get("v")),
                            }

                            print(
                                f"\n📊 [{symbol}] Nouvelle bougie {INTERVAL} fermée à {candle['timestamp'].strftime('%H:%M:%S')}")
                            print(
                                f"  └─ Prix: O:{candle['open']:.4f} H:{candle['high']:.4f} L:{candle['low']:.4f} C:{candle['close']:.4f} | Vol: {candle['volume']:,.0f}")

                            candles.append(candle)

                            # Garder seulement les 150 dernières bougies
                            if len(candles) > 150:
                                candles = candles[-150:]

                            if len(candles) < ADX_WINDOW:
                                print(
                                    f"  ⚠️  [{symbol}] Pas assez de données pour calculer les indicateurs ({len(candles)}/{ADX_WINDOW})")
                                continue

                            df = pd.DataFrame(candles)
                            df = add_indicators(df).dropna()

                            if df.empty:
                                print(f"  ⚠️  [{symbol}] DataFrame vide après calcul des indicateurs")
                                continue

                            date_str = df.iloc[-1]["timestamp"].date().isoformat()

                            # Logique de trading
                            if symbol not in positions and daily_count[symbol][date_str] < MAX_TRADES_PER_DAY:
                                print(f"  🔍 [{symbol}] Évaluation du signal...")
                                if evaluate_signal(df):
                                    entry_price = df.iloc[-1]["close"]
                                    print(f"\n💰 OUVERTURE POSITION {symbol}")
                                    print(f"  ├─ Type: LONG")
                                    print(f"  ├─ Prix d'entrée: {entry_price:.4f}")
                                    print(f"  ├─ Take Profit: {entry_price * (1 + TP_PCT):.4f} (+{TP_PCT * 100:.1f}%)")
                                    print(f"  └─ Stop Loss: {entry_price * (1 - SL_PCT):.4f} (-{SL_PCT * 100:.1f}%)")
                                    positions[symbol] = entry_price

                            elif symbol in positions:
                                current = df.iloc[-1]["close"]
                                entry = positions[symbol]
                                raw_ret = (current - entry) / entry

                                # Vérifier si TP ou SL est atteint
                                if raw_ret >= TP_PCT or raw_ret <= -SL_PCT:
                                    ret = np.sign(raw_ret) * min(abs(raw_ret), TP_PCT if raw_ret > 0 else SL_PCT)
                                    pos_size = capital_global * RISK_PER_TRADE * LEVERAGE
                                    pnl = pos_size * ret
                                    capital_global += pnl
                                    trades_data[symbol].append({
                                        "timestamp": candle['timestamp'],
                                        "pnl": pnl,
                                        "return": ret,
                                        "entry_price": entry,
                                        "exit_price": current
                                    })
                                    daily_count[symbol][date_str] += 1

                                    reason = "Take Profit" if raw_ret >= TP_PCT else "Stop Loss"
                                    print(f"\n🏁 FERMETURE POSITION {symbol} ({reason})")
                                    print(f"  ├─ Prix d'entrée: {entry:.4f}")
                                    print(f"  ├─ Prix de sortie: {current:.4f}")
                                    print(f"  ├─ Performance: {ret * 100:+.2f}%")
                                    print(f"  ├─ PnL: ${pnl:+.2f}")
                                    print(f"  └─ Capital total: ${capital_global:.2f}")
                                    del positions[symbol]
                                else:
                                    print(
                                        f"  📊 [{symbol}] Position ouverte: {raw_ret * 100:+.2f}% (Entry: {entry:.4f}, Current: {current:.4f})")

                            await maybe_print_report()

                        except json.JSONDecodeError as e:
                            print(f"⚠️  [{symbol}] Erreur JSON: {e}")
                        except Exception as inner_e:
                            print(f"⚠️  [{symbol}] Erreur traitement bougie: {inner_e}")
                            import traceback
                            traceback.print_exc()

                finally:
                    # Annule la tâche de ping
                    ping_task.cancel()

        except websockets.exceptions.ConnectionClosed as e:
            print(f"🔴 [{symbol}] Connexion WebSocket fermée: {e}")
            print(f"  └─ Reconnexion dans 5 secondes...")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"🔴 [{symbol}] Erreur WebSocket: {e}")
            print(f"  └─ Reconnexion dans 5 secondes...")
            await asyncio.sleep(5)


async def maybe_print_report():
    global last_report_time
    now = datetime.now(timezone.utc)
    if last_report_time is None or (now - last_report_time).total_seconds() >= 1800:
        last_report_time = now
        print("\n" + "=" * 60)
        print(f"📊 RAPPORT PÉRIODIQUE - {now.strftime('%H:%M:%S UTC')}")
        print("=" * 60)

        total_trades = sum(len(trades_data[sym]) for sym in SYMBOLS)
        total_pnl = capital_global - BASE_CAPITAL
        win_trades = sum(len([t for t in trades_data[sym] if t['pnl'] > 0]) for sym in SYMBOLS)

        print("\n📈 PERFORMANCE PAR SYMBOLE:")
        for symbol in SYMBOLS:
            n_trades = len(trades_data[symbol])
            wins = len([t for t in trades_data[symbol] if t['pnl'] > 0])
            pnl_sum = sum(t['pnl'] for t in trades_data[symbol])
            roi = pnl_sum / BASE_CAPITAL * 100
            winrate = wins / n_trades * 100 if n_trades else 0
            status = "📈" if symbol in positions else "⏸️"
            print(f"  {status} {symbol:15} | Trades: {n_trades:3} | Win Rate: {winrate:5.1f}% | PnL: ${pnl_sum:+7.2f}")

        roi_global = total_pnl / BASE_CAPITAL * 100
        winrate_global = win_trades / total_trades * 100 if total_trades else 0

        print("\n💼 RÉSUMÉ GLOBAL:")
        print(f"  ├─ Capital actuel: ${capital_global:.2f}")
        print(f"  ├─ Performance: ${total_pnl:+.2f} ({roi_global:+.2f}%)")
        print(f"  ├─ Nombre de trades: {total_trades}")
        print(f"  ├─ Positions ouvertes: {len(positions)}")
        print(f"  └─ Taux de réussite: {winrate_global:.1f}%")

        if positions:
            print("\n🔄 POSITIONS ACTUELLES:")
            for sym, entry_price in positions.items():
                print(f"  └─ {sym}: Entrée à {entry_price:.4f}")

        print("=" * 60 + "\n")


async def main():
    print("\n" + "=" * 60)
    print("🚀 BOT DE TRADING CRYPTO - DÉMARRAGE")
    print("=" * 60)
    print(f"📊 Symboles: {', '.join(SYMBOLS)}")
    print(f"⏰ Intervalle: {INTERVAL}")
    print(f"💰 Capital initial: ${BASE_CAPITAL}")
    print(f"📈 Leverage: {LEVERAGE}x")
    print(f"🎯 TP/SL: +{TP_PCT * 100:.1f}% / -{SL_PCT * 100:.1f}%")
    print(f"📅 Max trades/jour: {MAX_TRADES_PER_DAY}")
    print(f"⚖️  Risque par trade: {RISK_PER_TRADE * 100:.1f}%")
    print("=" * 60 + "\n")

    # Lancer tous les streams en parallèle
    tasks = [handle_stream(sym) for sym in SYMBOLS]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Arrêt du bot demandé par l'utilisateur")
    except Exception as e:
        print(f"\n💥 Erreur fatale: {e}")
        import traceback

        traceback.print_exc()