import ccxt.async_support as ccxt
import pandas as pd
import pandas_ta as ta
import asyncio

async def fetch_and_analyze(exchange, symbol):
    try:
        # Fetch last 100 hours of data to have enough for indicators
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe='1h', limit=100)
        df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
        
        # --- CALCULATE INDICATORS ---
        df.ta.rsi(length=14, append=True)
        df.ta.ema(length=20, append=True)
        df.ta.ema(length=50, append=True)
        df.ta.atr(length=14, append=True)
        
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        # --- SCORING & TREND LOGIC ---
        score = 0
        direction = "NEUTRAL"
        
        # 1. Trend Detection (EMA 20 > 50 is Bullish)
        if last['EMA_20'] > last['EMA_50']:
            score += 30
            direction = "LONG"
        else:
            score += 30 # Base score for Short
            direction = "SHORT"
        
        # 2. RSI Analysis (Oversold/Overbought)
        if direction == "LONG" and last['RSI_14'] < 40: score += 40
        if direction == "SHORT" and last['RSI_14'] > 60: score += 40
        
        # --- TRADE PARAMETERS (TP/SL/LEVERAGE) ---
        entry = last['close']
        atr = last['ATRr_14']
        
        if score >= 60:
            # Set SL and TP based on ATR (Volatility)
            if direction == "LONG":
                sl = entry - (atr * 1.5)
                tp = entry + (atr * 3)
                icon = "🟢"
            else:
                sl = entry + (atr * 1.5)
                tp = entry - (atr * 3)
                icon = "🔴"
            
            # Risk-based Leverage: Aiming to lose only 1% of balance if SL is hit
            sl_pct = abs(entry - sl) / entry
            lev = min(20, round(0.01 / sl_pct)) # Cap leverage at 20x for safety
            
            return {
                "symbol": symbol, 
                "score": score, 
                "dir": f"{icon} {direction}",
                "entry": entry, 
                "tp": tp, 
                "sl": sl, 
                "lev": lev
            }
        return None
    except Exception as e:
        print(f"Error analyzing {symbol}: {e}")
        return None

async def get_top_signals():
    # Initialize Binance Futures
    exchange = ccxt.binance({'options': {'defaultType': 'future'}})
    
    try:
        markets = await exchange.load_markets()
        # Filter for top USDT pairs (scanning 50 for stability)
        symbols = [s for s in markets if '/USDT' in s][:50]
        
        # Run scans in parallel for high speed
        tasks = [fetch_and_analyze(exchange, s) for s in symbols]
        all_results = await asyncio.gather(*tasks)
        
        # Filter out None values and sort by highest confidence score
        signals = [s for s in all_results if s is not None]
        return sorted(signals, key=lambda x: x['score'], reverse=True)[:10]
    
    finally:
        await exchange.close()
