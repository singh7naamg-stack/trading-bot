import os
import asyncio
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Import the logic we just created in engine.py
from engine import get_top_signals

# Logging setup to see errors in Render logs
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

# --- COMMANDS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 **Trading Scanner Bot Active**\n\n"
        "I scan 50+ Binance Futures pairs for high-probability setups.\n\n"
        "Use /signals to start the scan.",
        parse_mode='Markdown'
    )

async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Send a "Processing" message so the user knows the bot is working
    status_msg = await update.message.reply_text("🔍 Scanning Binance Futures markets... Please wait.")
    
    try:
        # Call the engine to get the top 5 trades
        results = await get_top_signals()
        
        if not results:
            await status_msg.edit_text("❌ No strong signals found right now. Market is neutral.")
            return

        report = "🚀 **TOP 5 TRADING SIGNALS**\n\n"
        
        for res in results:
            report += (
                f"💎 **{res['symbol']}** ({res['dir']})\n"
                f"🎯 **Entry:** `{res['entry']:.4f}`\n"
                f"✅ **TP:** `{res['tp']:.4f}`\n"
                f"🚫 **SL:** `{res['sl']:.4f}`\n"
                f"⚖️ **Leverage:** `{res['lev']}x` (Risk 1%)\n"
                f"⭐ **Confidence:** `{res['score']}%` \n\n"
                f"----------------------------\n\n"
            )

        # Update the initial message with the final report
        await status_msg.edit_text(report, parse_mode='Markdown')

    except Exception as e:
        logging.error(f"Error in signals command: {e}")
        await status_msg.edit_text("⚠️ An error occurred while scanning. Check Render logs.")

# --- MAIN APP ---

def main():
    if not TOKEN:
        logging.error("BOT_TOKEN is missing!")
        return

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signals", signals))

    print("🚀 Bot is running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
