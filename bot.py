import os
import requests
import asyncio
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# =========================
# CONFIG
# =========================
TOKEN = os.getenv("TOKEN")
CHAT_ID = None

# =========================
# TRACKING SYSTEM
# =========================
trade_history = {}
win_count = 0
loss_count = 0

# =========================
# UI MENU
# =========================
def menu():
    return ReplyKeyboardMarkup(
        [["TRADE"], ["WIN", "LOSS"]],
        resize_keyboard=True
    )

# =========================
# GET SYMBOLS (BINANCE SCAN)
# =========================
def get_symbols():
    try:
        data = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            timeout=5
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

            if volume < 60_000_000:
                continue

            if change < 0.3:
                continue

            symbols.append(sym)

        return symbols[:120]

    except:
        return []

# =========================
# CANDLES
# =========================
def get_closes(symbol):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1m&limit=40"
        data = requests.get(url, timeout=5).json()

        if not isinstance(data, list) or len(data) < 25:
            return []

        return [float(c[4]) for c in data]

    except:
        return []

# =========================
# RSI
# =========================
def rsi(prices):
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
# EMA
# =========================
def ema(prices):
    k = 2 / (len(prices) + 1)
    e = prices[0]

    for p in prices:
        e = p * k + e * (1 - k)

    return e

# =========================
# MOMENTUM
# =========================
def momentum(prices):
    return (prices[-1] - prices[-6]) / prices[-6] * 100

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
        s += 18
    else:
        s -= 18

    if 35 < r < 65:
        s += 12
    else:
        s -= 15

    if abs(m) > 0.4:
        s += 12
    else:
        s -= 8

    return max(0, min(100, s))

# =========================
# CONFIDENCE
# =========================
def confidence(prices, sc):
    price = prices[-1]
    e = ema(prices)
    r = rsi(prices)
    m = momentum(prices)

    c = 50

    if price > e:
        c += 20
    else:
        c -= 20

    if 40 < r < 60:
        c += 15
    else:
        c -= 10

    if abs(m) > 0.5:
        c += 15

    if sc > 80:
        c += 10

    return max(0, min(100, c))

# =========================
# TRADE LOGIC
# =========================
def trade(symbol):
    prices = get_closes(symbol)

    if not prices:
        return None

    price = prices[-1]

    sc = score(prices)
    conf = confidence(prices, sc)

    if sc < 70:
        return None

    e = ema(prices)
    r = rsi(prices)

    if price > e and r < 60:
        return {
            "symbol": symbol,
            "type": "BUY",
            "score": sc,
            "confidence": conf,
            "entry": price,
            "sl": price * 0.97,
            "tp": price * 1.05
        }

    if price < e and r > 40:
        return {
            "symbol": symbol,
            "type": "SELL",
            "score": sc,
            "confidence": conf,
            "entry": price,
            "sl": price * 1.03,
            "tp": price * 0.95
        }

    return None

# =========================
# SCAN MARKET
# =========================
def scan_market():
    results = []

    for sym in get_symbols():
        t = trade(sym)
        if t:
            results.append(t)

    results.sort(key=lambda x: x["confidence"], reverse=True)

    return results[:10]

# =========================
# ALERT LOOP (BACKGROUND)
# =========================
async def alert_loop(app):
    while True:
        data = scan_market()

        for d in data:
            if d["confidence"] < 80:
                continue

            symbol = d["symbol"]

            if symbol in trade_history:
                continue

            msg = f"""
🚨 HIGH CONFIDENCE SIGNAL

{symbol}
Type: {d['type']}
Confidence: {d['confidence']}%
Score: {d['score']}
Entry: {d['entry']:.4f}
SL: {d['sl']:.4f}
TP: {d['tp']:.4f}
"""

            if CHAT_ID:
                await app.bot.send_message(chat_id=CHAT_ID, text=msg)

            trade_history[symbol] = "PENDING"

        await asyncio.sleep(60)

# =========================
# HANDLER
# =========================
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global win_count, loss_count

    text = update.message.text.upper()

    if text == "TRADE":
        data = scan_market()

        msg = "🔥 TOP 10 HIGH CONFIDENCE SETUPS\n\n"

        for i, d in enumerate(data, 1):
            msg += f"#{i} {d['symbol']} ({d['type']})\n"
            msg += f"Conf: {d['confidence']}%\n"
            msg += f"Entry: {d['entry']:.4f}\n\n"

        await update.message.reply_text(msg)
        return

    if text == "WIN":
        win_count += 1
        await update.message.reply_text(f"✅ Win recorded | Wins: {win_count}")
        return

    if text == "LOSS":
        loss_count += 1
        await update.message.reply_text(f"❌ Loss recorded | Losses: {loss_count}")
        return

# =========================
# START COMMAND
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID
    CHAT_ID = update.message.chat_id

    await update.message.reply_text(
        "🚀 PRO TRADING BOT RUNNING ON CLOUD",
        reply_markup=menu()
    )

# =========================
# APP SETUP (FIXED RENDER VERSION)
# =========================
async def post_init(app):
    asyncio.create_task(alert_loop(app))

app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

print("Bot Running on Cloud...")

app.run_polling()
