import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Import our Binance engine
from engine import get_top_signals

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
user_chat_id = None 

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global user_chat_id
    user_chat_id = update.effective_chat.id
    await update.message.reply_text(
        "🚀 **QuestLife Binance Bot Active**\n\n"
        "• Scanning top 50 Binance pairs every 15 mins.\n"
        "• **Auto-Alerts:** 70% score or higher.\n"
        "• **Manual signals:** Use /signals (60% score).\n\n"
        "Type /signals to check the market now.",
        parse_mode='Markdown'
    )

async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔎 Scanning Binance Futures...")
    await run_scan_and_send(context.bot, update.effective_chat.id, threshold=60)
    await msg.delete()

async def auto_scan_job(context: ContextTypes.DEFAULT_TYPE):
    if user_chat_id:
        logger.info("Running automatic 15-minute scan...")
        await run_scan_and_send(context.bot, user_chat_id, threshold=70, is_auto=True)

async def run_scan_and_send(bot, chat_id, threshold, is_auto=False):
    try:
        results = await get_top_signals()
        filtered = [s for s in results if s['score'] >= threshold]
        
        if not filtered:
            if not is_auto:
                await bot.send_message(chat_id=chat_id, text="📉 No strong signals currently match your criteria.")
            return

        header = "🚨 **HIGH-QUALITY AUTO-ALERT**" if is_auto else "🚀 **BINANCE LIVE SIGNALS**"
        report = f"{header}\n\n"
        
        for res in filtered:
            report += (
                f"💎 **{res['symbol']}** ({res['dir']})\n"
                f"🎯 **Entry:** `{res['entry']}`\n"
                f"✅ **TP:** `{res['tp']:.4f}` | 🚫 **SL:** `{res['sl']:.4f}`\n"
                f"⚖️ **Lev:** `{res['lev']}x` | ⭐ **Score:** `{res['score']}%` \n"
                f"----------------------------\n\n"
            )
        
        await bot.send_message(chat_id=chat_id, text=report, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error: {e}")

def main():
    if not TOKEN:
        logger.error("Token missing!")
        return

    # Optimized for Singapore server latency
    app = ApplicationBuilder().token(TOKEN).read_timeout(30).write_timeout(30).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signals", signals))

    # Background task every 15 minutes (900 seconds)
    app.job_queue.run_repeating(auto_scan_job, interval=900, first=10)

    logger.info("QuestLife Bot is Online (Binance/Singapore)")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
