import os
import random
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# =========================
# CONFIG
# =========================

TOKEN = os.getenv("BOT_TOKEN")

coins = [
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "LTCUSDT"
]

# =========================
# LOGGING
# =========================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# =========================
# SIGNAL ENGINE
# =========================

def generate_signal(symbol):

    # Fake market simulation (for testing)
    price = round(random.uniform(50, 120000), 2)

    rsi = random.randint(15, 90)

    ema_trend = random.choice(["bullish", "bearish"])

    volume_strength = random.randint(40, 100)

    momentum = random.randint(40, 100)

    # =========================
    # SCORE LOGIC
    # =========================

    score = 0

    # RSI Logic
    if 25 <= rsi <= 45:
        score += 35
    elif 45 < rsi <= 60:
        score += 15
    elif rsi > 75:
        score -= 30

    # Trend Logic
    if ema_trend == "bullish":
        score += 25
    else:
        score -= 10

    # Volume
    score += volume_strength * 0.2

    # Momentum
    score += momentum * 0.2

    score = round(score, 2)

    # =========================
    # SIGNAL TYPE
    # =========================

    if score >= 70:
        signal = "🟢 STRONG BUY"
        direction = "LONG"
        leverage = "3x - 5x"
        risk = "LOW"

    elif score >= 50:
        signal = "🟡 BUY"
        direction = "LONG"
        leverage = "2x - 3x"
        risk = "MEDIUM"

    elif score <= 0:
        signal = "🔴 SHORT"
        direction = "SHORT"
        leverage = "2x - 5x"
        risk = "HIGH"

    else:
        signal = "⚪ WAIT"
        direction = "NO TRADE"
        leverage = "Avoid"
        risk = "HIGH"

    # =========================
    # ENTRY / TP / SL
    # =========================

    if direction == "LONG":

        entry_low = round(price * 0.998, 2)
        entry_high = round(price * 1.002, 2)

        stop_loss = round(price * 0.98, 2)

        tp1 = round(price * 1.015, 2)
        tp2 = round(price * 1.03, 2)
        tp3 = round(price * 1.05, 2)

    elif direction == "SHORT":

        entry_low = round(price * 0.998, 2)
        entry_high = round(price * 1.002, 2)

        stop_loss = round(price * 1.02, 2)

        tp1 = round(price * 0.985, 2)
        tp2 = round(price * 0.97, 2)
        tp3 = round(price * 0.95, 2)

    else:

        entry_low = "-"
        entry_high = "-"
        stop_loss = "-"
        tp1 = "-"
        tp2 = "-"
        tp3 = "-"

    return {
        "symbol": symbol,
        "signal": signal,
        "direction": direction,
        "score": score,
        "rsi": rsi,
        "trend": ema_trend,
        "volume": volume_strength,
        "momentum": momentum,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "sl": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "leverage": leverage,
        "risk": risk
    }

# =========================
# /start
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    msg = (
        "🚀 PRO SIGNAL BOT V3\n\n"
        "Commands:\n"
        "/signals → Top ranked futures signals\n"
    )

    await update.message.reply_text(msg)

# =========================
# /signals
# =========================

async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE):

    all_signals = []

    for coin in coins:
        data = generate_signal(coin)
        all_signals.append(data)

    # Sort by score
    ranked = sorted(all_signals, key=lambda x: x["score"], reverse=True)

    message = "📊 TOP 10 FUTURES SIGNALS\n\n"

    for i, s in enumerate(ranked[:10], start=1):

        message += (
            f"{i}. {s['symbol']}\n"
            f"{s['signal']}\n"
            f"Direction: {s['direction']}\n"
            f"Confidence Score: {s['score']}\n\n"

            f"💰 Entry Zone:\n"
            f"{s['entry_low']} - {s['entry_high']}\n\n"

            f"🛑 Stop Loss:\n"
            f"{s['sl']}\n\n"

            f"🎯 Take Profits:\n"
            f"TP1: {s['tp1']}\n"
            f"TP2: {s['tp2']}\n"
            f"TP3: {s['tp3']}\n\n"

            f"⚡ Safe Leverage:\n"
            f"{s['leverage']}\n\n"

            f"📈 Trend: {s['trend']}\n"
            f"📊 RSI: {s['rsi']}\n"
            f"📦 Volume Strength: {s['volume']}\n"
            f"🔥 Momentum: {s['momentum']}\n"
            f"⚠️ Risk: {s['risk']}\n\n"
            f"━━━━━━━━━━━━━━━\n\n"
        )

    await update.message.reply_text(message)

# =========================
# MAIN
# =========================

def main():

    print("🚀 PRO SIGNAL BOT V3 RUNNING")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signals", signals))

    app.run_polling(close_loop=False)

# =========================
# START
# =========================

if __name__ == "__main__":
    main()
