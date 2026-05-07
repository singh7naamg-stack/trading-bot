import ccxt.async_support as ccxt
import pandas as pd
import asyncio
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange

async def fetch_and_analyze(exchange, symbol):
    try:
        # Fetching 100 candles (1h timeframe) for trend analysis
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe='1h', limit=100)
        if not ohlcv or len(ohlcv) < 50: return None
        
        df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
        
        # --- Indicator Calculations ---
        df['RSI_14'] = RSIIndicator(close=df['close'], window=14).rsi()
        df['EMA_20'] = EMAIndicator(close=df['close'], window=20).ema_indicator()
        df['EMA_50'] = EMAIndicator(close=df['close'], window=50).ema_indicator()
        df['ATR_14'] = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=14).average_true_range()
        
        last = df.iloc[-1]
        
        # --- Scoring Logic ---
        score = 0
        direction = "NEUTRAL"
        
        # Trend Check (30 Points)
        if last['EMA_20'] > last['EMA_50']:
            score += 30
            direction = "LONG"
        else:
            score += 30
            direction = "SHORT"
            
        # Momentum Check (40 Points)
        if direction == "LONG" and last['RSI_14'] < 45: 
            score += 40
        elif direction == "SHORT" and last['RSI_14'] > 55: 
            score += 40
        
        entry = last['close']
        atr = last['ATR_14']
        
        # Only return if the score is worth looking at
        if score >= 60:
            if direction == "LONG":
                sl = entry - (atr * 1.5)
                tp = entry + (atr * 3)
                icon = "🟢"
            else:
                sl = entry + (atr * 1.5)
                tp = entry - (atr * 3)
                icon = "🔴"
            
            # Risk Management: Calculate leverage for ~1% risk
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
        # Filter for top 50 USDT Futures pairs
        symbols = [s for s in markets if '/USDT' in s and ':' not in s][:50]
        
        tasks = [fetch_and_analyze(exchange, s) for s in symbols]
        all_results = await asyncio.gather(*tasks)
        
        signals = [s for s in all_results if s is not None]
        return sorted(signals, key=lambda x: x['score'], reverse=True)
    finally:
        await exchange.close()
