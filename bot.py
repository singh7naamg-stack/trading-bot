# ============================================================
#  QuestLife Signal Bot — main.py  v4.0
#  Multi-user | Market Briefing | News-aware Signals
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

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
load_dotenv()

TOKEN            = os.getenv("BOT_TOKEN")
SUBSCRIBERS_FILE = "subscribers.json"
COOLDOWN_SECONDS = 120
AUTO_THRESHOLD   = 75
MANUAL_THRESHOLD = 60


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
        logger.error(f"Subscriber save failed: {e}")

subscribers    = load_subscribers()
last_scan_time = {}
last_ctx       = None   # Cache last market context for /briefing


# ─── Formatting ───────────────────────────────────────────────────────────────

def fmt_price(p: float) -> str:
    if p >= 1000:  return f"{p:,.2f}"
    if p >= 1:     return f"{p:.4f}"
    if p >= 0.01:  return f"{p:.5f}"
    return f"{p:.8f}"

def format_signal(res: dict, rank: int) -> str:
    medal   = {0: "🥇", 1: "🥈", 2: "🥉"}.get(rank, "💎")
    fr_str  = f"{res['funding_rate']:+.4f}%" if res.get("funding_rate") is not None else "N/A"
    vol_str = f"${res.get('vol_24h_m', '?')}M"
    news_icon = {"POSITIVE": "📰✅", "NEGATIVE": "📰❌", "NEUTRAL": ""}.get(res.get("news", "NEUTRAL"), "")

    out = (
        f"{medal} *{res['symbol']}* {res['dir']} {news_icon}\n"
        f"🎯 Entry : `{fmt_price(res['entry'])}`\n"
        f"✅ TP    : `{fmt_price(res['tp'])}` | 🚫 SL: `{fmt_price(res['sl'])}`\n"
        f"⚖️ Lev   : `{res['lev']}x` | 📊 R:R `1:{res['rr']}`\n"
        f"⭐ Score : `{res['score']}%` | RSI `{res['rsi']}` | ADX `{res['adx']}`\n"
        f"💰 FR    : `{fr_str}` | 📈 Vol: `{vol_str}`\n"
        f"💡 _{res['reasons'][:80]}_\n"
    )
    if res.get("news_headline"):
        out += f"📰 _{res['news_headline'][:70]}_\n"
    out += "─" * 30 + "\n\n"
    return out

def build_report(results: list, ctx, is_auto: bool, threshold: int) -> str:
    header = "🚨 *HIGH\\-QUALITY AUTO\\-ALERT*" if is_auto else "🚀 *BINANCE LIVE SIGNALS*"
    ts     = datetime.now(timezone.utc).strftime("%H:%M UTC")
    body   = f"{header}  `{ts}`\n"

    # Market context header
    if ctx:
        body += f"\n{ctx.summary()}\n"
        if ctx.macro_event_today:
            body += f"\n🚨 *MACRO EVENT: {ctx.macro_event_name}* — volatility risk, reduce size\\!\n"

    body += f"\n📋 *{len(results)} signal(s) — score ≥ {threshold}%*\n\n"
    for i, r in enumerate(results):
        body += format_signal(r, i)
    body += "⚠️ _Signals are educational only\\. Always manage your own risk\\._"
    return body


# ─── Core Scan & Send ─────────────────────────────────────────────────────────

async def scan_and_send(bot, chat_id: int, threshold: int, is_auto: bool = False):
    global last_ctx
    try:
        results, ctx = await get_top_signals()
        last_ctx = ctx   # Cache for /briefing command
        filtered = [s for s in results if s["score"] >= threshold]

        if not filtered:
            if not is_auto:
                macro_warn = f"\n\n{ctx.macro_warning()}" if ctx and ctx.macro_event_today else ""
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"📉 No signals meet criteria right now\\.{macro_warn}\nMarket may be ranging — patience is key\\.",
                    parse_mode="MarkdownV2"
                )
            return

        report = build_report(filtered, ctx, is_auto=is_auto, threshold=threshold)
        chunks = [filtered[i:i+3] for i in range(0, len(filtered), 3)] if len(report) > 4000 else [filtered]
        for chunk in chunks:
            msg = build_report(chunk, ctx if chunk is filtered else None, is_auto=is_auto, threshold=threshold)
            try:
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="MarkdownV2")
            except Exception:
                # Fallback: strip markdown if formatting fails
                await bot.send_message(chat_id=chat_id, text=msg.replace("*", "").replace("`", "").replace("_", "").replace("\\", ""))
            await asyncio.sleep(0.5)

    except Exception as e:
        logger.error(f"scan_and_send error [{chat_id}]: {e}")
        if not is_auto:
            await bot.send_message(chat_id=chat_id, text="⚠️ Scan error\\. Try again in a moment\\.", parse_mode="MarkdownV2")


# ─── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subscribers.add(chat_id)
    save_subscribers(subscribers)
    await update.message.reply_text(
        "🚀 *QuestLife Signal Bot v4\\.0 — Active*\n\n"
        "📡 *Intelligence layers:*\n"
        "• BTC 4H trend gate \\(bears block all LONG signals\\)\n"
        "• FOMC/CPI/NFP macro event detection\n"
        "• Crypto news sentiment \\(CryptoPanic\\)\n"
        "• Funding rates \\+ Open Interest \\+ L/S ratio\n"
        "• Fear & Greed \\+ BTC dominance\n"
        "• 8\\-pillar scoring \\(120pts max\\)\n\n"
        "📋 *Commands:*\n"
        "/signals — Manual scan \\(score ≥ 60%\\)\n"
        "/top — Single best signal now\n"
        "/briefing — Full market context report\n"
        "/status — Bot health\n"
        "/stop — Unsubscribe\n\n"
        "⚠️ _Educational only\\. Not financial advice\\._",
        parse_mode="MarkdownV2"
    )

async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    now     = asyncio.get_event_loop().time()
    if chat_id in last_scan_time and (now - last_scan_time[chat_id]) < COOLDOWN_SECONDS:
        wait = int(COOLDOWN_SECONDS - (now - last_scan_time[chat_id]))
        await update.message.reply_text(f"⏳ Wait `{wait}s` before scanning again\\.", parse_mode="MarkdownV2")
        return
    last_scan_time[chat_id] = now
    msg = await update.message.reply_text("🔎 Running 8\\-pillar market\\-aware analysis\\.\\.\\.", parse_mode="MarkdownV2")
    await scan_and_send(context.bot, chat_id, threshold=MANUAL_THRESHOLD)
    try:
        await msg.delete()
    except Exception:
        pass

async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🏆 Finding best signal\\.\\.\\.", parse_mode="MarkdownV2")
    try:
        results, ctx = await get_top_signals()
        if not results:
            await update.message.reply_text("📉 No qualifying signals right now\\.", parse_mode="MarkdownV2")
        else:
            best   = results[0]
            report = "🏆 *TOP SIGNAL RIGHT NOW*\n\n" + format_signal(best, 0)
            try:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=report, parse_mode="MarkdownV2")
            except Exception:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=report.replace("*","").replace("`","").replace("_","").replace("\\",""))
    except Exception as e:
        logger.error(f"/top error: {e}")
    finally:
        try:
            await msg.delete()
        except Exception:
            pass

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    Shows BTC trend, Fear & Greed, dominance, macro events, L/S ratio, OI.
    """
    global last_ctx
    msg = await update.message.reply_text("📊 Fetching full market intelligence\\.\\.\\.", parse_mode="MarkdownV2")
    try:
        if last_ctx is None:
            # Need to do a fresh fetch
            import ccxt.async_support as ccxt
            from market_intel import build_market_context
            exchange = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
            try:
                await exchange.load_markets()
                last_ctx = await build_market_context(exchange, [], "")
            finally:
                await exchange.close()

        ctx = last_ctx
        ls_str  = f"{ctx.ls_ratio:.2f}" if ctx.ls_ratio else "N/A"
        oi_str  = f"{ctx.oi_change_pct:+.1f}%" if ctx.oi_change_pct else "N/A"
        dom_str = f"{ctx.btc_dominance:.1f}%" if ctx.btc_dominance else "N/A"

        btc_icon  = "🟢 Bullish" if ctx.btc_is_bullish() else ("🔴 Bearish" if ctx.btc_is_bearish() else "⚪ Neutral")
        fg_bar    = "█" * (ctx.fear_greed // 10) + "░" * (10 - ctx.fear_greed // 10)

        report = (
            f"📊 *MARKET INTELLIGENCE BRIEFING*\n"
            f"`{ctx.fetched_at}`\n\n"
            f"₿ *Bitcoin*\n"
            f"Price: `${ctx.btc_price:,.0f}` \\({ctx.btc_change_24h:+.1f}% 24h\\)\n"
            f"4H Trend: `{btc_icon}`\n"
            f"Daily Trend: `{ctx.btc_trend_daily}`\n\n"
            f"😨 *Sentiment*\n"
            f"Fear & Greed: `{ctx.fear_greed}/100` {ctx.fear_greed_label}\n"
            f"`{fg_bar}`\n"
            f"BTC Dominance: `{dom_str}`\n\n"
            f"📈 *Futures Positioning*\n"
            f"L/S Ratio: `{ls_str}` \\({'🐂 More longs' if ctx.ls_ratio > 1 else '🐻 More shorts'}\\)\n"
            f"OI Change: `{oi_str}` \\(4H\\)\n\n"
        )

        if ctx.macro_event_today:
            report += (
                f"🚨 *MACRO EVENT TODAY*\n"
                f"`{ctx.macro_event_name}`\n"
                f"Impact: `{ctx.macro_event_impact}`\n"
                f"⚠️ _Expect high volatility\\. Reduce position size\\._\n\n"
            )
        else:
            report += "✅ *No major macro events today*\n\n"

        # What this means for trading
        if ctx.btc_is_bearish():
            report += "🔴 *Signal: ALL altcoin LONGs blocked \\(BTC bearish\\)*\n"
        elif ctx.btc_is_bullish() and ctx.fear_greed < 50:
            report += "🟢 *Signal: Favorable — BTC bull + Fear = good LONG setup*\n"
        elif ctx.is_extreme_greed():
            report += "⚠️ *Signal: Caution — Extreme Greed, LONGs risky*\n"

        await update.message.reply_text(report, parse_mode="MarkdownV2")

    except Exception as e:
        logger.error(f"/briefing error: {e}")
        await update.message.reply_text("⚠️ Briefing fetch failed\\. Try again\\.", parse_mode="MarkdownV2")
    finally:
        try:
            await msg.delete()
        except Exception:
            pass

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscribers.discard(update.effective_chat.id)
    save_subscribers(subscribers)
    await update.message.reply_text("🔕 Unsubscribed\\. Use /start to resubscribe\\.", parse_mode="MarkdownV2")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    btc_str = f"${last_ctx.btc_price:,.0f} ({last_ctx.btc_trend_4h})" if last_ctx else "pending"
    await update.message.reply_text(
        f"✅ *Bot Status: Online*\n\n"
        f"👥 Subscribers    : `{len(subscribers)}`\n"
        f"⏱ Auto\\-scan      : every `15 min`\n"
        f"🎯 Auto threshold : `{AUTO_THRESHOLD}%`\n"
        f"📊 Manual threshold: `{MANUAL_THRESHOLD}%`\n"
        f"📈 Timeframes     : `1H \\+ 4H`\n"
        f"₿ BTC last seen  : `{btc_str}`\n"
        f"🔢 Pairs scanned  : Top `40` by 24H volume\n"
        f"🧠 Pillars        : `8` \\(120pts max\\)\n"
        f"📰 News intel     : `{'Active' if os.getenv('CRYPTOPANIC_TOKEN') else 'Add CRYPTOPANIC_TOKEN to enable'}`",
        parse_mode="MarkdownV2"
    )

async def auto_scan_job(context: ContextTypes.DEFAULT_TYPE):
    if not subscribers:
        return
    logger.info(f"Auto\\-scan for {len(subscribers)} subscriber(s)...")
    for chat_id in list(subscribers):
        await scan_and_send(context.bot, chat_id, threshold=AUTO_THRESHOLD, is_auto=True)
        await asyncio.sleep(1)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not TOKEN:
        logger.error("BOT_TOKEN missing!")
        return

    app = ApplicationBuilder().token(TOKEN).read_timeout(30).write_timeout(30).build()
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("signals",  signals))
    app.add_handler(CommandHandler("top",      top))
    app.add_handler(CommandHandler("briefing", briefing))
    app.add_handler(CommandHandler("stop",     stop))
    app.add_handler(CommandHandler("status",   status))
    app.job_queue.run_repeating(auto_scan_job, interval=900, first=30)
    logger.info(f"QuestLife Bot v4.0 Online | {len(subscribers)} subscriber(s)")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
