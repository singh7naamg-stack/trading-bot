import os
import asyncio
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Import the engine
from engine import get_top_signals

# Professional logging to catch the "silent" crashes
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 **QuestLife Trading Bot Active**\n\n"
        "Scanning top 50 Bybit Futures pairs for setups.\n"
        "Use /signals to start.",
        parse_mode='Markdown'
    )

async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("🔍 Connecting to Bybit API...")
    
    # Retry logic: If it fails, it tries up to 3 times before giving up
    for attempt in range(3):
        try:
            await status_msg.edit_text(f"🔍 Scanning markets... (Attempt {attempt + 1}/3)")
            results = await get_top_signals()
            
            if not results:
                await status_msg.edit_text("⚠️ No strong signals found. Markets are sideways.")
                return

            report = "🚀 **TOP 10 LIVE SIGNALS**\n\n"
            for res in results:
                report += (
                    f"💎 **{res['symbol']}** ({res['dir']})\n"
                    f"🎯 **Entry:** `{res['entry']}`\n"
                    f"✅ **TP:** `{res['tp']:.4f}`\n"
                    f"🚫 **SL:** `{res['sl']:.4f}`\n"
                    f"⚖️ **Leverage:** `{res['lev']}x` (Risk 1%)\n"
                    f"⭐ **Score:** `{res['score']}%` \n\n"
                    f"----------------------------\n\n"
                )

            await status_msg.edit_text(report, parse_mode='Markdown')
            return # Success! Exit the retry loop

        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                await asyncio.sleep(2) # Wait 2 seconds before retrying
            else:
                await status_msg.edit_text("❌ Connection error. Please try /signals again in a minute.")

def main():
    if not TOKEN:
        logger.error("BOT_TOKEN is missing!")
        return

    app = ApplicationBuilder().token(TOKEN).read_timeout(30).write_timeout(30).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signals", signals))

    logger.info("Bot is starting deployment...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
