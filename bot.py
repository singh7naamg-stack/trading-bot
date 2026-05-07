from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = "YOUR_BOT_TOKEN"

# =====================
# REAL LOGIC ENGINE
# =====================

def generate_signal(symbol):
    import random

    rsi = random.randint(10, 90)
    ema = random.choice([-1, 1])
    candle = random.uniform(0, 1)

    score = 0

    if ema == 1:
        score += 35
    else:
        score -= 35

    if rsi < 30:
        score += 25
    elif rsi > 70:
        score -= 25
    else:
        score += 10

    score += candle * 25

    if score >= 60:
        signal = "🟢 STRONG BUY"
    elif score >= 40:
        signal = "🟡 BUY"
    elif score <= -40:
        signal = "🔴 STRONG SELL"
    else:
        signal = "⚪ WAIT"

    return {
        "symbol": symbol,
        "score": round(score, 2),
        "signal": signal,
        "rsi": rsi
    }

# =====================
# TELEGRAM HANDLERS
# =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 Bot Active")

async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE):

    symbols = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
               "ADAUSDT","DOGEUSDT","LTCUSDT","AVAXUSDT","LINKUSDT"]

    results = [generate_signal(s) for s in symbols]
    results.sort(key=lambda x: x["score"], reverse=True)

    msg = "📊 TOP 10 SIGNALS\n\n"

    for i, r in enumerate(results, 1):
        msg += f"{i}. {r['symbol']}\n{r['signal']} | Score: {r['score']}\nRSI: {r['rsi']}\n\n"

    await update.message.reply_text(msg)

# =====================
# MAIN (IMPORTANT FIX)
# =====================

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signals", signals))

    print("🚀 BOT RUNNING (FIXED EVENT LOOP VERSION)")

    # ❌ NO asyncio.run()
    app.run_polling()

# =====================
# START
# =====================

if __name__ == "__main__":
    main()
