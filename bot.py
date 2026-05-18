import os
import json
import time
import logging
import asyncio
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    CallbackQueryHandler, ContextTypes
)

from engine import get_top_signals, MANUAL_THRESHOLD, MAX_SIGNALS

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
CTX_CACHE_SECS   = 1800  # 30 min briefing cache


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
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Save failed {path}: {e}")

subscribers    = set(load_json(SUBSCRIBERS_FILE, []))
balances       = load_json(BALANCES_FILE, {})
open_trades    = load_json(TRADES_FILE, {})
last_scan_time = {}
last_ctx       = None
last_ctx_time  = 0.0


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


# ─── BTC Price Fetch (lightweight) ───────────────────────────────────────────

async def fetch_btc_trend_lightweight():
    """
    Fetch ONLY BTC 4H trend using direct REST calls.
    No ccxt, no market loading, no heavy scan.
    Uses same endpoint as trade monitor — single price call.
    Safe to call every 30 minutes without ban risk.
    """
    try:
        import aiohttp
        import pandas as pd
        from ta.trend import EMAIndicator

        # Single REST call for BTC 4H candles
        url = "https://fapi.binance.com/fapi/v1/klines"
        params = {
            "symbol"   : "BTCUSDT",
            "interval" : "4h",
            "limit"    : 60,
        }
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8)
        ) as s:
            async with s.get(url, params=params) as r:
                if r.status != 200:
                    return None, 0.0
                candles = await r.json()

        if not candles or len(candles) < 50:
            return None, 0.0

        close  = pd.Series([float(c[4]) for c in candles])
        ema20  = EMAIndicator(close=close, window=20).ema_indicator().iloc[-1]
        ema50  = EMAIndicator(close=close, window=50).ema_indicator().iloc[-1]
        price  = float(close.iloc[-1])

        if ema20 > ema50 * 1.001:   trend = "BULL"
        elif ema20 < ema50 * 0.999: trend = "BEAR"
        else:                        trend = "NEUTRAL"

        return trend, price

    except Exception as e:
        logger.warning(f"Lightweight BTC trend fetch failed: {e}")
        return None, 0.0


# ─── Market Context Builder ───────────────────────────────────────────────────

async def build_context_standalone():
    """
    Build full market context for /briefing.
    Called only when cache is older than 30 minutes.
    """
    global last_ctx, last_ctx_time
    try:
        import ccxt.async_support as ccxt_lib
        from market_intel import build_market_context

        exchange = ccxt_lib.binance({
            "options"        : {"defaultType": "future"},
            "enableRateLimit": True,
        })
        try:
            logger.info("Building market context for /briefing...")
            await exchange.load_markets()
            ctx = await build_market_context(
                exchange, [], os.getenv("CRYPTOPANIC_TOKEN", "")
            )
            if ctx is not None:
                last_ctx      = ctx
                last_ctx_time = time.time()
                logger.info(f"Context OK: BTC ${ctx.btc_price:,.0f} | 4H:{ctx.btc_trend_4h}")
            else:
                logger.warning("build_market_context returned None")
        finally:
            try:
                await exchange.close()
            except Exception:
                pass
    except Exception as e:
        logger.error(f"build_context_standalone failed: {e}", exc_info=True)


# ─── Signal Formatters ────────────────────────────────────────────────────────

def fmt_signal(res, rank, balance=None):
    medal   = {0:"🥇",1:"🥈",2:"🥉"}.get(rank,"💎")
    score   = res["score"]
    fr_str  = f"{res['funding_rate']:+.4f}%" if res.get("funding_rate") is not None else "N/A"
    quality = (
        "🔥 ELITE"  if score >= 110 else
        "⚡ STRONG" if score >= 95  else
        "✅ GOOD"   if score >= 85  else
        "📊 VALID"
    )
    news_ic = {"POSITIVE":"📰✅","NEGATIVE":"📰❌"}.get(res.get("news",""),"")
    sym     = res["symbol"].replace("/USDT","")
    dir_    = "LONG" if "LONG" in res["dir"] else "SHORT"

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
    out += f"\n_Monitor: /addtrade {sym} {dir_} {fmt(res['entry'])} {fmt(res['tp2'])} {fmt(res['sl'])}_"
    out += "\n" + "─" * 30 + "\n\n"
    return out


def fmt_guide(res):
    side = "BUY / LONG" if "LONG" in res["dir"] else "SELL / SHORT"
    icon = "🟢" if "LONG" in res["dir"] else "🔴"
    sym  = res["symbol"].replace("/USDT","")
    dir_ = "LONG" if "LONG" in res["dir"] else "SHORT"
    return (
        f"📖 *HOW TO ENTER: {res['symbol']}*\n\n"
        f"*Step 1 — Open Binance App*\n"
        f"Futures → USDT-M → search `{sym}`\n\n"
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
        f"Move SL to entry `{fmt(res['entry'])}` → trade is now risk-free\n\n"
        f"*If SL hits:* Accept the loss. Do not add more money.\n\n"
        f"*To monitor this trade:*\n"
        f"`/addtrade {sym} {dir_} {fmt(res['entry'])} {fmt(res['tp2'])} {fmt(res['sl'])}`"
    )


def build_header(results, ctx, threshold):
    ts   = datetime.now(timezone.utc).strftime("%H:%M UTC")
    body = f"🚀 *STRICT SIGNALS — PROFESSIONAL SCAN*  `{ts}`\n"
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


# ─── Core Scan & Send ─────────────────────────────────────────────────────────

async def scan_and_send(bot, chat_id, threshold, is_auto=False):
    global last_ctx, last_ctx_time
    try:
        results, ctx = await get_top_signals()

        from engine import is_banned, get_ban_remaining_mins
        if is_banned():
            if not is_auto:
                mins = get_ban_remaining_mins()
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"⏸ *Binance API temporarily rate-limited*\n\n"
                        f"Auto-resumes in approximately `{mins} minutes`.\n"
                        f"No action needed — bot will recover automatically."
                    ),
                    parse_mode="Markdown"
                )
            return

        if ctx and ctx.btc_price > 0:
            last_ctx      = ctx
            last_ctx_time = time.time()

        filtered = [s for s in results if s["score"] >= threshold]

        if not filtered:
            if not is_auto:
                btc_s = ctx.btc_trend_4h if ctx else "unknown"
                macro = f"\nMacro event: *{ctx.macro_event_name}*" if ctx and ctx.macro_event_today else ""
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
            return

        balance = balances.get(str(chat_id), 0)

        header = build_header(filtered, ctx, threshold)
        try:
            await bot.send_message(chat_id=chat_id, text=header, parse_mode="Markdown")
        except Exception:
            await bot.send_message(chat_id=chat_id, text=header.replace("*","").replace("`","").replace("_",""))

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

    except Exception as e:
        logger.error(f"scan_and_send [{chat_id}]: {e}", exc_info=True)
        if not is_auto:
            await bot.send_message(
                chat_id=chat_id,
                text="⚠️ Scan error. Please try again in a moment."
            )


# ─── Trade Monitor ────────────────────────────────────────────────────────────

def get_user_trades(chat_id):
    return open_trades.get(str(chat_id), [])

def save_user_trades(chat_id, trades):
    open_trades[str(chat_id)] = trades
    save_json(TRADES_FILE, open_trades)


async def fetch_current_price(symbol):
    try:
        import aiohttp
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as s:
            async with s.get(
                "https://fapi.binance.com/fapi/v1/ticker/price",
                params={"symbol": f"{symbol}USDT"}
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    return float(data["price"])
    except Exception as e:
        logger.warning(f"Price fetch failed for {symbol}: {e}")
    return None


async def monitor_trade(bot, chat_id, trade):
    symbol    = trade["symbol"]
    direction = trade["direction"]
    entry     = trade["entry"]
    tp1       = trade["tp1"]
    tp2       = trade["tp2"]
    sl        = trade["sl"]
    tp1_hit   = trade.get("tp1_hit", False)
    tp2_hit   = trade.get("tp2_hit", False)

    price = await fetch_current_price(symbol)
    if not price:
        return trade

    alerts  = []
    updated = False

    if direction == "LONG":
        pnl_pct = ((price - entry) / entry) * 100

        if not tp1_hit and price >= tp1:
            alerts.append(
                f"🎉 *TP1 HIT — {symbol} LONG*\n\n"
                f"Price reached `{fmt(price)}`\n\n"
                f"1. Close 40% of position now\n"
                f"2. Move SL up to entry `{fmt(entry)}`\n"
                f"3. Trade is now risk-free\n\n"
                f"Profit: `+{pnl_pct:.2f}%` 💰"
            )
            trade["tp1_hit"] = True
            updated = True

        elif tp1_hit and not tp2_hit and price >= tp2:
            alerts.append(
                f"🎉 *TP2 HIT — {symbol} LONG*\n\n"
                f"Price reached `{fmt(price)}`\n\n"
                f"1. Close another 40%\n"
                f"2. Let 20% run to TP3\n"
                f"3. Move SL to `{fmt(tp1)}`\n\n"
                f"Profit: `+{pnl_pct:.2f}%` 🚀"
            )
            trade["tp2_hit"] = True
            updated = True

        sl_dist_pct = ((price - sl) / entry) * 100

        if price <= sl:
            alerts.append(
                f"🚨 *STOP LOSS HIT — {symbol} LONG*\n\n"
                f"Price: `{fmt(price)}` | SL: `{fmt(sl)}`\n\n"
                f"Close position NOW if not done.\n"
                f"Loss: `{pnl_pct:.2f}%`\n\n"
                f"_Small loss is fine. Bot will find the next setup._"
            )
            trade["sl_hit"] = True
            updated = True

        elif -sl_dist_pct < 1.0 and price > sl:
            alerts.append(
                f"⚠️ *SL WARNING — {symbol} LONG*\n\n"
                f"Price `{fmt(price)}` is within 1% of SL `{fmt(sl)}`\n"
                f"Watch closely. Consider closing manually."
            )

        if last_ctx and last_ctx.btc_is_bearish() and not trade.get("btc_warn_sent"):
            alerts.append(
                f"⚠️ *BTC TURNED BEARISH — {symbol} LONG at risk*\n\n"
                f"Consider closing 50% and tightening SL.\n"
                f"P&L: `{pnl_pct:+.2f}%`"
            )
            trade["btc_warn_sent"] = True
            updated = True

    elif direction == "SHORT":
        pnl_pct = ((entry - price) / entry) * 100

        if not tp1_hit and price <= tp1:
            alerts.append(
                f"🎉 *TP1 HIT — {symbol} SHORT*\n\n"
                f"Price dropped to `{fmt(price)}`\n\n"
                f"1. Close 40% of position\n"
                f"2. Move SL to entry `{fmt(entry)}`\n"
                f"3. Trade is now risk-free\n\n"
                f"Profit: `+{pnl_pct:.2f}%` 💰"
            )
            trade["tp1_hit"] = True
            updated = True

        elif tp1_hit and not tp2_hit and price <= tp2:
            alerts.append(
                f"🎉 *TP2 HIT — {symbol} SHORT*\n\n"
                f"Price dropped to `{fmt(price)}`\n\n"
                f"Close another 40%. Let 20% run.\n"
                f"Profit: `+{pnl_pct:.2f}%` 🚀"
            )
            trade["tp2_hit"] = True
            updated = True

        sl_dist_pct = ((sl - price) / entry) * 100

        if price >= sl:
            alerts.append(
                f"🚨 *STOP LOSS HIT — {symbol} SHORT*\n\n"
                f"Price: `{fmt(price)}` | SL: `{fmt(sl)}`\n\n"
                f"Close position NOW if not done.\n"
                f"Loss: `{pnl_pct:.2f}%`"
            )
            trade["sl_hit"] = True
            updated = True

        elif sl_dist_pct < 1.0 and price < sl:
            alerts.append(
                f"⚠️ *SL WARNING — {symbol} SHORT*\n\n"
                f"Price `{fmt(price)}` is within 1% of SL `{fmt(sl)}`\n"
                f"Watch closely."
            )

        if last_ctx and last_ctx.btc_is_bullish() and not trade.get("btc_warn_sent"):
            alerts.append(
                f"⚠️ *BTC TURNED BULLISH — {symbol} SHORT at risk*\n\n"
                f"Consider closing 50% and tightening SL.\n"
                f"P&L: `{pnl_pct:+.2f}%`"
            )
            trade["btc_warn_sent"] = True
            updated = True

    for alert in alerts:
        try:
            await bot.send_message(
                chat_id=int(chat_id), text=alert, parse_mode="Markdown"
            )
        except Exception:
            await bot.send_message(
                chat_id=int(chat_id),
                text=alert.replace("*","").replace("`","").replace("_","")
            )
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
        "• Scans top 25 Binance Futures pairs on demand\n"
        "• 6 hard filters + 13 pillar scoring (130pts max)\n"
        "• LONG signals when BTC 4H bullish\n"
        "• SHORT signals when BTC 4H bearish\n"
        "• Monitors your open trades 24/7\n"
        "• Alerts you when BTC trend flips\n"
        "• Step-by-step entry guide per signal\n"
        "• Exact position size based on your balance\n\n"
        "*Setup — do these first:*\n"
        "1. /setbalance 500 — enter your USDT balance\n"
        "2. /learn — understand the basics\n\n"
        "📋 *Commands:*\n"
        "/signals — scan now (manual only)\n"
        "/top — best signal now\n"
        "/briefing — market overview\n"
        "/addtrade — register trade to monitor\n"
        "/mytrades — see monitored trades\n"
        "/closetrade — remove trade from monitor\n"
        "/setbalance 500 — set your balance\n"
        "/learn — trading education\n"
        "/status — bot info\n"
        "/stop — unsubscribe\n\n"
        "⚠️ _Educational only. Not financial advice. Always use stop loss._",
        parse_mode="Markdown"
    )


async def setbalance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    try:
        if not context.args:
            await update.message.reply_text(
                "Usage: /setbalance 500\nReplace 500 with your actual USDT balance."
            )
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
            f"After 10 losing trades in a row you still have `${amount * 0.817:,.0f}` left.",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text("Invalid number. Example: /setbalance 500")


async def addtrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    args    = context.args

    if not args or len(args) < 5:
        await update.message.reply_text(
            "Usage:\n`/addtrade ICPUSDT LONG 3.57 3.85 3.42`\n\n"
            "Fields: symbol direction entry tp sl\n\n"
            "Examples:\n"
            "`/addtrade BTCUSDT LONG 67000 70000 65000`\n"
            "`/addtrade ETHUSDT SHORT 2400 2200 2500`",
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

        tp1 = tp1_opt if tp1_opt else (
            entry + (tp_main - entry) * 0.5 if direction == "LONG"
            else entry - (entry - tp_main) * 0.5
        )

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

        trades   = get_user_trades(chat_id)
        existing = [t for t in trades if t["symbol"] == symbol and not t.get("sl_hit")]
        if existing:
            await update.message.reply_text(
                f"You already have an open {symbol} trade being monitored.\n"
                f"Use /closetrade {symbol} first to remove it."
            )
            return

        trades.append(trade)
        save_user_trades(chat_id, trades)

        sl_pct         = abs(entry - sl) / entry * 100
        tp_pct         = abs(tp_main - entry) / entry * 100
        direction_word = "rises" if direction == "LONG" else "drops"

        await update.message.reply_text(
            f"✅ *Trade Registered — Monitoring 24/7*\n\n"
            f"Symbol    : `{symbol}/USDT`\n"
            f"Direction : `{direction}`\n"
            f"Entry     : `{fmt(entry)}`\n"
            f"TP1       : `{fmt(tp1)}` _(close 40%)_\n"
            f"TP2       : `{fmt(tp_main)}` _(close 40%)_\n"
            f"SL        : `{fmt(sl)}`\n"
            f"Risk      : `{sl_pct:.2f}%` | Target: `{tp_pct:.2f}%`\n\n"
            f"*Alerts when price {direction_word} to TP1/TP2,\n"
            f"within 1% of SL, or BTC trend flips.*\n\n"
            f"_Monitoring every 5 minutes._",
            parse_mode="Markdown"
        )

    except (ValueError, IndexError):
        await update.message.reply_text(
            "Invalid format. Example:\n`/addtrade ICPUSDT LONG 3.57 3.85 3.42`",
            parse_mode="Markdown"
        )


async def mytrades(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        price     = await fetch_current_price(symbol)
        if price:
            pnl     = ((price - entry) / entry * 100) if direction == "LONG" else ((entry - price) / entry * 100)
            pnl_str = f"`{pnl:+.2f}%`"
            p_str   = f"`{fmt(price)}`"
        else:
            pnl_str = "N/A"
            p_str   = "N/A"
        msg += (
            f"{icon} *{symbol}/USDT {direction}*\n"
            f"Entry : `{fmt(entry)}` | Now: {p_str} | P&L: {pnl_str}\n"
            f"SL    : `{fmt(t['sl'])}` | TP2: `{fmt(t['tp2'])}`\n"
            f"TP1   : {tp1_hit} | TP2: {tp2_hit}\n"
            f"_/closetrade {symbol} to remove_\n\n"
        )

    try:
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(
            msg.replace("*","").replace("`","").replace("_","")
        )


async def closetrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text(
            "Usage: /closetrade ICPUSDT\nOr /closetrade ALL to close all."
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
    if before == len(trades):
        await update.message.reply_text(f"No monitored trade found for {symbol}.")
        return

    save_user_trades(chat_id, trades)
    await update.message.reply_text(
        f"✅ *{symbol}/USDT removed from monitoring.*",
        parse_mode="Markdown"
    )


async def learn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args).lower() if context.args else ""
    lessons = {
        "leverage": (
            "📚 *Leverage*\n\n"
            "Multiplies your position. 3x on $100 = control $300.\n"
            "Also multiplies losses. Bot recommends 1-5x max.\n"
            "10x leverage = 10% move against you = total loss."
        ),
        "sl": (
            "📚 *Stop Loss*\n\n"
            "Set it when you open the trade. NEVER remove it.\n"
            "It keeps you in the game long-term."
        ),
        "futures": (
            "📚 *Futures*\n\n"
            "LONG = bet price goes UP.\n"
            "SHORT = bet price goes DOWN.\n"
            "Always use USDT-Margined on Binance."
        ),
        "tp": (
            "📚 *Take Profit*\n\n"
            "TP1 = close 40%, TP2 = close 40%, TP3 = let 20% run.\n"
            "After TP1 hits: move SL to entry = risk-free trade."
        ),
        "position": (
            "📚 *Position Sizing*\n\n"
            "Never risk more than 2% per trade.\n"
            "$500 balance = $10 max risk.\n"
            "Set balance: /setbalance 500"
        ),
        "rsi": (
            "📚 *RSI*\n\n"
            "Above 68 = overbought, bot rejects LONG.\n"
            "Below 25 = oversold, bot rejects SHORT.\n"
            "Best LONG entries: RSI 35-52."
        ),
        "monitor": (
            "📚 *Trade Monitor*\n\n"
            "`/addtrade ICPUSDT LONG 3.57 3.85 3.42`\n\n"
            "Bot watches 24/7, alerts on TP hits, SL danger, BTC flip."
        ),
    }
    if topic and topic in lessons:
        await update.message.reply_text(lessons[topic], parse_mode="Markdown")
    else:
        keyboard = [
            [InlineKeyboardButton("📊 Leverage",      callback_data="learn_leverage"),
             InlineKeyboardButton("🛡 Stop Loss",     callback_data="learn_sl")],
            [InlineKeyboardButton("📈 Futures",       callback_data="learn_futures"),
             InlineKeyboardButton("🎯 Take Profit",   callback_data="learn_tp")],
            [InlineKeyboardButton("💰 Position Size", callback_data="learn_position"),
             InlineKeyboardButton("📉 RSI",           callback_data="learn_rsi")],
            [InlineKeyboardButton("👁 Trade Monitor", callback_data="learn_monitor")],
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
        "leverage": "📚 *Leverage*\n\nMultiplies your position and losses equally.\nBot recommends 1-5x max.",
        "sl":       "📚 *Stop Loss*\n\nSet it when you open the trade. NEVER remove it.",
        "futures":  "📚 *Futures*\n\nLONG = price goes UP. SHORT = price goes DOWN.\nAlways use USDT-Margined on Binance.",
        "tp":       "📚 *Take Profit*\n\nTP1 = close 40%, TP2 = close 40%, TP3 = let 20% run.\nAfter TP1: move SL to entry = risk-free.",
        "position": "📚 *Position Sizing*\n\nNever risk more than 2% per trade.\n$500 balance = $10 max risk.\n/setbalance 500",
        "rsi":      "📚 *RSI*\n\nAbove 68 = bot rejects LONG.\nBelow 25 = bot rejects SHORT.\nBest LONG entries: RSI 35-52.",
        "monitor":  "📚 *Trade Monitor*\n\n`/addtrade ICPUSDT LONG 3.57 3.85 3.42`\n\nAlerts on TP hits and SL danger.",
    }
    text = lessons.get(topic, "Topic not found. Try /learn")
    try:
        await context.bot.send_message(
            chat_id=query.message.chat_id, text=text, parse_mode="Markdown"
        )
    except Exception:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=text.replace("*","").replace("`","")
        )


async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    now     = asyncio.get_event_loop().time()
    if chat_id in last_scan_time and (now - last_scan_time[chat_id]) < COOLDOWN_SECONDS:
        wait = int(COOLDOWN_SECONDS - (now - last_scan_time[chat_id]))
        await update.message.reply_text(f"⏳ Please wait {wait}s before scanning again.")
        return
    last_scan_time[chat_id] = now
    msg = await update.message.reply_text(
        "🔎 Running strict 13-pillar scan across top 25 pairs..."
    )
    await scan_and_send(context.bot, chat_id, threshold=MANUAL_THRESHOLD)
    try:
        await msg.delete()
    except Exception:
        pass


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🏆 Finding the single best signal...")
    try:
        results, ctx = await get_top_signals()

        from engine import is_banned, get_ban_remaining_mins
        if is_banned():
            mins = get_ban_remaining_mins()
            await update.message.reply_text(
                f"⏸ *Binance API rate-limited*\n\nAuto-resumes in `{mins} minutes`.",
                parse_mode="Markdown"
            )
            return

        global last_ctx, last_ctx_time
        if ctx and ctx.btc_price > 0:
            last_ctx      = ctx
            last_ctx_time = time.time()

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
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=text, parse_mode="Markdown"
                    )
                except Exception:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=text.replace("*","").replace("`","").replace("_","")
                    )
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
    global last_ctx, last_ctx_time
    msg = await update.message.reply_text("📊 Fetching market intelligence...")
    try:
        ctx_age       = time.time() - last_ctx_time
        needs_refresh = (
            last_ctx is None or
            last_ctx.btc_price == 0 or
            ctx_age > CTX_CACHE_SECS
        )

        if needs_refresh:
            logger.info(f"Briefing: refreshing context (age={ctx_age:.0f}s)")
            await build_context_standalone()
        else:
            logger.info(f"Briefing: using cached context (age={ctx_age:.0f}s)")

        if last_ctx is None:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    "⚠️ Could not fetch market data right now.\n\n"
                    "Please try /signals first — that always works.\n"
                    "After /signals completes, /briefing will work from cache."
                )
            )
            return

        ctx        = last_ctx
        btc_icon   = "🟢" if ctx.btc_is_bullish() else ("🔴" if ctx.btc_is_bearish() else "⚪")
        daily_icon = "🟢" if ctx.btc_trend_daily == "BULL" else ("🔴" if ctx.btc_trend_daily == "BEAR" else "⚪")
        fg_icon    = "😱" if ctx.is_extreme_fear() else ("🤑" if ctx.is_extreme_greed() else "😐")
        price_str  = f"${ctx.btc_price:,.0f}" if ctx.btc_price and ctx.btc_price > 0 else "N/A"
        change_val = ctx.btc_change_24h or 0
        change_str = f"up {change_val:.1f}pct" if change_val >= 0 else f"down {abs(change_val):.1f}pct"
        fg_bar     = "█" * (ctx.fear_greed // 10) + "░" * (10 - ctx.fear_greed // 10)
        ls_str     = f"{ctx.ls_ratio:.2f}" if ctx.ls_ratio else "N/A"
        oi_val     = ctx.oi_change_pct or 0
        oi_str     = f"up {oi_val:.1f}pct" if oi_val >= 0 else f"down {abs(oi_val):.1f}pct"
        dom_str    = f"{ctx.btc_dominance:.1f}pct" if ctx.btc_dominance else "N/A"
        cache_mins = int(ctx_age / 60) if not needs_refresh else 0

        if ctx.btc_is_bearish():
            verdict = "🔴 BTC bearish — SHORT signals active, LONGs blocked"
        elif ctx.btc_is_bullish() and ctx.fear_greed < 50:
            verdict = "🟢 BTC bullish + Fear = strong LONG environment"
        elif ctx.is_extreme_greed():
            verdict = "⚠️ Extreme Greed — reduce position sizes"
        elif ctx.is_extreme_fear():
            verdict = "💡 Extreme Fear — historically best LONG zone"
        else:
            verdict = "⚪ Neutral — follow signal scores"

        cache_note = f" _(cached {cache_mins}min ago)_" if cache_mins > 0 else ""

        report = (
            f"📊 *MARKET BRIEFING*\n"
            f"_{ctx.fetched_at}_{cache_note}\n\n"
            f"₿ BTC: `{price_str}` ({change_str} 24h)\n"
            f"4H: {btc_icon} `{ctx.btc_trend_4h}` | Daily: {daily_icon} `{ctx.btc_trend_daily}`\n\n"
            f"F&G: `{ctx.fear_greed}/100` {fg_icon} {ctx.fear_greed_label}\n"
            f"`{fg_bar}`\n"
            f"BTC Dom: `{dom_str}` | L/S: `{ls_str}`\n"
            f"OI: `{oi_str}`\n\n"
        )
        if ctx.macro_event_today:
            report += (
                f"🚨 *MACRO EVENT TODAY*\n"
                f"`{ctx.macro_event_name}` ({ctx.macro_event_impact})\n"
                f"Reduce position sizes today.\n\n"
            )
        else:
            report += "✅ No major macro events today\n\n"

        report += f"*Verdict:* {verdict}"

        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=report, parse_mode="Markdown"
            )
        except Exception:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=report.replace("*","").replace("`","").replace("_","")
            )

    except Exception as e:
        logger.error(f"/briefing: {e}", exc_info=True)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ Briefing failed. Try /signals first then /briefing again."
        )
    finally:
        try:
            await msg.delete()
        except Exception:
            pass


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscribers.discard(update.effective_chat.id)
    save_json(SUBSCRIBERS_FILE, list(subscribers))
    await update.message.reply_text(
        "🔕 Unsubscribed. Use /start to resubscribe anytime."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id   = str(update.effective_chat.id)
    btc_str   = (
        f"${last_ctx.btc_price:,.0f} ({last_ctx.btc_trend_4h})"
        if last_ctx and last_ctx.btc_price > 0
        else "Send /briefing first"
    )
    bal_str   = f"${balances[chat_id]:,.2f}" if chat_id in balances else "Not set — /setbalance"
    trades    = [t for t in get_user_trades(chat_id) if not t.get("sl_hit")]
    news_str  = "Active" if os.getenv("CRYPTOPANIC_TOKEN") else "Add CRYPTOPANIC_TOKEN to enable"
    cache_age = int((time.time() - last_ctx_time) / 60) if last_ctx_time > 0 else 0

    from engine import is_banned, get_ban_remaining_mins
    ban_str = (
        f"⏸ Rate-limited ({get_ban_remaining_mins()}min remaining)"
        if is_banned()
        else "✅ Clear"
    )

    await update.message.reply_text(
        "✅ *Bot Status: Online — v5.0 FINAL*\n\n"
        f"Your balance     : `{bal_str}`\n"
        f"Active trades    : `{len(trades)}` being monitored\n"
        f"Subscribers      : `{len(subscribers)}`\n"
        f"Auto scan        : `Disabled` (manual /signals only)\n"
        f"Trade monitor    : `Every 5 min`\n"
        f"BTC trend watch  : `Every 30 min` (alerts on 4H flip)\n"
        f"Binance API      : `{ban_str}`\n"
        f"Context cache    : `{cache_age}min old`\n"
        f"Manual threshold : `{MANUAL_THRESHOLD}%`\n"
        f"Max signals      : `{MAX_SIGNALS}` per scan\n"
        f"Hard filters     : `6`\n"
        f"Scoring pillars  : `13` (130pts max)\n"
        f"BTC last seen    : `{btc_str}`\n"
        f"News intel       : `{news_str}`",
        parse_mode="Markdown"
    )


# ─── Background Jobs ──────────────────────────────────────────────────────────

async def trade_monitor_job(context: ContextTypes.DEFAULT_TYPE):
    """Every 5 min — checks all open trades. Single price fetch per trade."""
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


async def btc_trend_watcher(context: ContextTypes.DEFAULT_TYPE):
    """
    Every 30 min — checks BTC 4H trend using ONE lightweight API call.
    Alerts subscribers ONLY when trend flips from BEAR to BULL.
    Zero ban risk — single klines endpoint call, no heavy scan.
    """
    if not subscribers:
        return

    trend, price = await fetch_btc_trend_lightweight()
    if trend is None:
        return

    # Get previous trend from job data
    prev_trend = context.job.data if context.job.data else "BEAR"

    logger.info(f"BTC trend watcher: {prev_trend} → {trend} | ${price:,.0f}")

    # Alert when flipping from BEAR/NEUTRAL to BULL
    if trend == "BULL" and prev_trend != "BULL":
        logger.info("BTC 4H flipped BULL — alerting all subscribers")
        for chat_id in list(subscribers):
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "🚨 *BTC 4H TREND FLIPPED BULLISH*\n\n"
                        f"BTC is now `BULL` on the 4H chart\n"
                        f"Price: `${price:,.0f}`\n\n"
                        "*This is the signal you were waiting for.*\n\n"
                        "Run /signals NOW — LONG setups may be available\n\n"
                        "_Bot detected this automatically. "
                        "Check /briefing for full market context._"
                    ),
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Trend alert failed for {chat_id}: {e}")
            await asyncio.sleep(0.5)

    # Also alert when flipping from BULL to BEAR (protect open longs)
    elif trend == "BEAR" and prev_trend == "BULL":
        logger.info("BTC 4H flipped BEAR — alerting all subscribers")
        for chat_id in list(subscribers):
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "⚠️ *BTC 4H TREND FLIPPED BEARISH*\n\n"
                        f"BTC is now `BEAR` on the 4H chart\n"
                        f"Price: `${price:,.0f}`\n\n"
                        "LONG signals are now blocked.\n"
                        "If you have open LONG trades, check /mytrades\n\n"
                        "_Consider tightening stop losses on open positions._"
                    ),
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Bear alert failed for {chat_id}: {e}")
            await asyncio.sleep(0.5)

    # Update stored trend
    context.job.data = trend


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

    # Trade monitor — single price fetch per trade, every 5 min
    app.job_queue.run_repeating(
        trade_monitor_job,
        interval=300,
        first=30
    )

    # BTC trend watcher — ONE klines call every 30 min, zero ban risk
    # Alerts you automatically when 4H flips BULL or BEAR
    app.job_queue.run_repeating(
        btc_trend_watcher,
        interval=1800,
        first=60,
        data="BEAR"   # assume BEAR on start, will detect flip correctly
    )

    logger.info(
        f"QuestLife Bot v5.0 FINAL | {len(subscribers)} subscribers | "
        f"Auto scan OFF | Trade monitor ON | BTC trend watcher ON"
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
