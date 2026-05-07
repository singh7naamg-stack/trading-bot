import os
import asyncio
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Import the engine
from engine import get_top_signals

# Professional logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
# This variable will store your chat ID automatically after you type /start
user_chat_id = None 

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global user_chat_id
    user_chat_id = update.effective_chat.id
    await update.message.reply_text(
        "🚀 **QuestLife Auto-Pilot Active**\n\n"
        "I am now scanning the markets every 15 minutes.\n"
        "I will alert you ONLY if I find a signal with a score of **70% or higher**.\n\n"
        "Use /signals to force a manual scan at any time.",
        parse_mode='Markdown'
    )

async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("🔍 Scanning markets manually...")
    await run_scan_and_send(context.bot, update.effective_chat.id, threshold=60)
    await status_msg.delete()

async def auto_scan_job(context: ContextTypes.DEFAULT_TYPE):
    """Background task that runs every 15 minutes"""
    if user_chat_id:
        logger.info("Running automatic background scan...")
        # Only notify for VERY strong signals (70+)
        await run_scan_and_send(context.bot, user_chat_id, threshold=70, is_auto=True)

async def run_scan_and_send(bot, chat_id, threshold, is_auto=False):
    try:
        results = await get_top_signals()
        filtered = [s for s in results if s['score'] >= threshold]
        
        if not filtered:
            if not is_auto:
                await bot.send_message(chat_id=chat_id, text="⚠️ No strong signals found right now.")
            return

        report = f"{'🚨 **HIGH-QUALITY AUTO-ALERT**' if is_auto else '🚀 **LIVE SIGNALS**'}\n\n"
        for res in filtered:
            report += (
                f"💎 **{res['symbol']}** ({res['dir']})\n"
                f"🎯 **Entry:** `{res['entry']}`\n"
                f"✅ **TP:** `{res['tp']:.4f}` | 🚫 **SL:** `{res['sl']:.4f}`\n"
                f"⚖️ **Lev:** `{res['lev']}x` | ⭐ **Score:** `{res['score']}%` \n\n"
                f"----------------------------\n\n"
            )
        
        await bot.send_message(chat_id=chat_id, text=report, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Scan error: {e}")

def main():
    if not TOKEN:
        logger.error("BOT_TOKEN is missing!")
        return

    # Updated with 30s timeouts for stability
    app = ApplicationBuilder().token(TOKEN).read_timeout(30).write_timeout(30).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signals", signals))

    # This sets up the 15-minute background timer
    job_queue = app.job_queue
    job_queue.run_repeating(auto_scan_job, interval=900, first=10)

    logger.info("QuestLife Bot starting in Singapore...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
