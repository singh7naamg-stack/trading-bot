import os
import json
import logging
import asyncio
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    CallbackQueryHandler, ContextTypes
)

from engine import get_top_signals, AUTO_THRESHOLD, MANUAL_THRESHOLD, MAX_SIGNALS

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)
load_dotenv()

TOKEN            = os.getenv("BOT_TOKEN")
SUBSCRIBERS_FILE = "/data/subscribers.json"
BALANCES_FILE    = "/data/balances.json"
TRADES_FILE      = "/data/trades.json"
COOLDOWN_SECONDS = 120

# ─── Persistence ──────────────────────────────────────────────────────────────

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default

def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Save failed {path}: {e}")

subscribers    = set(load_json(SUBSCRIBERS_FILE, []))
balances       = load_json(BALANCES_FILE, {})
open_trades    = load_json(TRADES_FILE, {})   # {chat_id: [trade, trade, ...]}
last_scan_time = {}
last_ctx       = None
last_signal_scores = {}  # track last score per symbol to avoid repeat alerts


# ─── Price Formatter ──────────────────────────────────────────────────────────

def fmt(p):
    if p >= 1000:  return f"{p:,.2f}"
    if p >= 1:     return f"{p:.4f}"
    if p >= 0.01:  return f"{p:.5f}"
    return f"{p:.8f}"


# ─── Position Size Calculator ─────────────────────────────────────────────────

def calc_position(balance_usdt, sl_pct, lev):
    if sl_pct <= 0 or balance_usdt <= 0:
        return 0, 0, 0
    risk_amount   = balance_usdt * 0.02
    position_size = risk_amount / (sl_pct / 100)
    margin_needed = position_size / lev
    return round(risk_amount, 2), round(position_size, 2), round(margin_needed, 2)


# ─── Signal Formatter ─────────────────────────────────────────────────────────

def fmt_signal(res, rank, balance=None):
    medal   = {0:"🥇",1:"🥈",2:"🥉"}.get(rank,"💎")
    score   = res["score"]
    fr_str  = f"{res['funding_rate']:+.4f}%" if res.get("funding_rate") is not None else "N/A"
    quality = "🔥 ELITE" if score>=110 else ("⚡ STRONG" if score>=95 else ("✅ GOOD" if score>=85 else "📊 VALID"))
    news_ic = {"POSITIVE":"📰✅","NEGATIVE":"📰❌"}.get(res.get("news",""),"")

    out = (
        f"{medal} *{res['symbol']}* {res['dir']} {news_ic}\n"
        f"Quality : {quality} `({score}pts)`\n"
        f"Entry   : `{fmt(res['entry'])}`\n"
        f"TP1     : `{fmt(res['tp1'])}` _(close 40% here)_\n"
        f"TP2     : `{fmt(res['tp2'])}` _(close 40% here)_\n"
        f"TP3     : `{fmt(res['tp3'])}` _(let 20% run)_\n"
        f"SL      : `{fmt(res['sl'])}` _({res['sl_pct']}% away)_\n"
        f"Leverage: `{res['lev']}x` | R:R `1:{res['rr']}`\n"
        f"RSI     : `{res['rsi']}` | ADX: `{res['adx']}` | FR: `{fr_str}`\n"
        f"Liq Zone: `~{fmt(res['liq_est'])}`\n"
    )

    if balance and balance > 0:
        risk, pos, margin = calc_position(balance, res["sl_pct"], res["lev"])
        out += (
            f"\n💰 *Position (2% rule):*\n"
            f"Put in  : `${margin:.2f}` USDT margin\n"
            f"Max loss: `${risk:.2f}` if SL hits\n"
            f"If TP2  : `+${risk * res['rr']:.2f}` profit\n"
        )

    if res.get("news_headline"):
        out += f"News    : _{res['news_headline'][:65]}_\n"

    out += f"Reason  : _{res['reasons'][:85]}_\n"

    # Quick add trade button
    direction = "LONG" if "LONG" in res["dir"] else "SHORT"
    out += f"\n_Reply /addtrade {res['symbol'].replace('/USDT','')} {direction} {fmt(res['entry'])} {fmt(res['tp2'])} {fmt(res['sl'])} to monitor this trade_"
    out += "\n" + "─" * 30 + "\n\n"
    return out


def fmt_guide(res):
    side = "BUY / LONG" if "LONG" in res["dir"] else "SELL / SHORT"
    icon = "🟢" if "LONG" in res["dir"] else "🔴"
    return (
        f"📖 *HOW TO ENTER: {res['symbol']}*\n\n"
        f"*Step 1 — Open Binance App*\n"
        f"Futures → USDT-M → search `{res['symbol'].replace('/USDT','')}`\n\n"
        f"*Step 2 — Set Leverage*\n"
        f"Set to `{res['lev']}x` max. Do not go higher.\n\n"
        f"*Step 3 — Place Order*\n"
        f"Order type: `Limit`\n"
        f"Direction: {icon} `{side}`\n"
        f"Price: `{fmt(res['entry'])}`\n\n"
        f"*Step 4 — Set Stop Loss IMMEDIATELY*\n"
        f"SL at: `{fmt(res['sl'])}` — set this before anything else\n"
        f"⚠️ _Never remove your SL. It protects your account._\n\n"
        f"*Step 5 — Set Take Profits*\n"
        f"TP1 `{fmt(res['tp1'])}` → close 40% of position\n"
        f"TP2 `{fmt(res['tp2'])}` → close 40% of position\n"
        f"TP3 `{fmt(res['tp3'])}` → close remaining 20%\n\n"
        f"*Step 6 — After TP1 hits*\n"
        f"Move your SL to entry `{fmt(res['entry'])}` → trade is now risk-free\n\n"
        f"*If SL hits:* Accept the loss, close the trade.\n"
        f"Do not add more money. The bot will find the next setup.\n\n"
        f"*To let the bot monitor this trade:*\n"
        f"`/addtrade {res['symbol'].replace('/USDT','')} {'LONG' if 'LONG' in res['dir'] else 'SHORT'} {fmt(res['entry'])} {fmt(res['tp2'])} {fmt(res['sl'])}`"
    )


def build_header(results, ctx, is_auto, threshold):
    header = "🚨 *HIGH CONVICTION SIGNAL FOUND*" if is_auto else "🚀 *STRICT SIGNALS — PROFESSIONAL SCAN*"
    ts     = datetime.now(timezone.utc).strftime("%H:%M UTC")
    body   = f"{header}  `{ts}`\n"
    if ctx:
        btc_icon = "🟢" if ctx.btc_is_bullish() else ("🔴" if ctx.btc_is_bearish() else "⚪")
        body += (
            f"BTC: {btc_icon} `{ctx.btc_trend_4h}` | "
            f"F&G: `{ctx.fear_greed}` {ctx.fear_greed_label} | "
            f"L/S: `{ctx.ls_ratio:.2f}`\n"
        )
        if ctx.macro_event_today:
            body += f"🚨 *MACRO: {ctx.macro_event_name}* — reduce size!\n"
    body += f"\n*{len(results)} signal(s) — {threshold}%+ score*\n\n"
    return body


# ─── Scan & Send ──────────────────────────────────────────────────────────────

async def scan_and_send(bot, chat_id, threshold, is_auto=False):
    global last_ctx
    try:
        results, ctx = await get_top_signals()
        last_ctx     = ctx
        filtered     = [s for s in results if s["score"] >= threshold]

        if not filtered:
            if not is_auto:
                btc_s  = ctx.btc_trend_4h if ctx else "unknown"
                macro  = f"\nMacro event: *{ctx.macro_event_name}*" if ctx and ctx.macro_event_today else ""
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"📭 *No signals passed strict criteria.*\n\n"
                        f"BTC is `{btc_s}`{macro}\n\n"
                        f"Scanned top 25 pairs through 6 hard filters and 13 pillars. "
                        f"Nothing reached {threshold}%.\n\n"
                        f"_No signal = no trade. Patience protects capital._"
                    ),
                    parse_mode="Markdown"
                )
            return False  # No signals sent

        balance = balances.get(str(chat_id), 0)

        # Header
        header = build_header(filtered, ctx, is_auto, threshold)
        try:
            await bot.send_message(chat_id=chat_id, text=header, parse_mode="Markdown")
        except Exception:
            await bot.send_message(chat_id=chat_id, text=header.replace("*","").replace("`","").replace("_",""))

        # Each signal + guide
        for i, res in enumerate(filtered):
            sig = fmt_signal(res, i, balance if balance > 0 else None)
            try:
                await bot.send_message(chat_id=chat_id, text=sig, parse_mode="Markdown")
            except Exception:
                await bot.send_message(chat_id=chat_id, text=sig.replace("*","").replace("`","").replace("_",""))
            await asyncio.sleep(0.3)

            guide = fmt_guide(res)
            try:
                await bot.send_message(chat_id=chat_id, text=guide, parse_mode="Markdown")
            except Exception:
                await bot.send_message(chat_id=chat_id, text=guide.replace("*","").replace("`","").replace("_",""))
            await asyncio.sleep(0.3)

        footer = (
            "_Strategy: Take 40% at TP1, move SL to entry (risk-free), "
            "take 40% at TP2, let 20% run to TP3._\n"
            "_Never risk more than 2% of balance per trade._"
        )
        try:
            await bot.send_message(chat_id=chat_id, text=footer, parse_mode="Markdown")
        except Exception:
            await bot.send_message(chat_id=chat_id, text=footer.replace("*","").replace("`","").replace("_",""))

        return True  # Signals were sent

    except Exception as e:
        logger.error(f"scan_and_send [{chat_id}]: {e}", exc_info=True)
        if not is_auto:
            await bot.send_message(chat_id=chat_id, text="⚠️ Scan error. Please try again.")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
#  TRADE MONITOR
#  User registers a trade → bot checks it every 5 minutes
#  Alerts on: SL approach, TP hits, trend reversal, RSI extreme, BTC flip
# ═══════════════════════════════════════════════════════════════════════════════

def get_user_trades(chat_id):
    return open_trades.get(str(chat_id), [])

def save_user_trades(chat_id, trades):
    open_trades[str(chat_id)] = trades
    save_json(TRADES_FILE, open_trades)


async def fetch_current_price(symbol):
    """
    Fetch live price using Binance REST directly — no ccxt overhead.
    Single lightweight call, no connection setup/teardown.
    """
    try:
        import aiohttp
        url    = "https://fapi.binance.com/fapi/v1/ticker/price"
        params = {"symbol": f"{symbol}USDT"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            async with s.get(url, params=params) as r:
                if r.status == 200:
                    data = await r.json()
                    return float(data["price"])
    except Exception as e:
        logger.warning(f"Price fetch failed for {symbol}: {e}")
        return None


async def monitor_trade(bot, chat_id, trade):
    """
    Check one open trade against current market conditions.
    
    Alert conditions (in order of severity):
      🔴 EMERGENCY — Price hit or passed SL → get out NOW
      🔴 CRITICAL  — Price within 1% of SL → prepare to exit
      🟡 WARNING   — BTC 4H flipped against trade direction
      🟡 WARNING   — RSI went extreme against trade (overbought on LONG etc)
      🟡 WARNING   — Funding rate went extreme against position
      🟢 SUCCESS   — TP1 hit → take 40% profit, move SL to entry
      🟢 SUCCESS   — TP2 hit → take 40% profit, let 20% run
      🟢 SUCCESS   — TP3 hit → close remaining 20%, celebrate
    """
    symbol    = trade["symbol"]
    direction = trade["direction"]   # "LONG" or "SHORT"
    entry     = trade["entry"]
    tp1       = trade["tp1"]
    tp2       = trade["tp2"]
    sl        = trade["sl"]
    tp1_hit   = trade.get("tp1_hit", False)
    tp2_hit   = trade.get("tp2_hit", False)

    # Fetch live price
    price = await fetch_current_price(symbol)
    if not price:
        return trade  # Can't check without price

    alerts = []
    updated = False

    if direction == "LONG":
        pnl_pct = ((price - entry) / entry) * 100

        # ── TP hits ───────────────────────────────────────────────────────
        if not tp1_hit and price >= tp1:
            alerts.append(
                f"🎉 *TP1 HIT — {symbol} LONG*\n\n"
                f"Price reached `{fmt(price)}`\n"
                f"TP1 was `{fmt(tp1)}`\n\n"
                f"*Action required:*\n"
                f"1. Close 40% of your position now\n"
                f"2. Move your Stop Loss UP to your entry price `{fmt(entry)}`\n"
                f"3. Your trade is now risk-free — let the rest run to TP2 `{fmt(tp2)}`\n\n"
                f"Profit so far: `+{pnl_pct:.2f}%` 💰"
            )
            trade["tp1_hit"] = True
            updated = True

        elif tp1_hit and not tp2_hit and price >= tp2:
            alerts.append(
                f"🎉 *TP2 HIT — {symbol} LONG*\n\n"
                f"Price reached `{fmt(price)}`\n\n"
                f"*Action required:*\n"
                f"1. Close another 40% of your position\n"
                f"2. Let the remaining 20% run to TP3\n"
                f"3. Move SL up to `{fmt(tp1)}` to protect remaining profit\n\n"
                f"Profit: `+{pnl_pct:.2f}%` 🚀"
            )
            trade["tp2_hit"] = True
            updated = True

        # ── SL alerts ─────────────────────────────────────────────────────
        sl_dist_pct = ((price - sl) / entry) * 100  # positive = safe

        if price <= sl:
            alerts.append(
                f"🚨 *STOP LOSS HIT — {symbol} LONG*\n\n"
                f"Current price: `{fmt(price)}`\n"
                f"Your SL was: `{fmt(sl)}`\n\n"
                f"*Close your position NOW if not already closed.*\n\n"
                f"Loss: `{pnl_pct:.2f}%`\n\n"
                f"_This is normal. The bot will find the next setup. "
                f"Protecting capital is the priority._"
            )
            trade["sl_hit"] = True
            updated = True

        elif -sl_dist_pct < 1.0 and price > sl:  # Within 1% of SL
            alerts.append(
                f"⚠️ *SL WARNING — {symbol} LONG*\n\n"
                f"Price `{fmt(price)}` is very close to your SL `{fmt(sl)}`\n"
                f"Only `{abs(sl_dist_pct):.2f}%` away\n\n"
                f"*Watch closely.* If price closes a 1H candle below `{fmt(sl)}`, "
                f"consider closing manually before SL triggers to get a better price."
            )

        # ── BTC trend flip against LONG ────────────────────────────────────
        if last_ctx and last_ctx.btc_is_bearish():
            if not trade.get("btc_warn_sent"):
                alerts.append(
                    f"⚠️ *BTC TREND WARNING — {symbol} LONG*\n\n"
                    f"BTC 4H trend has turned *BEARISH*\n"
                    f"Your LONG on {symbol} is now fighting the market direction.\n\n"
                    f"Current price: `{fmt(price)}` | Entry: `{fmt(entry)}`\n"
                    f"P&L: `{pnl_pct:+.2f}%`\n\n"
                    f"*Suggested action:*\n"
                    f"• If you are in profit: consider closing 50% now\n"
                    f"• Tighten your SL to just below `{fmt(price * 0.99)}`\n"
                    f"• Do NOT add to this position"
                )
                trade["btc_warn_sent"] = True
                updated = True

    elif direction == "SHORT":
        pnl_pct = ((entry - price) / entry) * 100

        # ── TP hits ───────────────────────────────────────────────────────
        if not tp1_hit and price <= tp1:
            alerts.append(
                f"🎉 *TP1 HIT — {symbol} SHORT*\n\n"
                f"Price dropped to `{fmt(price)}`\n"
                f"TP1 was `{fmt(tp1)}`\n\n"
                f"*Action required:*\n"
                f"1. Close 40% of your SHORT position\n"
                f"2. Move SL DOWN to your entry `{fmt(entry)}`\n"
                f"3. Trade is now risk-free — let rest run to TP2 `{fmt(tp2)}`\n\n"
                f"Profit: `+{pnl_pct:.2f}%` 💰"
            )
            trade["tp1_hit"] = True
            updated = True

        elif tp1_hit and not tp2_hit and price <= tp2:
            alerts.append(
                f"🎉 *TP2 HIT — {symbol} SHORT*\n\n"
                f"Price dropped to `{fmt(price)}`\n\n"
                f"*Action required:*\n"
                f"1. Close another 40% of position\n"
                f"2. Let 20% run further\n\n"
                f"Profit: `+{pnl_pct:.2f}%` 🚀"
            )
            trade["tp2_hit"] = True
            updated = True

        # ── SL alerts ─────────────────────────────────────────────────────
        sl_dist_pct = ((sl - price) / entry) * 100

        if price >= sl:
            alerts.append(
                f"🚨 *STOP LOSS HIT — {symbol} SHORT*\n\n"
                f"Current price: `{fmt(price)}`\n"
                f"Your SL was: `{fmt(sl)}`\n\n"
                f"*Close your position NOW if not already closed.*\n\n"
                f"Loss: `{pnl_pct:.2f}%`\n\n"
                f"_Small loss is fine. The bot finds the next setup._"
            )
            trade["sl_hit"] = True
            updated = True

        elif sl_dist_pct < 1.0 and price < sl:
            alerts.append(
                f"⚠️ *SL WARNING — {symbol} SHORT*\n\n"
                f"Price `{fmt(price)}` is very close to your SL `{fmt(sl)}`\n"
                f"Only `{sl_dist_pct:.2f}%` away\n\n"
                f"*Watch closely.* Consider closing manually to get better price."
            )

        # ── BTC trend flip against SHORT ───────────────────────────────────
        if last_ctx and last_ctx.btc_is_bullish():
            if not trade.get("btc_warn_sent"):
                alerts.append(
                    f"⚠️ *BTC TREND WARNING — {symbol} SHORT*\n\n"
                    f"BTC 4H trend is *BULLISH*\n"
                    f"Your SHORT on {symbol} is fighting the market direction.\n\n"
                    f"Current price: `{fmt(price)}` | Entry: `{fmt(entry)}`\n"
                    f"P&L: `{pnl_pct:+.2f}%`\n\n"
                    f"*Suggested action:*\n"
                    f"• If in profit: close 50% now\n"
                    f"• Tighten SL to just above current price"
                )
                trade["btc_warn_sent"] = True
                updated = True

    # Send all alerts
    for alert in alerts:
        try:
            await bot.send_message(chat_id=int(chat_id), text=alert, parse_mode="Markdown")
        except Exception:
            await bot.send_message(chat_id=int(chat_id), text=alert.replace("*","").replace("`","").replace("_",""))
        await asyncio.sleep(0.3)

    if updated:
        save_user_trades(chat_id, get_user_trades(chat_id))

    return trade


# ─── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subscribers.add(chat_id)
    save_json(SUBSCRIBERS_FILE, list(subscribers))
    await update.message.reply_text(
        "🚀 *QuestLife Signal Bot — v5.0 FINAL*\n\n"
        "*What this bot does:*\n"
        "• Scans top 25 Binance Futures pairs 24/7\n"
        "• Only alerts when a genuinely high score signal is found\n"
        "• Monitors your open trades and alerts on TP hits, SL danger, and trend reversals\n"
        "• Gives step-by-step entry instructions for every signal\n"
        "• Calculates exact position size based on your balance\n\n"
        "*Setup — do these first:*\n"
        "1. /setbalance 500 — enter your USDT balance\n"
        "2. /learn — understand the basics\n\n"
        "📋 *Commands:*\n"
        "/signals — scan now\n"
        "/top — best signal now\n"
        "/briefing — market overview\n"
        "/addtrade — register a trade to monitor\n"
        "/mytrades — see your monitored trades\n"
        "/closetrade — mark a trade as closed\n"
        "/setbalance 500 — set your balance\n"
        "/learn — trading education\n"
        "/status — bot info\n"
        "/stop — unsubscribe from auto-alerts\n\n"
        "⚠️ _Educational only. Not financial advice. Always use stop loss._",
        parse_mode="Markdown"
    )


async def setbalance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    try:
        if not context.args:
            await update.message.reply_text("Usage: /setbalance 500\nReplace 500 with your actual USDT balance.")
            return
        amount = float(context.args[0])
        if amount <= 0:
            await update.message.reply_text("Balance must be greater than 0.")
            return
        balances[chat_id] = amount
        save_json(BALANCES_FILE, balances)
        risk = amount * 0.02
        await update.message.reply_text(
            f"✅ *Balance saved: ${amount:,.2f} USDT*\n\n"
            f"Max risk per trade: `${risk:.2f}` (2% rule)\n\n"
            f"Every signal will now show your exact position size.\n"
            f"After 10 losing trades in a row you still have `${amount*0.817:,.0f}` left.",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text("Invalid number. Example: /setbalance 500")


async def addtrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Register a trade for monitoring.
    Usage: /addtrade ICPUSDT LONG 3.57 3.85 3.42
    Or:    /addtrade ICPUSDT LONG 3.57 3.85 3.42 3.70
           (symbol, direction, entry, tp2, sl, [tp1 optional])
    """
    chat_id = str(update.effective_chat.id)
    args    = context.args

    if not args or len(args) < 5:
        await update.message.reply_text(
            "Usage:\n"
            "`/addtrade ICPUSDT LONG 3.57 3.85 3.42`\n\n"
            "Fields: symbol direction entry tp sl\n\n"
            "Example:\n"
            "`/addtrade BTCUSDT LONG 67000 70000 65000`\n"
            "`/addtrade ETHUSDT SHORT 3200 3000 3350`\n\n"
            "_The bot will monitor this trade 24/7 and alert you on TP hits, "
            "SL danger, and market changes against your position._",
            parse_mode="Markdown"
        )
        return

    try:
        symbol    = args[0].upper().replace("USDT","")
        direction = args[1].upper()
        entry     = float(args[2])
        tp_main   = float(args[3])
        sl        = float(args[4])
        tp1_opt   = float(args[5]) if len(args) > 5 else None

        if direction not in ("LONG","SHORT"):
            await update.message.reply_text("Direction must be LONG or SHORT.")
            return

        # Auto-calculate TP1 if not provided (midpoint between entry and TP)
        if tp1_opt:
            tp1 = tp1_opt
        else:
            tp1 = entry + (tp_main - entry) * 0.5 if direction == "LONG" else entry - (entry - tp_main) * 0.5

        trade = {
            "symbol"       : symbol,
            "direction"    : direction,
            "entry"        : entry,
            "tp1"          : tp1,
            "tp2"          : tp_main,
            "sl"           : sl,
            "tp1_hit"      : False,
            "tp2_hit"      : False,
            "sl_hit"       : False,
            "btc_warn_sent": False,
            "added_at"     : datetime.now(timezone.utc).isoformat(),
        }

        trades = get_user_trades(chat_id)

        # Check if symbol already monitored
        existing = [t for t in trades if t["symbol"] == symbol and not t.get("sl_hit")]
        if existing:
            await update.message.reply_text(
                f"You already have an open {symbol} trade being monitored.\n"
                f"Use /closetrade {symbol} first to remove it."
            )
            return

        trades.append(trade)
        save_user_trades(chat_id, trades)

        sl_pct    = abs(entry - sl) / entry * 100
        tp_pct    = abs(tp_main - entry) / entry * 100
        direction_word = "rises" if direction == "LONG" else "drops"

        await update.message.reply_text(
            f"✅ *Trade Registered — Now Monitoring 24/7*\n\n"
            f"Symbol    : `{symbol}/USDT`\n"
            f"Direction : `{direction}`\n"
            f"Entry     : `{fmt(entry)}`\n"
            f"TP1       : `{fmt(tp1)}` _(close 40% here)_\n"
            f"TP2       : `{fmt(tp_main)}` _(close 40% here)_\n"
            f"SL        : `{fmt(sl)}`\n"
            f"Risk      : `{sl_pct:.2f}%` | Target: `{tp_pct:.2f}%`\n\n"
            f"*You will be alerted when:*\n"
            f"• Price {direction_word} to TP1 or TP2\n"
            f"• Price comes within 1% of your SL\n"
            f"• BTC trend flips against your direction\n"
            f"• SL is hit\n\n"
            f"_Monitoring every 5 minutes._",
            parse_mode="Markdown"
        )

    except (ValueError, IndexError):
        await update.message.reply_text(
            "Invalid format. Example:\n`/addtrade ICPUSDT LONG 3.57 3.85 3.42`",
            parse_mode="Markdown"
        )


async def mytrades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all monitored trades for this user."""
    chat_id = str(update.effective_chat.id)
    trades  = [t for t in get_user_trades(chat_id) if not t.get("sl_hit")]

    if not trades:
        await update.message.reply_text(
            "📭 No open trades being monitored.\n\n"
            "Use /addtrade to register a trade.\n"
            "Example: `/addtrade ICPUSDT LONG 3.57 3.85 3.42`",
            parse_mode="Markdown"
        )
        return

    msg = "📊 *Your Monitored Trades:*\n\n"
    for t in trades:
        symbol    = t["symbol"]
        direction = t["direction"]
        entry     = t["entry"]
        tp1_hit   = "✅" if t.get("tp1_hit") else "⏳"
        tp2_hit   = "✅" if t.get("tp2_hit") else "⏳"
        icon      = "🟢" if direction == "LONG" else "🔴"

        # Fetch current price
        price = await fetch_current_price(symbol)
        if price:
            pnl = ((price - entry)/entry*100) if direction=="LONG" else ((entry-price)/entry*100)
            pnl_str = f"`{pnl:+.2f}%`"
            price_str = f"`{fmt(price)}`"
        else:
            pnl_str   = "N/A"
            price_str = "N/A"

        msg += (
            f"{icon} *{symbol}/USDT {direction}*\n"
            f"Entry : `{fmt(entry)}` | Now: {price_str} | P&L: {pnl_str}\n"
            f"SL    : `{fmt(t['sl'])}` | TP2: `{fmt(t['tp2'])}`\n"
            f"TP1   : {tp1_hit} | TP2: {tp2_hit}\n"
            f"_/closetrade {symbol} to remove_\n\n"
        )

    try:
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(msg.replace("*","").replace("`","").replace("_",""))


async def closetrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mark a trade as closed and remove from monitoring."""
    chat_id = str(update.effective_chat.id)

    if not context.args:
        await update.message.reply_text(
            "Usage: /closetrade ICPUSDT\n\nOr /closetrade ALL to close all trades."
        )
        return

    symbol = context.args[0].upper().replace("USDT","")
    trades = get_user_trades(chat_id)

    if symbol == "ALL":
        save_user_trades(chat_id, [])
        await update.message.reply_text("✅ All trades removed from monitoring.")
        return

    before = len(trades)
    trades = [t for t in trades if t["symbol"] != symbol]
    after  = len(trades)

    if before == after:
        await update.message.reply_text(f"No monitored trade found for {symbol}.")
        return

    save_user_trades(chat_id, trades)
    await update.message.reply_text(
        f"✅ *{symbol}/USDT removed from monitoring.*\n\n"
        f"Use /addtrade to register a new trade.",
        parse_mode="Markdown"
    )


async def learn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args).lower() if context.args else ""
    lessons = {
        "leverage": "📚 *Leverage*\n\nMultiplies your position. 3x leverage on $100 = control $300.\nRisk: 3x also means 3x losses. Bot recommends 1-5x max.\n10x leverage = 10% move against you = total loss.",
        "sl":       "📚 *Stop Loss*\n\nAutomatic exit if price hits danger zone.\nSet it the moment you open the trade.\nNEVER remove it. It is what keeps you in the game long-term.",
        "futures":  "📚 *Futures*\n\nTrade price movements without owning the coin.\nLONG = bet price goes UP.\nSHORT = bet price goes DOWN.\nAlways use USDT-Margined futures on Binance.",
        "tp":       "📚 *Take Profit (TP1/TP2/TP3)*\n\nTP1 = close 40% (safe quick profit)\nTP2 = close 40% (main target)\nTP3 = let 20% run to catch full move\nAfter TP1: move SL to entry = risk-free trade.",
        "position": "📚 *Position Sizing*\n\nNever risk more than 2% of balance per trade.\n$500 balance = max $10 risk per trade.\nBot calculates this automatically.\nSet balance: /setbalance 500",
        "rsi":      "📚 *RSI (0-100)*\n\nAbove 68 = overbought, risky to buy\nBelow 32 = oversold, risky to short\nBot rejects signals in these zones automatically.\nBest entries: RSI 35-52 for LONG, 48-65 for SHORT.",
        "monitor":  "📚 *Trade Monitor*\n\nAfter entering a trade, tell the bot:\n`/addtrade ICPUSDT LONG 3.57 3.85 3.42`\n\nBot then watches 24/7 and alerts you when:\n• TP1 or TP2 is hit\n• Price is dangerously close to SL\n• BTC trend flips against your trade\n• SL is hit",
    }
    if topic in lessons:
        await update.message.reply_text(lessons[topic], parse_mode="Markdown")
    else:
        keyboard = [
            [InlineKeyboardButton("📊 Leverage",     callback_data="learn_leverage"),
             InlineKeyboardButton("🛡 Stop Loss",    callback_data="learn_sl")],
            [InlineKeyboardButton("📈 Futures",      callback_data="learn_futures"),
             InlineKeyboardButton("🎯 Take Profit",  callback_data="learn_tp")],
            [InlineKeyboardButton("💰 Position Size",callback_data="learn_position"),
             InlineKeyboardButton("📉 RSI",          callback_data="learn_rsi")],
            [InlineKeyboardButton("👁 Trade Monitor",callback_data="learn_monitor")],
        ]
        await update.message.reply_text(
            "📚 *Trading Education — Pick a Topic:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def learn_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    topic = query.data.replace("learn_","")
    lessons = {
        "leverage": "📚 *Leverage*\n\nMultiplies your position. 3x on $100 = control $300.\nAlso multiplies losses. Bot recommends 1-5x max.",
        "sl":       "📚 *Stop Loss*\n\nSet it when you open the trade. NEVER remove it.\nIt is what keeps you in the game long-term.",
        "futures":  "📚 *Futures*\n\nLONG = bet price goes UP.\nSHORT = bet price goes DOWN.\nAlways use USDT-Margined on Binance.",
        "tp":       "📚 *Take Profit*\n\nTP1 = close 40%, TP2 = close 40%, TP3 = let 20% run.\nAfter TP1: move SL to entry = risk-free.",
        "position": "📚 *Position Sizing*\n\nNever risk more than 2% per trade.\n$500 balance = $10 max risk.\nBot calculates it: /setbalance 500",
        "rsi":      "📚 *RSI*\n\nAbove 68 = overbought (bot rejects LONG)\nBelow 32 = oversold (bot rejects SHORT)\nBest LONG entries: RSI 35-52.",
        "monitor":  "📚 *Trade Monitor*\n\n`/addtrade ICPUSDT LONG 3.57 3.85 3.42`\n\nBot watches 24/7 → alerts on TP hits, SL danger, BTC trend flip.",
    }
    text = lessons.get(topic, "Topic not found. Try /learn")
    try:
        await context.bot.send_message(chat_id=query.message.chat_id, text=text, parse_mode="Markdown")
    except Exception:
        await context.bot.send_message(chat_id=query.message.chat_id, text=text.replace("*","").replace("`",""))


async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    now     = asyncio.get_event_loop().time()
    if chat_id in last_scan_time and (now - last_scan_time[chat_id]) < COOLDOWN_SECONDS:
        wait = int(COOLDOWN_SECONDS - (now - last_scan_time[chat_id]))
        await update.message.reply_text(f"⏳ Please wait {wait}s before scanning again.")
        return
    last_scan_time[chat_id] = now
    msg = await update.message.reply_text("🔎 Running strict 13-pillar scan...")
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
                "📭 *No signal passed strict criteria.*\n\n"
                "_No setup = no trade. Patience protects capital._",
                parse_mode="Markdown"
            )
        else:
            best    = results[0]
            balance = balances.get(str(update.effective_chat.id), 0)
            sig     = fmt_signal(best, 0, balance if balance > 0 else None)
            guide   = fmt_guide(best)
            for text in ["🏆 *BEST SIGNAL NOW*\n\n" + sig, guide]:
                try:
                    await context.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode="Markdown")
                except Exception:
                    await context.bot.send_message(chat_id=update.effective_chat.id, text=text.replace("*","").replace("`","").replace("_",""))
                await asyncio.sleep(0.3)
    except Exception as e:
        logger.error(f"/top: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Error. Try again.")
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
            exchange = ccxt_lib.binance({"options":{"defaultType":"future"},"enableRateLimit":True})
            try:
                await exchange.load_markets()
                last_ctx = await build_market_context(exchange, [], "")
            finally:
                await exchange.close()

        ctx        = last_ctx
        btc_icon   = "🟢" if ctx.btc_is_bullish() else ("🔴" if ctx.btc_is_bearish() else "⚪")
        daily_icon = "🟢" if ctx.btc_trend_daily == "BULL" else ("🔴" if ctx.btc_trend_daily == "BEAR" else "⚪")
        fg_icon    = "😱" if ctx.is_extreme_fear() else ("🤑" if ctx.is_extreme_greed() else "😐")
        price_str = f"${ctx.btc_price:,.0f}" if ctx.btc_price and ctx.btc_price > 0 else "fetching..."
        change_val = ctx.btc_change_24h or 0
        change_str = f"up {change_val:.1f}pct" if change_val >= 0 else f"down {abs(change_val):.1f}pct"
        fg_bar     = "█" * (ctx.fear_greed // 10) + "░" * (10 - ctx.fear_greed // 10)

        if ctx.btc_is_bearish():            verdict = "🔴 BTC bearish — all alt LONGs blocked"
        elif ctx.btc_is_bullish() and ctx.fear_greed < 50: verdict = "🟢 BTC bullish + Fear = strong LONG environment"
        elif ctx.is_extreme_greed():        verdict = "⚠️ Extreme Greed — reduce position sizes"
        elif ctx.is_extreme_fear():         verdict = "💡 Extreme Fear — historically best LONG zone"
        else:                               verdict = "⚪ Neutral — follow signal scores"

        report = (
            "📊 *MARKET BRIEFING*\n"
            f"_{ctx.fetched_at}_\n\n"
            f"₿ BTC: `{price_str}` ({change_str} 24h)\n"
            f"4H: {btc_icon} `{ctx.btc_trend_4h}` | Daily: {daily_icon} `{ctx.btc_trend_daily}`\n\n"
            f"F&G: `{ctx.fear_greed}/100` {fg_icon} {ctx.fear_greed_label}\n"
            f"`{fg_bar}`\n"
            f"BTC Dom: `{ctx.btc_dominance:.1f}pct` | L/S: `{ctx.ls_ratio:.2f}`\n"
            f"OI: `{'up' if ctx.oi_change_pct>=0 else 'down'} {abs(ctx.oi_change_pct):.1f}pct`\n\n"
        )
        if ctx.macro_event_today:
            report += f"🚨 *MACRO: {ctx.macro_event_name}* ({ctx.macro_event_impact})\nReduce position sizes today.\n\n"
        else:
            report += "✅ No major macro events today\n\n"
        report += f"*Verdict:* {verdict}"

        try:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=report, parse_mode="Markdown")
        except Exception:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=report.replace("*","").replace("`","").replace("_",""))
    except Exception as e:
        logger.error(f"/briefing: {e}", exc_info=True)
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Briefing failed. Try /signals first then /briefing.")
    finally:
        try:
            await msg.delete()
        except Exception:
            pass


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscribers.discard(update.effective_chat.id)
    save_json(SUBSCRIBERS_FILE, list(subscribers))
    await update.message.reply_text("🔕 Unsubscribed from auto-alerts. Use /start to resubscribe.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id  = str(update.effective_chat.id)
    btc_str  = f"${last_ctx.btc_price:,.0f} ({last_ctx.btc_trend_4h})" if last_ctx else "Run /signals first"
    bal_str  = f"${balances[chat_id]:,.2f}" if chat_id in balances else "Not set — /setbalance"
    trades   = [t for t in get_user_trades(chat_id) if not t.get("sl_hit")]
    news_str = "Active" if os.getenv("CRYPTOPANIC_TOKEN") else "Add CRYPTOPANIC_TOKEN to enable"
    await update.message.reply_text(
        "✅ *Bot Status: Online — v5.0 FINAL*\n\n"
        f"Your balance     : `{bal_str}`\n"
        f"Active trades    : `{len(trades)}` being monitored\n"
        f"Subscribers      : `{len(subscribers)}`\n"
        f"Signal scan      : every `20 min` (opportunity-only alerts)\n"
        f"Trade monitor    : every `5 min`\n"
        f"Auto threshold   : `{AUTO_THRESHOLD}%`\n"
        f"Manual threshold : `{MANUAL_THRESHOLD}%`\n"
        f"Max signals      : `{MAX_SIGNALS}` per scan\n"
        f"Hard filters     : `6`\n"
        f"Scoring pillars  : `13` (130pts max)\n"
        f"BTC last seen    : `{btc_str}`\n"
        f"News intel       : `{news_str}`",
        parse_mode="Markdown"
    )


# ─── Background Jobs ──────────────────────────────────────────────────────────

async def opportunity_scan_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Scans every 20 minutes but ONLY sends alert if a qualifying signal is found.
    No noise. No 'no signals found' messages. Pure opportunity alerts only.
    """
    if not subscribers:
        return
    logger.info(f"Opportunity scan for {len(subscribers)} subscriber(s)...")
    for chat_id in list(subscribers):
        sent = await scan_and_send(context.bot, chat_id, threshold=AUTO_THRESHOLD, is_auto=True)
        if sent:
            logger.info(f"Signal alert sent to {chat_id}")
        await asyncio.sleep(1)


async def trade_monitor_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Runs every 5 minutes.
    Checks every user's open trades and fires alerts if needed.
    """
    for chat_id, trades in list(open_trades.items()):
        active = [t for t in trades if not t.get("sl_hit") and not t.get("tp2_hit")]
        if not active:
            continue
        logger.info(f"Monitoring {len(active)} trade(s) for {chat_id}")
        updated_trades = []
        for trade in trades:
            if not trade.get("sl_hit") and not trade.get("tp2_hit"):
                updated = await monitor_trade(context.bot, chat_id, trade)
                updated_trades.append(updated)
            else:
                updated_trades.append(trade)
        open_trades[chat_id] = updated_trades
        save_json(TRADES_FILE, open_trades)
        await asyncio.sleep(0.5)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not TOKEN:
        logger.error("BOT_TOKEN missing!")
        return

    app = ApplicationBuilder().token(TOKEN).read_timeout(30).write_timeout(30).build()

    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("signals",    signals))
    app.add_handler(CommandHandler("top",        top))
    app.add_handler(CommandHandler("briefing",   briefing))
    app.add_handler(CommandHandler("setbalance", setbalance))
    app.add_handler(CommandHandler("addtrade",   addtrade))
    app.add_handler(CommandHandler("mytrades",   mytrades))
    app.add_handler(CommandHandler("closetrade", closetrade))
    app.add_handler(CommandHandler("learn",      learn))
    app.add_handler(CommandHandler("stop",       stop_cmd))
    app.add_handler(CommandHandler("status",     status))
    app.add_handler(CallbackQueryHandler(learn_callback, pattern="^learn_"))

    # Opportunity-only scan every 20 min (no alert if nothing qualifies)
    app.job_queue.run_repeating(opportunity_scan_job, interval=3600, first=300)

    # Trade monitor every 5 min
    app.job_queue.run_repeating(trade_monitor_job, interval=300, first=30)

    logger.info(f"QuestLife Bot v5.0 FINAL | {len(subscribers)} subscribers | Trade monitor active")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
