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

def load_subscribers():
    if os.path.exists(SUBSCRIBERS_FILE):
        try:
            with open(SUBSCRIBERS_FILE) as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()

def save_subscribers(subs):
    try:
        with open(SUBSCRIBERS_FILE, "w") as f:
            json.dump(list(subs), f)
    except Exception as e:
        logger.error(f"Subscriber save failed: {e}")

subscribers    = load_subscribers()
last_scan_time = {}
last_ctx       = None


# ─── Formatting ───────────────────────────────────────────────────────────────

def fmt_price(p):
    if p >= 1000:  return f"{p:,.2f}"
    if p >= 1:     return f"{p:.4f}"
    if p >= 0.01:  return f"{p:.5f}"
    return f"{p:.8f}"

def format_signal(res, rank):
    medal    = {0: "🥇", 1: "🥈", 2: "🥉"}.get(rank, "💎")
    fr_str   = f"{res['funding_rate']:+.4f}%" if res.get("funding_rate") is not None else "N/A"
    vol_str  = f"${res.get('vol_24h_m', '?')}M"
    news_icon = {"POSITIVE": "📰✅", "NEGATIVE": "📰❌"}.get(res.get("news", ""), "")

    out = (
        f"{medal} *{res['symbol']}* {res['dir']} {news_icon}\n"
        f"Entry  : `{fmt_price(res['entry'])}`\n"
        f"TP     : `{fmt_price(res['tp'])}` | SL: `{fmt_price(res['sl'])}`\n"
        f"Lev    : `{res['lev']}x` | R:R `1:{res['rr']}`\n"
        f"Score  : `{res['score']}%` | RSI `{res['rsi']}` | ADX `{res['adx']}`\n"
        f"FR     : `{fr_str}` | Vol: `{vol_str}`\n"
        f"Reason : _{res['reasons'][:80]}_\n"
    )
    if res.get("news_headline"):
        out += f"News   : _{res['news_headline'][:70]}_\n"
    out += "─" * 28 + "\n\n"
    return out

def build_report(results, ctx, is_auto, threshold):
    header = "🚨 *HIGH-QUALITY AUTO-ALERT*" if is_auto else "🚀 *BINANCE LIVE SIGNALS*"
    ts     = datetime.now(timezone.utc).strftime("%H:%M UTC")
    body   = f"{header}  `{ts}`\n"

    if ctx:
        btc_icon = "🟢" if ctx.btc_is_bullish() else ("🔴" if ctx.btc_is_bearish() else "⚪")
        body += (
            f"\n"
            f"BTC: {btc_icon} `{ctx.btc_trend_4h}` | F&G: `{ctx.fear_greed}` {ctx.fear_greed_label}\n"
        )
        if ctx.macro_event_today:
            body += f"🚨 *MACRO: {ctx.macro_event_name}* - reduce size!\n"

    body += f"\n*{len(results)} signal(s) — score >= {threshold}%*\n\n"
    for i, r in enumerate(results):
        body += format_signal(r, i)
    body += "_Signals are educational only. Always manage your own risk._"
    return body


# ─── Core Scan & Send ─────────────────────────────────────────────────────────

async def scan_and_send(bot, chat_id, threshold, is_auto=False):
    global last_ctx
    try:
        results, ctx = await get_top_signals()
        last_ctx = ctx
        filtered = [s for s in results if s["score"] >= threshold]

        if not filtered:
            if not is_auto:
                await bot.send_message(
                    chat_id=chat_id,
                    text="📉 No signals meet criteria right now. Market may be ranging — patience is key."
                )
            return

        report = build_report(filtered, ctx, is_auto=is_auto, threshold=threshold)

        if len(report) <= 4000:
            try:
                await bot.send_message(chat_id=chat_id, text=report, parse_mode="Markdown")
            except Exception:
                await bot.send_message(chat_id=chat_id, text=report.replace("*", "").replace("`", "").replace("_", ""))
        else:
            for chunk in [filtered[i:i+3] for i in range(0, len(filtered), 3)]:
                msg = build_report(chunk, None, is_auto=is_auto, threshold=threshold)
                try:
                    await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                except Exception:
                    await bot.send_message(chat_id=chat_id, text=msg.replace("*", "").replace("`", "").replace("_", ""))
                await asyncio.sleep(0.5)

    except Exception as e:
        logger.error(f"scan_and_send error [{chat_id}]: {e}", exc_info=True)
        if not is_auto:
            await bot.send_message(chat_id=chat_id, text="⚠️ Scan error. Please try again in a moment.")


# ─── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subscribers.add(chat_id)
    save_subscribers(subscribers)
    await update.message.reply_text(
        "🚀 *QuestLife Signal Bot v4.0 — Active*\n\n"
        "📡 *Intelligence layers:*\n"
        "• BTC 4H trend gate (bears block all LONG signals)\n"
        "• FOMC/CPI/NFP macro event detection\n"
        "• Crypto news sentiment (CryptoPanic)\n"
        "• Funding rates + Open Interest + L/S ratio\n"
        "• Fear & Greed + BTC dominance\n"
        "• 8-pillar scoring (120pts max)\n\n"
        "📋 *Commands:*\n"
        "/signals — Manual scan (score >= 60%)\n"
        "/top — Single best signal now\n"
        "/briefing — Full market context report\n"
        "/status — Bot health\n"
        "/stop — Unsubscribe\n\n"
        "⚠️ _Educational only. Not financial advice._",
        parse_mode="Markdown"
    )


async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    now     = asyncio.get_event_loop().time()
    if chat_id in last_scan_time and (now - last_scan_time[chat_id]) < COOLDOWN_SECONDS:
        wait = int(COOLDOWN_SECONDS - (now - last_scan_time[chat_id]))
        await update.message.reply_text(f"⏳ Please wait {wait}s before scanning again.")
        return
    last_scan_time[chat_id] = now
    msg = await update.message.reply_text("🔎 Running 8-pillar market-aware analysis...")
    await scan_and_send(context.bot, chat_id, threshold=MANUAL_THRESHOLD)
    try:
        await msg.delete()
    except Exception:
        pass


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🏆 Finding best signal right now...")
    try:
        results, ctx = await get_top_signals()
        if not results:
            await update.message.reply_text("📉 No qualifying signals right now. Try again later.")
        else:
            best   = results[0]
            report = "🏆 *TOP SIGNAL RIGHT NOW*\n\n" + format_signal(best, 0)
            try:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=report, parse_mode="Markdown")
            except Exception:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=report.replace("*", "").replace("`", "").replace("_", ""))
    except Exception as e:
        logger.error(f"/top error: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Error fetching top signal. Try again.")
    finally:
        try:
            await msg.delete()
        except Exception:
            pass


async def briefing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_ctx
    msg = await update.message.reply_text("📊 Fetching market intelligence...")
    try:
        # Build fresh context if no scan has run yet
        if last_ctx is None:
            import ccxt.async_support as ccxt_lib
            from market_intel import build_market_context
            exchange = ccxt_lib.binance({
                "options": {"defaultType": "future"},
                "enableRateLimit": True,
            })
            try:
                await exchange.load_markets()
                last_ctx = await build_market_context(exchange, [], "")
            finally:
                await exchange.close()

        ctx = last_ctx

        # Safe value strings — no special chars that break Markdown
        btc_icon   = "🟢" if ctx.btc_is_bullish() else ("🔴" if ctx.btc_is_bearish() else "⚪")
        daily_icon = "🟢" if ctx.btc_trend_daily == "BULL" else ("🔴" if ctx.btc_trend_daily == "BEAR" else "⚪")
        fg_icon    = "😱" if ctx.is_extreme_fear() else ("🤑" if ctx.is_extreme_greed() else "😐")

        # Format numbers as plain strings — no +/- signs outside code blocks
        price_str  = f"${ctx.btc_price:,.0f}" if ctx.btc_price else "N/A"
        change_val = ctx.btc_change_24h or 0
        change_str = f"up {change_val:.1f}pct" if change_val >= 0 else f"down {abs(change_val):.1f}pct"
        ls_str     = f"{ctx.ls_ratio:.2f}" if ctx.ls_ratio else "N/A"
        oi_val     = ctx.oi_change_pct or 0
        oi_str     = f"up {oi_val:.1f}pct" if oi_val >= 0 else f"down {abs(oi_val):.1f}pct"
        dom_str    = f"{ctx.btc_dominance:.1f}pct" if ctx.btc_dominance else "N/A"
        fg_bar     = "█" * (ctx.fear_greed // 10) + "░" * (10 - ctx.fear_greed // 10)

        # Trading verdict — plain text, no special chars
        if ctx.btc_is_bearish():
            verdict = "🔴 BTC 4H bearish — all altcoin LONGs blocked by bot"
        elif ctx.btc_is_bullish() and ctx.fear_greed < 50:
            verdict = "🟢 BTC bullish + Fear = good LONG environment"
        elif ctx.is_extreme_greed():
            verdict = "⚠️ Extreme Greed — LONGs are risky, consider reducing size"
        elif ctx.is_extreme_fear():
            verdict = "💡 Extreme Fear — historically good LONG accumulation zone"
        else:
            verdict = "⚪ Neutral conditions — follow individual signal scores"

        # Build report using only safe Markdown (* and `)
        report = (
            "📊 *MARKET INTELLIGENCE BRIEFING*\n"
            f"_{ctx.fetched_at}_\n\n"
            "₿ *Bitcoin*\n"
            f"Price    : `{price_str}` ({change_str} 24h)\n"
            f"4H Trend : {btc_icon} `{ctx.btc_trend_4h}`\n"
            f"Daily    : {daily_icon} `{ctx.btc_trend_daily}`\n\n"
            "😨 *Sentiment*\n"
            f"Fear and Greed : `{ctx.fear_greed} of 100` {fg_icon} {ctx.fear_greed_label}\n"
            f"`{fg_bar}`\n"
            f"BTC Dominance  : `{dom_str}`\n\n"
            "📈 *Futures Positioning*\n"
            f"Long/Short Ratio : `{ls_str}`\n"
            f"OI Change (4H)   : `{oi_str}`\n\n"
        )

        if ctx.macro_event_today:
            report += (
                "🚨 *MACRO EVENT TODAY*\n"
                f"`{ctx.macro_event_name}`\n"
                f"Impact : `{ctx.macro_event_impact}`\n"
                "⚠️ Expect volatility. Reduce position size.\n\n"
            )
        else:
            report += "✅ *No major macro events today*\n\n"

        report += f"*Verdict:* {verdict}"

        # Send as plain Markdown
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=report,
                parse_mode="Markdown"
            )
        except Exception as md_err:
            logger.warning(f"Markdown send failed, sending plain: {md_err}")
            # Ultimate fallback — strip all markdown
            plain = report.replace("*", "").replace("`", "").replace("_", "")
            await context.bot.send_message(chat_id=update.effective_chat.id, text=plain)

    except Exception as e:
        logger.error(f"/briefing error: {e}", exc_info=True)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ Briefing failed. Run /signals first to warm up market data, then try /briefing again."
        )
    finally:
        try:
            await msg.delete()
        except Exception:
            pass


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscribers.discard(update.effective_chat.id)
    save_subscribers(subscribers)
    await update.message.reply_text("🔕 Unsubscribed. Use /start to resubscribe anytime.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    btc_str  = f"${last_ctx.btc_price:,.0f} ({last_ctx.btc_trend_4h})" if last_ctx else "Run /signals first"
    news_str = "Active" if os.getenv("CRYPTOPANIC_TOKEN") else "Add CRYPTOPANIC_TOKEN to enable"
    await update.message.reply_text(
        "✅ *Bot Status: Online*\n\n"
        f"Subscribers     : `{len(subscribers)}`\n"
        f"Auto-scan       : every `15 min`\n"
        f"Auto threshold  : `{AUTO_THRESHOLD}%`\n"
        f"Manual threshold: `{MANUAL_THRESHOLD}%`\n"
        f"Timeframes      : `1H + 4H`\n"
        f"BTC last seen   : `{btc_str}`\n"
        f"Pairs scanned   : Top `40` by 24H volume\n"
        f"Pillars         : `8` (120pts max)\n"
        f"News intel      : `{news_str}`",
        parse_mode="Markdown"
    )


async def auto_scan_job(context: ContextTypes.DEFAULT_TYPE):
    if not subscribers:
        return
    logger.info(f"Auto-scan for {len(subscribers)} subscriber(s)...")
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
