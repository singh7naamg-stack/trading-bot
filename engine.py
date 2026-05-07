import ccxt.async_support as ccxt
import pandas as pd
import asyncio
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange

async def fetch_and_analyze(exchange, symbol):
    try:
        # Fetching 100 candles (1h timeframe)
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe='1h', limit=100)
        if not ohlcv or len(ohlcv) < 50: return None
        
        df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
        
        # Indicators
        df['RSI_14'] = RSIIndicator(close=df['close'], window=14).rsi()
        df['EMA_20'] = EMAIndicator(close=df['close'], window=20).ema_indicator()
        df['EMA_50'] = EMAIndicator(close=df['close'], window=50).ema_indicator()
        df['ATR_14'] = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=14).average_true_range()
        
        last = df.iloc[-1]
        score = 0
        direction = "NEUTRAL"
        
        # Trend (30 pts)
        if last['EMA_20'] > last['EMA_50']:
            score += 30
            direction = "LONG"
        else:
            score += 30
            direction = "SHORT"
            
        # Momentum (40 pts)
        if direction == "LONG" and last['RSI_14'] < 45: score += 40
        elif direction == "SHORT" and last['RSI_14'] > 55: score += 40
        
        if score >= 60:
            entry = last['close']
            atr = last['ATR_14']
            if direction == "LONG":
                sl, tp, icon = entry - (atr * 1.5), entry + (atr * 3), "🟢"
            else:
                sl, tp, icon = entry + (atr * 1.5), entry - (atr * 3), "🔴"
            
            sl_pct = abs(entry - sl) / entry
            lev = min(20, round(0.01 / sl_pct)) if sl_pct > 0 else 1
            
            return {
                "symbol": symbol, "score": score, "dir": f"{icon} {direction}",
                "entry": entry, "tp": tp, "sl": sl, "lev": lev
            }
        return None
    except Exception:
        return None

async def get_top_signals():
    # Dedicated Binance connection
    exchange = ccxt.binance({'options': {'defaultType': 'future'}})
    try:
        markets = await exchange.load_markets()
        # Reduce to top 30 pairs to prevent IP ban
        symbols = [s for s in markets if '/USDT' in s and ':' not in s][:30]
        
        signals = []
        for s in symbols:
            res = await fetch_and_analyze(exchange, s)
            if res:
                signals.append(res)
            # 🛑 CRITICAL: This 0.2s pause prevents the "418 Too Many Requests" error
            await asyncio.sleep(0.2) 
            
        return sorted(signals, key=lambda x: x['score'], reverse=True)
    finally:
        await exchange.close()
