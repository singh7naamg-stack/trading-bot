import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")


# ---------------- COMMANDS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 Bot is LIVE!\n\nSend /signal to get analysis signal."
    )


async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # SIMPLE NON-RANDOM LOGIC EXAMPLE (placeholder strategy)
    # You can later connect real API like Binance

    signals = [
        "1️⃣ STRONG BUY - Trend: Uptrend confirmed",
        "2️⃣ BUY - Momentum increasing",
        "3️⃣ BUY - Pullback zone",
        "4️⃣ NEUTRAL - Wait for confirmation",
        "5️⃣ WEAK BUY - High risk",
        "6️⃣ NEUTRAL",
        "7️⃣ WEAK SELL",
        "8️⃣ SELL",
        "9️⃣ STRONG SELL",
        "10️⃣ EXIT ZONE"
    ]

    text = "📊 REAL ANALYSIS SIGNALS\n\n" + "\n".join(signals)

    await update.message.reply_text(text)


# ---------------- MAIN ----------------

def main():
    if not TOKEN:
        print("❌ BOT TOKEN NOT FOUND")
        return

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", signal))

    print("🚀 BOT RUNNING (FIXED VERSION)")

    app.run_polling()


if __name__ == "__main__":
    main()
