import os
import requests
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.getenv("TOKEN")
CHAT_ID = None

# =========================
# UI
# =========================
def menu():
    return ReplyKeyboardMarkup(
        [["TOP SIGNALS"], ["MARKET STATUS"]],
        resize_keyboard=True
    )

# =========================
# MARKET DATA
# =========================
def get_symbols():
    try:
        data = requests.get("https://api.binance.com/api/v3/ticker/24hr", timeout=5).json()
        symbols = []

        for x in data:
            sym = x["symbol"]

            if not sym.endswith("USDT"):
                continue

            try:
                vol = float(x["quoteVolume"])
                change = abs(float(x["priceChangePercent"]))
            except:
                continue

            if vol < 120_000_000:
                continue

            if change < 0.8:
                continue

            symbols.append(sym)

        return symbols[:40]

    except:
        return []

# =========================
# CANDLES
# =========================
def get_closes(symbol):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1m&limit=60"
        data = requests.get(url, timeout=5).json()

        return [float(c[4]) for c in data]

    except:
        return []

# =========================
# INDICATORS
# =========================
def ema(prices):
    k = 2 / (len(prices) + 1)
    e = prices[0]
    for p in prices:
        e = p * k + e * (1 - k)
    return e

def rsi(prices):
    gains, losses = [], []

    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))

    if len(gains) < 14:
        return 50

    ag = sum(gains[-14:]) / 14
    al = sum(losses[-14:]) / 14

    if al == 0:
        return 100

    rs = ag / al
    return 100 - (100 / (1 + rs))

def momentum(prices):
    return (prices[-1] - prices[-10]) / prices[-10] * 100

# =========================
# NEWS SENTIMENT (NEW LAYER)
# =========================
def news_sentiment():
    try:
        url = "https://cryptopanic.com/api/v1/posts/?auth_token=demo&public=true"
        data = requests.get(url, timeout=5).json()

        posts = data.get("results", [])[:10]

        score = 0

        for p in posts:
            title = p.get("title", "").lower()

            if any(w in title for w in ["crash", "hack", "fall", "drop", "lawsuit"]):
                score -= 2

            if any(w in title for w in ["bull", "rise", "surge", "breakout", "adoption"]):
                score += 2

        return score

    except:
        return 0

# =========================
# MARKET SCORE ENGINE
# =========================
def score(prices):
    price = prices[-1]
    e = ema(prices)
    r = rsi(prices)
    m = momentum(prices)
    news = news_sentiment()

    score = 50

    # trend
    if price > e:
        score += 20
    else:
        score -= 20

    # RSI zone
    if 40 < r < 60:
        score += 15
    elif r < 30 or r > 70:
        score -= 15

    # momentum
    if abs(m) > 1:
        score += 15
    else:
        score -= 10

    # news filter
    score += news * 3

    return max(0, min(100, score))

# =========================
# SIGNAL
# =========================
def analyze(symbol):
    prices = get_closes(symbol)

    if len(prices) < 30:
        return None

    sc = score(prices)

    if sc < 78:
        return None

    price = prices[-1]
    e = ema(prices)
    r = rsi(prices)

    direction = None

    if price > e and r < 65:
        direction = "BUY"
    elif price < e and r > 35:
        direction = "SELL"

    if not direction:
        return None

    return {
        "symbol": symbol,
        "type": direction,
        "score": sc,
        "entry": price,
        "sl": price * (0.97 if direction == "BUY" else 1.03),
        "tp": price * (1.05 if direction == "BUY" else 0.95)
    }

# =========================
# SCAN
# =========================
def scan():
    results = []

    for sym in get_symbols():
        t = analyze(sym)
        if t:
            results.append(t)

    results.sort(key=lambda x: x["score"], reverse=True)

    return results[:10]

# =========================
# TELEGRAM
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID
    CHAT_ID = update.message.chat_id

    await update.message.reply_text("🚀 PRO SIGNAL SYSTEM V2 ACTIVE", reply_markup=menu())

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.upper()

    if text == "TOP SIGNALS":

        data = scan()

        if not data:
            await update.message.reply_text("No strong market setups now.")
            return

        msg = "🔥 TOP 10 HIGH PROBABILITY SIGNALS\n\n"

        for i, d in enumerate(data, 1):
            msg += f"{i}. {d['symbol']} ({d['type']})\n"
            msg += f"Score: {d['score']}\n"
            msg += f"Entry: {d['entry']:.4f}\n"
            msg += f"SL: {d['sl']:.4f} | TP: {d['tp']:.4f}\n\n"

        await update.message.reply_text(msg)

# =========================
# MAIN
# =========================
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🚀 PRO SIGNAL BOT V2 RUNNING")
    app.run_polling()

if __name__ == "__main__":
    main()
