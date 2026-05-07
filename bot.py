import os
import asyncio
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes
)

# ----------------------------
# LOAD ENV VARIABLES
# ----------------------------
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise ValueError("BOT_TOKEN is missing. Add it in Render Environment Variables")

# ----------------------------
# BASIC COMMANDS
# ----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 Bot is running successfully!\nSend /signals to test."
    )

async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Placeholder for your real logic later
    msg = (
        "📊 TOP 10 SIGNALS (Demo Structure)\n\n"
        "1️⃣ BTC/USD - STRONG BUY\n"
        "2️⃣ ETH/USD - BUY\n"
        "3️⃣ GOLD - WEAK BUY\n"
        "4️⃣ NASDAQ - NEUTRAL\n"
        "5️⃣ EUR/USD - SELL\n"
        "6️⃣ GBP/USD - STRONG SELL\n"
        "7️⃣ SOL/USD - BUY\n"
        "8️⃣ XRP/USD - NEUTRAL\n"
        "9️⃣ DXY - BUY\n"
        "🔟 OIL - SELL\n\n"
        "⚠️ Note: Logic engine will be upgraded next."
    )

    await update.message.reply_text(msg)

# ----------------------------
# MAIN APP
# ----------------------------
def main():
    print("🚀 REAL SIGNAL BOT RUNNING")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signals", signals))

    # IMPORTANT: Render-safe polling
    app.run_polling(drop_pending_updates=True)

# ----------------------------
# START
# ----------------------------
if __name__ == "__main__":
    main()
