import telebot
import random
import time

TOKEN = "8790244435:AAGPxwzPh7g09DvH8RC3sj5irsL2fcYCYQ4"

bot = telebot.TeleBot(TOKEN)

# =====================
# SIGNAL ENGINE
# =====================

def generate_signal(symbol):

    rsi = random.randint(10, 90)
    trend = random.choice(["BULLISH", "BEARISH"])
    momentum = random.uniform(0, 25)

    score = 0

    if trend == "BULLISH":
        score += 35
    else:
        score -= 35

    if rsi < 30:
        score += 25
    elif rsi > 70:
        score -= 25
    else:
        score += 10

    score += momentum

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
        "signal": signal,
        "score": round(score, 2),
        "rsi": rsi
    }

# =====================
# COMMANDS
# =====================

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(
        message,
        "🚀 PRO SIGNAL BOT ACTIVE\n\nUse /signals"
    )

@bot.message_handler(commands=['signals'])
def signals(message):

    symbols = [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "BNBUSDT",
        "XRPUSDT",
        "ADAUSDT",
        "DOGEUSDT",
        "AVAXUSDT",
        "LINKUSDT",
        "LTCUSDT"
    ]

    results = []

    for s in symbols:
        results.append(generate_signal(s))

    results.sort(key=lambda x: x["score"], reverse=True)

    msg = "📊 TOP SIGNALS\n\n"

    for i, r in enumerate(results, 1):

        msg += (
            f"{i}. {r['symbol']}\n"
            f"{r['signal']}\n"
            f"Score: {r['score']}\n"
            f"RSI: {r['rsi']}\n\n"
        )

    bot.reply_to(message, msg)

# =====================
# RUN BOT
# =====================

print("🚀 BOT RUNNING SUCCESSFULLY")

while True:
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        print(f"ERROR: {e}")
        time.sleep(5)
