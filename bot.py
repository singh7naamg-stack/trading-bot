import os
import requests
import asyncio
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# TOKEN (from Render ENV)
# =========================
TOKEN = os.getenv("TOKEN")

CHAT_ID = None
trade_history = {}

# =========================
# MENU
# =========================
def menu():
    return ReplyKeyboardMarkup(
        [["TRADE"], ["AUTO"]],
        resize_keyboard=True
    )

# =========================
# GET SYMBOLS
# =========================
def get_symbols():
    try:
        data = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            timeout=10
        ).json()

        symbols = []

        for x in data:
            sym = x["symbol"]

            if not sym.endswith("USDT"):
                continue

            try:
                volume = float(x["quoteVolume"])
                change = abs(float(x["priceChangePercent"]))
            except:
                continue

            if volume < 50000000:
                continue
            if change < 0.8:
                continue

            symbols.append(sym)

        return symbols[:100]

    except:
        return []

# =========================
# CANDLES
# =========================
def get_closes(symbol):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=5m&limit=80"
        data = requests.get(url, timeout=10).json()

        closes = []
        volumes = []

        for c in data:
            closes.append(float(c[4]))
            volumes.append(float(c[5]))

        return closes, volumes

    except:
        return [], []

# =========================
# EMA
# =========================
def ema(prices, period=20):
    if len(prices) < period:
        return 0

    k = 2 / (period + 1)
    e = prices[0]

    for p in prices:
        e = p * k + e * (1 - k)

    return e

# =========================
# RSI
# =========================
def rsi(prices):
    if len(prices) < 15:
        return 50

    gains, losses = [], []

    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))

    avg_gain = sum(gains[-14:]) / 14
    avg_loss = sum(losses[-14:]) / 14

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# =========================
# MOMENTUM
# =========================
def momentum(prices):
    try:
        return (prices[-1] - prices[-6]) / prices[-6] * 100
    except:
        return 0

# =========================
# SCORE ENGINE
# =========================
def score(prices):
    price = prices[-1]

    e = ema(prices)
    r = rsi(prices)
    m = momentum(prices)

    s = 50

    if price > e:
        s += 20
    else:
        s -= 20

    if 40 < r < 65:
        s += 15
    else:
        s -= 10

    if abs(m) > 0.5:
        s += 15

    return max(0, min(100, s))

# =========================
# TRADE LOGIC
# =========================
def analyze(symbol):

    closes, volumes = get_closes(symbol)

    if len(closes) < 30:
        return None

    price = closes[-1]

    sc = score(closes)

    if sc < 75:
        return None

    ema_val = ema(closes)
    r = rsi(closes)
    m = momentum(closes)

    direction = "BUY" if price > ema_val else "SELL"

    # ENTRY FILTER
    if direction == "BUY" and r > 70:
        return None
    if direction == "SELL" and r < 30:
        return None

    # STOP LOSS / TAKE PROFIT
    sl = price * (0.97 if direction == "BUY" else 1.03)
    tp = price * (1.05 if direction == "BUY" else 0.95)

    confidence = sc

    return {
        "symbol": symbol,
        "type": direction,
        "confidence": confidence,
        "entry": price,
        "sl": sl,
        "tp": tp
    }

# =========================
# SCAN MARKET
# =========================
def scan_market():
    results = []

    for sym in get_symbols():
        t = analyze(sym)
        if t:
            results.append(t)

    results.sort(key=lambda x: x["confidence"], reverse=True)

    return results[:10]

# =========================
# HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID
    CHAT_ID = update.effective_chat.id

    await update.message.reply_text(
        "🚀 ELITE SIGNAL BOT READY",
        reply_markup=menu()
    )

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text.upper()

    if text == "TRADE":

        data = scan_market()

        if not data:
            await update.message.reply_text("No strong setups right now.")
            return

        msg = "🔥 TOP 10 SIGNALS\n\n"

        for i, d in enumerate(data, 1):
            msg += (
                f"#{i} {d['symbol']} ({d['type']})\n"
                f"Confidence: {d['confidence']}%\n"
                f"Entry: {d['entry']:.6f}\n"
                f"SL: {d['sl']:.6f}\n"
                f"TP: {d['tp']:.6f}\n\n"
            )

        await update.message.reply_text(msg)

# =========================
# MAIN (NO ASYNC LOOP BUG)
# =========================
def main():

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🚀 Bot Running...")

    app.run_polling()

# =========================
# RUN
# =========================
if __name__ == "__main__":
    main()
