# ============================================================
#  QuestLife Signal Bot — main.py  v2.0
#  Multi-user | Persistent Subscribers | Rate Limited
# ============================================================

import os
import json
import logging
import asyncio
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from engine import get_top_signals

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
load_dotenv()

TOKEN             = os.getenv("BOT_TOKEN")
SUBSCRIBERS_FILE  = "subscribers.json"
COOLDOWN_SECONDS  = 120   # 2 min between manual /signals calls
AUTO_THRESHOLD    = 75    # Score % for auto-alerts
MANUAL_THRESHOLD  = 60    # Score % for manual /signals

# ─── Subscriber Persistence ───────────────────────────────────────────────────

def load_subscribers() -> set:
    if os.path.exists(SUBSCRIBERS_FILE):
        try:
            with open(SUBSCRIBERS_FILE) as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()

def save_subscribers(subs: set):
    try:
        with open(SUBSCRIBERS_FILE, "w") as f:
            json.dump(list(subs), f)
    except Exception as e:
        logger.error(f"Could not save subscribers: {e}")

subscribers     = load_subscribers()
last_scan_time  = {}   # chat_id → event loop time (rate limiting)


# ─── Message Formatting ───────────────────────────────────────────────────────

def fmt_price(price: float) -> str:
    """Format price with appropriate decimal places for any coin."""
    if price >= 1000:  return f"{price:,.2f}"
    if price >= 1:     return f"{price:.4f}"
    if price >= 0.01:  return f"{price:.5f}"
    return f"{price:.8f}"

def format_signal(res: dict, rank: int) -> str:
    medal = {0: "🥇", 1: "🥈", 2: "🥉"}.get(rank, "💎")
    return (
        f"{medal} **{res['symbol']}**  {res['dir']}\n"
        f"🎯 Entry : `{fmt_price(res['entry'])}`\n"
        f"✅ TP    : `{fmt_price(res['tp'])}` | 🚫 SL: `{fmt_price(res['sl'])}`\n"
        f"⚖️ Lev   : `{res['lev']}x`  |  📊 R:R `1:{res['rr']}`\n"
        f"⭐ Score : `{res['score']}%`  RSI `{res['rsi']}`  ADX `{res['adx']}`\n"
        f"💡 _{res['reasons']}_\n"
        f"{'─' * 32}\n\n"
    )

def build_report(results: list, is_auto: bool, threshold: int) -> str:
    header = "🚨 *HIGH\\-QUALITY AUTO\\-ALERT*" if is_auto else "🚀 *BINANCE LIVE SIGNALS*"
    ts     = datetime.now(timezone.utc).strftime('%H:%M UTC')
    body   = f"{header}  `{ts}`\n"
    body  += f"📋 *{len(results)} signal(s) — score ≥ {threshold}%*\n\n"
    for i, r in enumerate(results):
        body += format_signal(r, i)
    body += "⚠️ _Signals are educational only\\. Always manage your own risk\\._"
    return body


# ─── Core Scan & Send ─────────────────────────────────────────────────────────

async def scan_and_send(bot, chat_id: int, threshold: int, is_auto: bool = False):
    try:
        results  = await get_top_signals()
        filtered = [s for s in results if s['score'] >= threshold]

        if not filtered:
            if not is_auto:
                await bot.send_message(
                    chat_id=chat_id,
                    text="📉 No signals meet the criteria right now.\nMarket may be ranging — wait for a setup.",
                )
            return

        report = build_report(filtered, is_auto=is_auto, threshold=threshold)

        # Telegram max message = 4096 chars — chunk if needed
        if len(report) <= 4096:
            await bot.send_message(chat_id=chat_id, text=report, parse_mode='MarkdownV2')
        else:
            # Send 3 signals per message
            for chunk in [filtered[i:i+3] for i in range(0, len(filtered), 3)]:
                chunk_report = build_report(chunk, is_auto=is_auto, threshold=threshold)
                await bot.send_message(chat_id=chat_id, text=chunk_report, parse_mode='MarkdownV2')
                await asyncio.sleep(0.5)

    except Exception as e:
        logger.error(f"scan_and_send error [{chat_id}]: {e}")
        if not is_auto:
            await bot.send_message(
                chat_id=chat_id,
                text="⚠️ Scan encountered an error. Please try again in a moment."
            )


# ─── Command Handlers ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subscribers.add(chat_id)
    save_subscribers(subscribers)

    await update.message.reply_text(
        "🚀 *QuestLife Signal Bot v2\\.0 — Active*\n\n"
        "📡 *What this bot does:*\n"
        "• Scans top 30 Binance Futures pairs by 24H volume\n"
        "• Dual timeframe analysis: 1H trend \\+ 4H confirmation\n"
        "• 5\\-pillar scoring: Trend · MTF · Momentum · ADX · Volume\n"
        "• Auto\\-alerts every 15min when score ≥ 75%\n\n"
        "📋 *Commands:*\n"
        "/signals — Manual scan \\(score ≥ 60%\\)\n"
        "/top — Best single signal right now\n"
        "/status — Bot health \\& subscriber count\n"
        "/stop — Unsubscribe from auto\\-alerts\n\n"
        "⚠️ _Signals are for education only\\. Always do your own research\\._",
        parse_mode='MarkdownV2'
    )


async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    now     = asyncio.get_event_loop().time()

    # Rate limiting — prevent API spam
    if chat_id in last_scan_time:
        elapsed   = now - last_scan_time[chat_id]
        remaining = int(COOLDOWN_SECONDS - elapsed)
        if elapsed < COOLDOWN_SECONDS:
            await update.message.reply_text(
                f"⏳ Please wait *{remaining}s* before scanning again\\.",
                parse_mode='MarkdownV2'
            )
            return

    last_scan_time[chat_id] = now
    msg = await update.message.reply_text("🔎 Running 5\\-pillar analysis on top 30 pairs\\.\\.\\.", parse_mode='MarkdownV2')
    await scan_and_send(context.bot, chat_id, threshold=MANUAL_THRESHOLD)
    try:
        await msg.delete()
    except Exception:
        pass


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Return the single highest-scoring signal."""
    msg = await update.message.reply_text("🏆 Finding the best signal right now\\.\\.\\.", parse_mode='MarkdownV2')
    try:
        results = await get_top_signals()
        if not results:
            await update.message.reply_text("📉 No qualifying signals found right now\\. Try again later\\.", parse_mode='MarkdownV2')
        else:
            best   = results[0]
            report = "🏆 *TOP SIGNAL RIGHT NOW*\n\n" + format_signal(best, 0)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=report,
                parse_mode='MarkdownV2'
            )
    except Exception as e:
        logger.error(f"/top error: {e}")
        await update.message.reply_text("⚠️ Error fetching top signal\\. Try again\\.", parse_mode='MarkdownV2')
    finally:
        try:
            await msg.delete()
        except Exception:
            pass


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subscribers.discard(chat_id)
    save_subscribers(subscribers)
    await update.message.reply_text(
        "🔕 Unsubscribed from auto\\-alerts\\.\nUse /start to resubscribe anytime\\.",
        parse_mode='MarkdownV2'
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"✅ *Bot Status: Online*\n\n"
        f"👥 Subscribers    : `{len(subscribers)}`\n"
        f"⏱ Auto\\-scan      : every `15 minutes`\n"
        f"🎯 Auto threshold : `{AUTO_THRESHOLD}%`\n"
        f"📊 Manual threshold: `{MANUAL_THRESHOLD}%`\n"
        f"🔢 Pairs scanned  : Top `30` by 24H volume\n"
        f"📈 Timeframes     : `1H \\+ 4H`\n"
        f"📐 Indicators     : EMA20/50, RSI14, ATR14, ADX14, Volume",
        parse_mode='MarkdownV2'
    )


# ─── Auto Scan Job ────────────────────────────────────────────────────────────

async def auto_scan_job(context: ContextTypes.DEFAULT_TYPE):
    if not subscribers:
        logger.info("Auto-scan: no subscribers, skipping.")
        return

    logger.info(f"Auto-scan running for {len(subscribers)} subscriber(s)...")

    for chat_id in list(subscribers):
        await scan_and_send(context.bot, chat_id, threshold=AUTO_THRESHOLD, is_auto=True)
        await asyncio.sleep(1)  # Stagger sends so Telegram doesn't rate-limit


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not TOKEN:
        logger.error("BOT_TOKEN is missing! Set it in your .env or Render environment variables.")
        return

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("signals", signals))
    app.add_handler(CommandHandler("top",     top))
    app.add_handler(CommandHandler("stop",    stop))
    app.add_handler(CommandHandler("status",  status))

    # Auto-scan every 15 minutes, first run after 30s
    app.job_queue.run_repeating(auto_scan_job, interval=900, first=30)

    logger.info(f"QuestLife Bot v2.0 Online | {len(subscribers)} subscriber(s) loaded")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
