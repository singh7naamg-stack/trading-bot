import os
import json
import logging
import asyncio
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from engine import get_top_signals, AUTO_THRESHOLD, MANUAL_THRESHOLD, MAX_SIGNALS

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
load_dotenv()

TOKEN            = os.getenv("BOT_TOKEN")
SUBSCRIBERS_FILE = "subscribers.json"
COOLDOWN_SECONDS = 120


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
    medal     = {0: "🥇", 1: "🥈", 2: "🥉"}.get(rank, "💎")
    fr_str    = f"{res['funding_rate']:+.4f}%" if res.get("funding_rate") is not None else "N/A"
    vol_str   = f"${res.get('vol_24h_m', '?')}M"
    news_icon = {"POSITIVE": "📰✅", "NEGATIVE": "📰❌"}.get(res.get("news", ""), "")
    score     = res['score']

    # Score quality label
    if score >= 100:   quality = "🔥 ELITE"
    elif score >= 90:  quality = "⚡ STRONG"
    elif score >= 80:  quality = "✅ GOOD"
    else:              quality = "📊 VALID"

    out = (
        f"{medal} *{res['symbol']}* {res['dir']} {news_icon}\n"
        f"Quality : {quality} `({score}%)`\n"
        f"Entry   : `{fmt_price(res['entry'])}`\n"
        f"TP      : `{fmt_price(res['tp'])}` | SL: `{fmt_price(res['sl'])}`\n"
        f"Lev     : `{res['lev']}x` | R:R `1:{res['rr']}`\n"
        f"RSI     : `{res['rsi']}` | ADX: `{res['adx']}` | Vol: `{vol_str}`\n"
        f"FR      : `{fr_str}`\n"
        f"Reason  : _{res['reasons'][:90]}_\n"
    )
    if res.get("news_headline"):
        out += f"News    : _{res['news_headline'][:70]}_\n"
    out += "─" * 28 + "\n\n"
    return out

def build_report(results, ctx, is_auto, threshold):
    header = "🚨 *HIGH-QUALITY AUTO-ALERT*" if is_auto else "🚀 *BINANCE SIGNALS — STRICT MODE*"
    ts     = datetime.now(timezone.utc).strftime("%H:%M UTC")

    body = f"{header}  `{ts}`\n"

    if ctx:
        btc_icon = "🟢" if ctx.btc_is_bullish() else ("🔴" if ctx.btc_is_bearish() else "⚪")
        body += (
            f"BTC: {btc_icon} `{ctx.btc_trend_4h}` | "
            f"F&G: `{ctx.fear_greed}` {ctx.fear_greed_label} | "
            f"L/S: `{ctx.ls_ratio:.2f}`\n"
        )
        if ctx.macro_event_today:
            body += f"🚨 *MACRO: {ctx.macro_event_name}* - reduce size!\n"

    body += f"\n*{len(results)} signal(s) — score >= {threshold}%*\n\n"

    for i, r in enumerate(results):
        body += format_signal(r, i)

    body += (
        f"_Strict mode: passed 6 hard filters + 10-pillar scoring._\n"
        f"_Max {MAX_SIGNALS} signals per scan. Educational only._"
    )
    return body


# ─── Core Scan & Send ─────────────────────────────────────────────────────────

async def scan_and_send(bot, chat_id, threshold, is_auto=False):
    global last_ctx
    try:
        results, ctx = await get_top_signals()
        last_ctx     = ctx
        filtered     = [s for s in results if s["score"] >= threshold]

        if not filtered:
            if not is_auto:
                # Tell user WHY there are no signals — not just silence
                btc_str  = f"BTC is currently {ctx.btc_trend_4h}" if ctx else ""
                macro_str = f"\n\nMacro event today: {ctx.macro_event_name}" if ctx and ctx.macro_event_today else ""
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"📭 *No signals passed strict criteria right now.*\n\n"
                        f"{btc_str}{macro_str}\n\n"
                        f"This is normal. The bot found no setups meeting the {threshold}% threshold "
                        f"after passing all 6 hard filters and 10-pillar scoring.\n\n"
                        f"_Waiting for a real setup is the right move._"
                    ),
                    parse_mode="Markdown"
                )
            return

        report = build_report(filtered, ctx, is_auto=is_auto, threshold=threshold)

        if len(report) <= 4000:
            try:
                await bot.send_message(chat_id=chat_id, text=report, parse_mode="Markdown")
            except Exception:
                await bot.send_message(chat_id=chat_id, text=report.replace("*","").replace("`","").replace("_",""))
        else:
            for chunk in [filtered[i:i+2] for i in range(0, len(filtered), 2)]:
                msg = build_report(chunk, None, is_auto=is_auto, threshold=threshold)
                try:
                    await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                except Exception:
                    await bot.send_message(chat_id=chat_id, text=msg.replace("*","").replace("`","").replace("_",""))
                await asyncio.sleep(0.5)

    except Exception as e:
        logger.error(f"scan_and_send [{chat_id}]: {e}", exc_info=True)
        if not is_auto:
            await bot.send_message(chat_id=chat_id, text="⚠️ Scan error. Please try again.")


# ─── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subscribers.add(chat_id)
    save_subscribers(subscribers)
    await update.message.reply_text(
        "🚀 *QuestLife Signal Bot v5.0 — STRICT MODE*\n\n"
        "📡 *What makes this strict:*\n"
        "• 6 hard filters — coin rejected instantly if any fail\n"
        "• 10-pillar scoring — EMA, RSI, ADX, Volume, Funding,\n"
        "  OI, L/S Ratio, F&G, Support/Resistance, Candle Pattern\n"
        "• Max 5 signals per scan — only the best\n"
        "• Auto alerts only at score >= 85%\n"
        "• Manual scan only at score >= 75%\n"
        "• No 4H confirmation = rejected entirely\n"
        "• ADX < 22 = ranging market = rejected\n"
        "• Chasing entries (RSI > 68) = rejected\n\n"
        "📋 *Commands:*\n"
        "/signals — Manual scan (>= 75%)\n"
        "/top — Best single signal now\n"
        "/briefing — Full market context\n"
        "/status — Bot health\n"
        "/stop — Unsubscribe\n\n"
        "⚠️ _If bot sends nothing — that IS the signal. No setup = no trade._",
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
    msg = await update.message.reply_text("🔎 Running strict 10-pillar scan across top 40 pairs...")
    await scan_and_send(context.bot, chat_id, threshold=MANUAL_THRESHOLD)
    try:
        await msg.delete()
    except Exception:
        pass


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🏆 Finding the single best signal...")
    try:
        results, ctx = await get_top_signals()
        if not results:
            await update.message.reply_text(
                "📭 *No signal passed strict criteria right now.*\n\n"
                "_This means the market has no high-conviction setup at this moment. "
                "That is a valid outcome — not every scan produces a signal._",
                parse_mode="Markdown"
            )
        else:
            best   = results[0]
            report = "🏆 *BEST SIGNAL RIGHT NOW*\n\n" + format_signal(best, 0)
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=report,
                    parse_mode="Markdown"
                )
            except Exception:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=report.replace("*","").replace("`","").replace("_","")
                )
    except Exception as e:
        logger.error(f"/top error: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Error. Try again in a moment.")
    finally:
        try:
            await msg.delete()
        except Exception:
            pass


async def briefing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_ctx
    msg = await update.message.reply_text("📊 Fetching market intelligence...")
    try:
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

        btc_icon   = "🟢" if ctx.btc_is_bullish() else ("🔴" if ctx.btc_is_bearish() else "⚪")
        daily_icon = "🟢" if ctx.btc_trend_daily == "BULL" else ("🔴" if ctx.btc_trend_daily == "BEAR" else "⚪")
        fg_icon    = "😱" if ctx.is_extreme_fear() else ("🤑" if ctx.is_extreme_greed() else "😐")

        price_str  = f"${ctx.btc_price:,.0f}" if ctx.btc_price else "N/A"
        change_val = ctx.btc_change_24h or 0
        change_str = f"up {change_val:.1f}pct" if change_val >= 0 else f"down {abs(change_val):.1f}pct"
        ls_str     = f"{ctx.ls_ratio:.2f}" if ctx.ls_ratio else "N/A"
        oi_val     = ctx.oi_change_pct or 0
        oi_str     = f"up {oi_val:.1f}pct" if oi_val >= 0 else f"down {abs(oi_val):.1f}pct"
        dom_str    = f"{ctx.btc_dominance:.1f}pct" if ctx.btc_dominance else "N/A"
        fg_bar     = "█" * (ctx.fear_greed // 10) + "░" * (10 - ctx.fear_greed // 10)

        if ctx.btc_is_bearish():
            verdict = "🔴 BTC 4H bearish — all altcoin LONGs blocked"
        elif ctx.btc_is_bullish() and ctx.fear_greed < 50:
            verdict = "🟢 BTC bullish + Fear = strong LONG environment"
        elif ctx.is_extreme_greed():
            verdict = "⚠️ Extreme Greed — LONGs risky, reduce size"
        elif ctx.is_extreme_fear():
            verdict = "💡 Extreme Fear — good LONG accumulation zone"
        else:
            verdict = "⚪ Neutral — follow individual signal scores"

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

        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=report,
                parse_mode="Markdown"
            )
        except Exception:
            plain = report.replace("*","").replace("`","").replace("_","")
            await context.bot.send_message(chat_id=update.effective_chat.id, text=plain)

    except Exception as e:
        logger.error(f"/briefing error: {e}", exc_info=True)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ Briefing failed. Run /signals first then try /briefing again."
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
        "✅ *Bot Status: Online — v5.0 STRICT*\n\n"
        f"Subscribers      : `{len(subscribers)}`\n"
        f"Auto-scan        : every `15 min`\n"
        f"Auto threshold   : `{AUTO_THRESHOLD}%`\n"
        f"Manual threshold : `{MANUAL_THRESHOLD}%`\n"
        f"Max signals      : `{MAX_SIGNALS}` per scan\n"
        f"Min volume       : `$10M` 24H\n"
        f"Timeframes       : `1H + 4H`\n"
        f"Hard filters     : `6`\n"
        f"Scoring pillars  : `10` (130pts max)\n"
        f"BTC last seen    : `{btc_str}`\n"
        f"News intel       : `{news_str}`",
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
    logger.info(f"QuestLife Bot v5.0 STRICT Online | {len(subscribers)} subscriber(s)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
