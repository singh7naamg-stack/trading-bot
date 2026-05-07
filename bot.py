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

# =====================================================
# TOKEN
# =====================================================
TOKEN = os.getenv("TOKEN")

# =====================================================
# GLOBALS
# =====================================================
CHAT_ID = None
trade_history = {}
win_count = 0
loss_count = 0

# =====================================================
# MENU
# =====================================================
def menu():
    return ReplyKeyboardMarkup(
        [["TRADE"], ["AUTO"], ["WIN"], ["LOSS"]],
        resize_keyboard=True
    )

# =====================================================
# GET SYMBOLS
# =====================================================
def get_symbols():

    try:
        data = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            timeout=10
        ).json()

        symbols = []

        for x in data:

            symbol = x["symbol"]

            if not symbol.endswith("USDT"):
                continue

            try:
                volume = float(x["quoteVolume"])
                change = abs(float(x["priceChangePercent"]))
            except:
                continue

            # Strong liquidity filter
            if volume < 50000000:
                continue

            # Avoid dead coins
            if change < 0.8:
                continue

            symbols.append(symbol)

        return symbols[:120]

    except:
        return []

# =====================================================
# GET CANDLES
# =====================================================
def get_klines(symbol, interval="5m", limit=120):

    try:
        url = (
            f"https://api.binance.com/api/v3/klines"
            f"?symbol={symbol}&interval={interval}&limit={limit}"
        )

        data = requests.get(url, timeout=10).json()

        closes = []
        volumes = []

        for candle in data:
            closes.append(float(candle[4]))
            volumes.append(float(candle[5]))

        return closes, volumes

    except:
        return [], []

# =====================================================
# EMA
# =====================================================
def ema(prices, period=20):

    if len(prices) < period:
        return 0

    k = 2 / (period + 1)

    e = prices[0]

    for p in prices:
        e = p * k + e * (1 - k)

    return e

# =====================================================
# RSI
# =====================================================
def rsi(prices, period=14):

    if len(prices) < period + 1:
        return 50

    gains = []
    losses = []

    for i in range(1, len(prices)):

        diff = prices[i] - prices[i - 1]

        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))

# =====================================================
# MOMENTUM
# =====================================================
def momentum(prices):

    try:
        return (
            (prices[-1] - prices[-8])
            / prices[-8]
        ) * 100

    except:
        return 0

# =====================================================
# ATR (VOLATILITY)
# =====================================================
def atr(prices):

    if len(prices) < 15:
        return 0

    diffs = []

    for i in range(1, len(prices)):
        diffs.append(abs(prices[i] - prices[i - 1]))

    return sum(diffs[-14:]) / 14

# =====================================================
# VOLUME SCORE
# =====================================================
def volume_strength(volumes):

    try:

        current = volumes[-1]
        average = sum(volumes[-20:]) / 20

        if average == 0:
            return 0

        return (current / average) * 100

    except:
        return 0

# =====================================================
# HIGHER TIMEFRAME TREND
# =====================================================
def trend_confirmation(symbol):

    closes_15m, _ = get_klines(symbol, "15m", 60)

    if len(closes_15m) < 30:
        return None

    price = closes_15m[-1]

    ema_15 = ema(closes_15m, 20)

    if price > ema_15:
        return "BUY"

    if price < ema_15:
        return "SELL"

    return None

# =====================================================
# SAFE LEVERAGE ENGINE
# =====================================================
def leverage_and_risk(volatility, symbol):

    leverage = "5x"
    risk = "MEDIUM"

    majors = [
        "BTCUSDT",
        "ETHUSDT",
        "BNBUSDT",
        "SOLUSDT"
    ]

    # LOW VOLATILITY
    if volatility < 1:

        if symbol in majors:
            leverage = "10x"
            risk = "LOW"
        else:
            leverage = "7x"
            risk = "LOW"

    # MEDIUM
    elif volatility < 3:
        leverage = "5x"
        risk = "MEDIUM"

    # HIGH
    else:
        leverage = "3x"
        risk = "HIGH"

    return leverage, risk

# =====================================================
# SIGNAL ENGINE
# =====================================================
def analyze(symbol):

    closes, volumes = get_klines(symbol, "5m", 120)

    if len(closes) < 50:
        return None

    try:

        # -------------------------------------------------
        # MAIN VALUES
        # -------------------------------------------------
        price = closes[-1]

        ema_fast = ema(closes, 20)
        ema_slow = ema(closes, 50)

        r = rsi(closes)
        m = momentum(closes)
        volatility = atr(closes)

        volume_score = volume_strength(volumes)

        trend = trend_confirmation(symbol)

        # -------------------------------------------------
        # BASE SCORE
        # -------------------------------------------------
        score = 50

        # TREND
        if ema_fast > ema_slow:
            score += 20
            direction = "BUY"
        else:
            score += 20
            direction = "SELL"

        # HIGHER TIMEFRAME CONFIRMATION
        if trend == direction:
            score += 20
        else:
            score -= 25

        # RSI QUALITY
        if direction == "BUY" and 45 < r < 68:
            score += 12

        elif direction == "SELL" and 32 < r < 55:
            score += 12

        else:
            score -= 10

        # MOMENTUM
        if abs(m) > 0.7:
            score += 15
        else:
            score -= 15

        # VOLUME
        if volume_score > 130:
            score += 18

        elif volume_score > 110:
            score += 10

        else:
            score -= 10

        # SIDEWAYS FILTER
        if volatility < 0.15:
            return None

        # -------------------------------------------------
        # FINAL FILTER
        # -------------------------------------------------
        confidence = max(0, min(100, score))

        # ONLY ELITE SIGNALS
        if confidence < 82:
            return None

        # -------------------------------------------------
        # ATR STOP LOSS
        # -------------------------------------------------
        stop_distance = volatility * 1.8
        target_distance = volatility * 3

        if direction == "BUY":

            sl = price - stop_distance
            tp = price + target_distance

        else:

            sl = price + stop_distance
            tp = price - target_distance

        # -------------------------------------------------
        # LEVERAGE
        # -------------------------------------------------
        lev, risk = leverage_and_risk(volatility, symbol)

        # -------------------------------------------------
        # GRADE
        # -------------------------------------------------
        if confidence >= 92:
            grade = "A+"

        elif confidence >= 88:
            grade = "A"

        else:
            grade = "B+"

        return {
            "symbol": symbol,
            "type": direction,
            "confidence": confidence,
            "grade": grade,
            "risk": risk,
            "leverage": lev,
            "entry": price,
            "sl": sl,
            "tp": tp,
            "volume": int(volume_score),
            "momentum": round(m, 2),
        }

    except:
        return None

# =====================================================
# MARKET SCANNER
# =====================================================
def scan_market():

    results = []

    symbols = get_symbols()

    for symbol in symbols:

        signal = analyze(symbol)

        if signal:
            results.append(signal)

    # BEST FIRST
    results.sort(
        key=lambda x: (
            x["confidence"],
            x["volume"],
            abs(x["momentum"])
        ),
        reverse=True
    )

    return results[:10]

# =====================================================
# REAL TIME ALERT LOOP
# =====================================================
async def alert_loop(app):

    global CHAT_ID

    while True:

        try:

            signals = scan_market()

            for s in signals:

                symbol = s["symbol"]

                # Avoid spam
                if symbol in trade_history:
                    continue

                msg = (
                    f"🚨 ELITE FUTURES SIGNAL\n\n"
                    f"#{signals.index(s)+1} {symbol}\n\n"
                    f"Direction: {s['type']}\n"
                    f"Grade: {s['grade']}\n"
                    f"Confidence: {s['confidence']}%\n"
                    f"Risk: {s['risk']}\n"
                    f"Leverage: {s['leverage']}\n\n"
                    f"Entry: {s['entry']:.6f}\n"
                    f"SL: {s['sl']:.6f}\n"
                    f"TP: {s['tp']:.6f}\n\n"
                    f"Volume Strength: {s['volume']}%"
                )

                if CHAT_ID:
                    await app.bot.send_message(
                        chat_id=CHAT_ID,
                        text=msg
                    )

                trade_history[symbol] = True

            await asyncio.sleep(120)

        except:
            await asyncio.sleep(30)

# =====================================================
# START COMMAND
# =====================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    global CHAT_ID

    CHAT_ID = update.effective_chat.id

    await update.message.reply_text(
        "🚀 ELITE FUTURES AI BOT ONLINE\n"
        "Top Ranked Institutional Signals",
        reply_markup=menu()
    )

# =====================================================
# MESSAGE HANDLER
# =====================================================
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):

    global win_count
    global loss_count

    text = update.message.text.upper()

    # =================================================
    # TRADE
    # =================================================
    if text == "TRADE":

        signals = scan_market()

        if not signals:

            await update.message.reply_text(
                "❌ No elite setups found now."
            )
            return

        msg = "🔥 TOP 10 ELITE FUTURES SETUPS\n\n"

        for i, s in enumerate(signals, 1):

            msg += (
                f"#{i} {s['symbol']} ({s['type']})\n"
                f"Grade: {s['grade']}\n"
                f"Confidence: {s['confidence']}%\n"
                f"Risk: {s['risk']}\n"
                f"Leverage: {s['leverage']}\n"
                f"Entry: {s['entry']:.6f}\n"
                f"SL: {s['sl']:.6f}\n"
                f"TP: {s['tp']:.6f}\n\n"
            )

        await update.message.reply_text(msg)
        return

    # =================================================
    # WIN
    # =================================================
    if text == "WIN":

        win_count += 1

        total = win_count + loss_count

        winrate = (
            (win_count / total) * 100
            if total > 0 else 0
        )

        await update.message.reply_text(
            f"✅ WIN RECORDED\n\n"
            f"Wins: {win_count}\n"
            f"Losses: {loss_count}\n"
            f"Win Rate: {winrate:.1f}%"
        )

        return

    # =================================================
    # LOSS
    # =================================================
    if text == "LOSS":

        loss_count += 1

        total = win_count + loss_count

        winrate = (
            (win_count / total) * 100
            if total > 0 else 0
        )

        await update.message.reply_text(
            f"❌ LOSS RECORDED\n\n"
            f"Wins: {win_count}\n"
            f"Losses: {loss_count}\n"
            f"Win Rate: {winrate:.1f}%"
        )

        return

    # =================================================
    # AUTO
    # =================================================
    if text == "AUTO":

        await update.message.reply_text(
            "🤖 AUTO ELITE SIGNAL MODE ACTIVE"
        )

        return

# =====================================================
# MAIN
# =====================================================
async def main():

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle
        )
    )

    # START ALERT LOOP
    asyncio.create_task(alert_loop(app))

    print("🚀 Elite Futures Bot Running...")

    await app.run_polling()

# =====================================================
# RUN
# =====================================================
if __name__ == "__main__":
    asyncio.run(main())
