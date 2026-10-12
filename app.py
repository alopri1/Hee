"""EURUSD Engine Pro — research dashboard, not a trading robot or financial advice."""
from flask import Flask, jsonify, render_template, request
import os, time, threading, math
from datetime import datetime, timezone
import requests
import pandas as pd
import numpy as np

app = Flask(__name__)
APP_NAME = "EURUSD Engine Pro"
CACHE_TTL = max(10, min(int(os.getenv("CACHE_TTL_SECONDS", "45")), 600))
_cache, _lock = {}, threading.Lock()
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125 Safari/537.36"}

class MarketDataError(Exception):
    pass

def get_candles(symbol="EURUSD=X", interval="5m", period=None):
    allowed = {"1m": "1d", "5m": "5d", "15m": "5d", "30m": "60d", "60m": "1y", "1d": "5y"}
    if interval not in allowed:
        raise MarketDataError("Unité de temps non prise en charge.")
    period = period or allowed[interval]
    key = f"{symbol}:{interval}:{period}"
    now = time.time()
    with _lock:
        item = _cache.get(key)
        if item and now - item[0] < CACHE_TTL:
            return item[1].copy()
    try:
        response = requests.get(YAHOO.format(symbol), params={"range": period, "interval": interval, "events": "history"}, headers=HEADERS, timeout=15)
        response.raise_for_status()
        payload = response.json()
        chart = payload.get("chart", {})
        if chart.get("error"):
            raise MarketDataError("La source de marché a signalé une erreur.")
        result = chart.get("result")
        if not result:
            raise MarketDataError("La source ne renvoie pas de bougies. Réessaie plus tard ou change la période.")
        obj = result[0]
        quotes = obj.get("indicators", {}).get("quote", [{}])[0]
        timestamps = obj.get("timestamp") or []
        if not timestamps:
            raise MarketDataError("La source a renvoyé un historique vide.")
        data = {name: quotes.get(name, [None] * len(timestamps)) for name in ("open", "high", "low", "close", "volume")}
        df = pd.DataFrame(data, index=pd.to_datetime(timestamps, unit="s", utc=True))
        df = df[~df.index.duplicated(keep="last")].sort_index().dropna(subset=["open", "high", "low", "close"])
        if len(df) < 50:
            raise MarketDataError(f"Historique insuffisant ({len(df)} bougies reçues, 50 minimum).")
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["volume"] = df["volume"].fillna(0)
        df.index.name = "time"
        with _lock:
            _cache[key] = (now, df)
        return df.copy()
    except MarketDataError:
        raise
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as exc:
        raise MarketDataError(f"Données de marché temporairement indisponibles ({type(exc).__name__}).") from exc

def indicators(df):
    x = df.copy()
    for period in (20, 50, 200):
        x[f"ema{period}"] = x.close.ewm(span=period, adjust=False).mean()
    prev_close = x.close.shift(1)
    true_range = pd.concat([x.high-x.low, (x.high-prev_close).abs(), (x.low-prev_close).abs()], axis=1).max(axis=1)
    x["atr"] = true_range.rolling(14, min_periods=14).mean()
    return x

def swing_points(df, left=3, right=3):
    highs, lows = [], []
    if len(df) < left + right + 5:
        return highs, lows
    h, l = df.high.to_numpy(), df.low.to_numpy()
    for i in range(left, len(df)-right):
        if h[i] >= max(h[i-left:i+right+1]) and h[i] > max(h[i-left:i]):
            highs.append((i, float(h[i])))
        if l[i] <= min(l[i-left:i+right+1]) and l[i] < min(l[i-left:i]):
            lows.append((i, float(l[i])))
    return highs, lows

def detect_features(raw):
    df = indicators(raw)
    last, prev = df.iloc[-1], df.iloc[-2]
    atr = float(last.atr) if pd.notna(last.atr) and last.atr > 0 else float((df.high-df.low).tail(14).mean())
    tail = df.tail(120).reset_index(drop=True)
    highs, lows = swing_points(tail)
    recent_high = max((p for _, p in highs[-8:]), default=float(tail.high.iloc[-20:].max()))
    recent_low = min((p for _, p in lows[-8:]), default=float(tail.low.iloc[-20:].min()))
    close = float(last.close)
    trend = "HAUSSIÈRE" if last.ema20 > last.ema50 and close > last.ema50 else "BAISSIÈRE" if last.ema20 < last.ema50 and close < last.ema50 else "NEUTRE"
    # Sweep/reclaim against the prior 20 completed candles; current candle is the trigger candle.
    prior_high = float(df.high.iloc[-21:-1].max())
    prior_low = float(df.low.iloc[-21:-1].min())
    sweep_high = float(last.high) > prior_high and close < prior_high
    sweep_low = float(last.low) < prior_low and close > prior_low
    # Latest three-candle fair-value gap candidate.
    fvg = None
    for i in range(max(2, len(df)-35), len(df)):
        a, c = df.iloc[i-2], df.iloc[i]
        if float(c.low) > float(a.high):
            fvg = {"type": "haussière", "low": float(a.high), "high": float(c.low), "time": df.index[i].isoformat()}
        elif float(c.high) < float(a.low):
            fvg = {"type": "baissière", "low": float(c.high), "high": float(a.low), "time": df.index[i].isoformat()}
    # Candidate OB: last opposite candle before a large body displacement. This is a rule-based proxy.
    ob = None
    for i in range(len(df)-2, max(2, len(df)-16), -1):
        base, move = df.iloc[i-1], df.iloc[i]
        body = abs(float(move.close)-float(move.open))
        if body >= max(atr*1.2, 1e-8):
            if move.close > move.open and base.close < base.open:
                ob = {"type": "haussier", "low": float(base.low), "high": float(max(base.open, base.close)), "time": df.index[i-1].isoformat()}
                break
            if move.close < move.open and base.close > base.open:
                ob = {"type": "baissier", "low": float(min(base.open, base.close)), "high": float(base.high), "time": df.index[i-1].isoformat()}
                break
    # FX tick/real volume is not guaranteed; no volume-weighted VWAP is invented.
    volume_ok = df.volume.notna().sum() > 30 and float(df.volume.fillna(0).tail(30).sum()) > 0
    vwap = None
    if volume_ok:
        sample = df.tail(100)
        typical = (sample.high + sample.low + sample.close) / 3
        volume = sample.volume.fillna(0)
        if float(volume.sum()) > 0:
            vwap = float((typical * volume).sum() / volume.sum())
    return {"price": close, "time": df.index[-1].isoformat(), "trend": trend,
            "ema20": float(last.ema20), "ema50": float(last.ema50),
            "ema200": float(last.ema200) if pd.notna(last.ema200) else None,
            "atr": atr, "atr_pips": atr*10000, "recent_high": recent_high, "recent_low": recent_low,
            "sweep_high": bool(sweep_high), "sweep_low": bool(sweep_low), "fvg": fvg, "order_block": ob,
            "vwap": vwap, "volume_available": bool(volume_ok), "candles": int(len(df)),
            "spread_note": "Le spread réel du courtier n'est pas fourni par cette source."}

def session_profile(raw, bins=48):
    """Rolling 288-candle VWAP/POC profile. If Yahoo FX volume is zero, label candle-count proxy explicitly."""
    if raw is None or raw.empty:
        return {"vwap": None, "poc": None, "method": "indisponible", "value_area_low": None, "value_area_high": None}
    sample = raw.tail(min(len(raw), 288)).copy()
    typical = (sample.high + sample.low + sample.close) / 3.0
    vol = pd.to_numeric(sample.volume, errors="coerce").fillna(0).clip(lower=0)
    real_volume = float(vol.sum()) > 0 and int((vol > 0).sum()) >= max(10, int(len(sample)*0.25))
    weights = vol if real_volume else pd.Series(1.0, index=sample.index)
    vwap = float((typical * weights).sum() / weights.sum()) if float(weights.sum()) else None
    low, high = float(sample.low.min()), float(sample.high.max())
    if high <= low:
        return {"vwap": vwap, "poc": float(typical.iloc[-1]), "method": "volume réel" if real_volume else "proxy par bougie; pas un vrai volume FX", "value_area_low": low, "value_area_high": high}
    edges = np.linspace(low, high, bins+1)
    idx = np.clip(np.digitize(typical.to_numpy(), edges)-1, 0, bins-1)
    profile = np.zeros(bins, dtype=float)
    for k, w in zip(idx, weights.to_numpy()): profile[k] += float(w)
    poc_i = int(np.argmax(profile)); poc = float((edges[poc_i]+edges[poc_i+1])/2)
    # Expand from POC to the nearest heavier adjacent bin until 70% of profile weight is covered.
    left = right = poc_i; covered = profile[poc_i]; target = profile.sum()*0.70
    while covered < target and (left > 0 or right < bins-1):
        lv = profile[left-1] if left > 0 else -1
        rv = profile[right+1] if right < bins-1 else -1
        if rv >= lv and right < bins-1: right += 1; covered += profile[right]
        elif left > 0: left -= 1; covered += profile[left]
        else: break
    return {"vwap": vwap, "poc": poc, "method": "volume Yahoo" if real_volume else "proxy par fréquence des bougies (pas un vrai POC de volume)",
            "value_area_low": float(edges[left]), "value_area_high": float(edges[right+1])}

def reaction_levels(raw):
    """Objective reaction references: previous completed day and recent confirmed local swings."""
    if raw is None or len(raw) < 10: return []
    out=[]
    try:
        daily = raw.resample('1D').agg({'high':'max','low':'min','close':'last'}).dropna()
        if len(daily) >= 2:
            prev=daily.iloc[-2]
            out.extend([{"name":"Haut de la veille", "price":float(prev.high), "kind":"resistance"},
                        {"name":"Bas de la veille", "price":float(prev.low), "kind":"support"},
                        {"name":"Clôture précédente", "price":float(prev.close), "kind":"reference"}])
    except Exception: pass
    tail=raw.tail(160).reset_index(drop=True)
    highs,lows=swing_points(tail,3,3)
    for i,price in highs[-3:]: out.append({"name":"Swing high confirmé", "price":float(price), "kind":"resistance"})
    for i,price in lows[-3:]: out.append({"name":"Swing low confirmé", "price":float(price), "kind":"support"})
    # Merge levels within 2 pips to avoid a visually noisy level list.
    out.sort(key=lambda z:z['price'])
    merged=[]
    for item in out:
        match=next((x for x in merged if abs(x['price']-item['price']) <= 0.00020),None)
        if match: match['name'] += ' + '+item['name']
        else: merged.append(item)
    return merged[-12:]

def dxy_features(raw):
    x=indicators(raw); last=x.iloc[-1]
    trend='HAUSSIÈRE' if last.ema20>last.ema50 and last.close>last.ema50 else 'BAISSIÈRE' if last.ema20<last.ema50 and last.close<last.ema50 else 'NEUTRE'
    prev=x.close.shift(1)
    change=float((last.close/prev.iloc[-1]-1)*100) if pd.notna(prev.iloc[-1]) and prev.iloc[-1] else 0.0
    return {"price":float(last.close),"time":raw.index[-1].isoformat(),"trend":trend,"change_pct_last_bar":change,
            "ema20":float(last.ema20),"ema50":float(last.ema50),"candles":int(len(raw))}

def build_analysis():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    requests_map = {
        "15m": ("EURUSD=X", "15m", None), "5m": ("EURUSD=X", "5m", None),
        "1m": ("EURUSD=X", "1m", None), "60m": ("EURUSD=X", "60m", None),
        "DXY_15m": ("DX-Y.NYB", "15m", None), "DXY_60m": ("DX-Y.NYB", "60m", None),
        "1d": ("EURUSD=X", "1d", "1y")
    }
    frames, errors = {}, {}
    def load(k, spec):
        symbol, interval, period = spec
        return k, get_candles(symbol=symbol, interval=interval, period=period)
    with ThreadPoolExecutor(max_workers=7) as pool:
        futures={pool.submit(load,k,v):k for k,v in requests_map.items()}
        for future in as_completed(futures):
            key=futures[future]
            try: _, frames[key]=future.result()
            except Exception as exc: errors[key]=str(exc)
    # M1 is useful for execution, but must not take down the entire analysis when a public feed limits it.
    if "60m" not in frames or "15m" not in frames or "5m" not in frames:
        raise MarketDataError("Historique EUR/USD incomplet. Source publique indisponible pour H1/M15/M5; réessaie dans quelques minutes.")
    if "1m" not in frames: frames["1m"] = frames["5m"]
    feats={tf:detect_features(frames[tf]) for tf in ("60m","15m","5m","1m")}
    h1,m15,m5,m1=feats["60m"],feats["15m"],feats["5m"],feats["1m"]
    dxy={}
    if "DXY_60m" in frames: dxy["60m"]=dxy_features(frames["DXY_60m"])
    if "DXY_15m" in frames: dxy["15m"]=dxy_features(frames["DXY_15m"])
    score,reasons=0,[]
    for label,feature,weight in (("H1",h1,3),("M15",m15,2),("M5",m5,1)):
        if feature["trend"]=="HAUSSIÈRE": score+=weight; reasons.append(f"{label}: structure de tendance haussière (+{weight}).")
        elif feature["trend"]=="BAISSIÈRE": score-=weight; reasons.append(f"{label}: structure de tendance baissière (-{weight}).")
    if m5["sweep_low"]: score+=2; reasons.append("M5: balayage du plus bas récent puis clôture de réintégration.")
    if m5["sweep_high"]: score-=2; reasons.append("M5: balayage du plus haut récent puis clôture de réintégration.")
    if m5["fvg"]:
        delta=1 if m5["fvg"]["type"]=="haussière" else -1
        score+=delta; reasons.append(f"M5: FVG {m5['fvg']['type']} candidate (+/-1 selon le sens).")
    # DXY is a contextual inverse-dollar check, not a deterministic EURUSD predictor.
    dxy_alignment="indisponible"
    if dxy:
        dx=dxy.get("15m",dxy.get("60m")); dxh=dxy.get("60m",dx)
        if dx and dx["trend"]=="BAISSIÈRE": score+=1; dxy_alignment="plutôt favorable à EUR/USD"; reasons.append("DXY: tendance baissière; confluence macro-directionnelle favorable à EUR/USD (+1).")
        elif dx and dx["trend"]=="HAUSSIÈRE": score-=1; dxy_alignment="pression potentielle sur EUR/USD"; reasons.append("DXY: tendance haussière; pression potentielle sur EUR/USD (-1).")
        if dx and dxh and dx["trend"]!=dxh["trend"]: reasons.append("DXY: désaccord M15/H1, signal dollar moins net.")
    # Reaction levels and profile are calculated from recent M5 candles.
    profile=session_profile(frames["5m"])
    levels=reaction_levels(frames["5m"])
    if "1d" in frames:
        daily_levels=reaction_levels(frames["1d"])
        for lev in daily_levels:
            if not any(abs(existing["price"]-lev["price"]) <= 0.00020 for existing in levels): levels.append(lev)
    current=m5["price"]
    nearest=sorted(levels,key=lambda z:abs(z["price"]-current))[:5]
    for lev in nearest:
        if abs(lev["price"]-current)<=max(m5["atr"]*0.5,0.00025): reasons.append(f"Niveau de réaction proche: {lev['name']} à {lev['price']:.5f}.")
    if profile.get("vwap") is not None:
        if current>profile["vwap"]: reasons.append("Prix au-dessus du VWAP/proxy de séance : contexte intraday relativement soutenu.")
        else: reasons.append("Prix sous le VWAP/proxy de séance : contexte intraday relativement faible.")
    if profile.get("poc") is not None: reasons.append("POC affiché comme niveau de référence; méthode de calcul visible dans le panneau.")
    direction="LONG" if score>=5 else "SHORT" if score<=-5 else "WAIT"
    trigger=m1["sweep_low"] if direction=="LONG" else m1["sweep_high"] if direction=="SHORT" else False
    # Require a real trigger only when M1 feed exists. If fallback M5, explicitly mark it.
    trigger_tf="M1" if "1m" in frames and frames["1m"] is not frames["5m"] else "M5 (repli; M1 indisponible)"
    if direction in ("LONG","SHORT") and not trigger:
        reasons.append(f"Biais directionnel mais pas de sweep/reclaim de déclenchement ({trigger_tf}); attente.")
        direction="WAIT"
    entry=m1["price"]; atr=max(m5["atr"],0.0001); sl=tp=rr=None
    if direction in ("LONG","SHORT"):
        risk=max(1.2*atr,0.00025)
        sl=entry-risk if direction=="LONG" else entry+risk
        tp=entry+1.8*atr if direction=="LONG" else entry-1.8*atr
        rr=round(abs(tp-entry)/abs(entry-sl),2)
    if not reasons: reasons.append("Confluence insuffisante selon les règles configurées.")
    return {"app":APP_NAME,"symbol":"EUR/USD","source":"Yahoo Finance public chart endpoints; DXY via DX-Y.NYB when available",
        "generated_at":datetime.now(timezone.utc).isoformat(),"signal":direction,"score":score,
        "confidence_label":"Score de confluence, pas une probabilité statistique","reasons":reasons,
        "entry_reference":entry if direction in ("LONG","SHORT") else None,"stop_loss":sl,"take_profit":tp,"rr":rr,
        "timeframes":feats,"dxy":dxy,"dxy_alignment":dxy_alignment,"profile":profile,"reaction_levels":levels,
        "warnings":[
          "Prototype de recherche, sans rentabilité garantie et sans exécution d'ordres.",
          "DXY est un filtre de contexte, pas une relation inverse parfaite avec EUR/USD.",
          "Sur le marché spot FX, Yahoo ne fournit généralement pas de volume centralisé fiable : le VWAP/POC peut être un proxy par bougie, clairement étiqueté.",
          "Order Blocks, FVG et sweeps sont des définitions algorithmiques candidates, pas des vérités institutionnelles universelles.",
          "Les données peuvent être retardées, incomplètes ou différentes du flux du courtier; spread, commissions et glissement réels ne sont pas disponibles.",
          "Le signal exige une validation visuelle et un test hors échantillon avant tout usage réel."
        ],"data_warnings":errors}

def _make_signals(df, strategy, dxy_df=None):
    """Create candle-close signals. Every signal is executed at the next candle open."""
    x = indicators(df).copy()
    prior_high = x.high.shift(1).rolling(20, min_periods=20).max()
    prior_low = x.low.shift(1).rolling(20, min_periods=20).min()
    x["sweep_long"] = (x.low < prior_low) & (x.close > prior_low)
    x["sweep_short"] = (x.high > prior_high) & (x.close < prior_high)
    trend_up = (x.ema20 > x.ema50) & (x.close > x.ema50)
    trend_down = (x.ema20 < x.ema50) & (x.close < x.ema50)
    # Simple displacement proxy: candle body >= 1.2 ATR; not a full OB/FVG reconstruction.
    body = (x.close-x.open).abs()
    displacement_up = (x.close > x.open) & (body >= 1.2*x.atr)
    displacement_down = (x.close < x.open) & (body >= 1.2*x.atr)
    signal = pd.Series(0, index=x.index, dtype=int)
    if strategy == "ema":
        signal = pd.Series(np.where((x.ema20 > x.ema50) & (x.ema20.shift(1) <= x.ema50.shift(1)), 1,
                            np.where((x.ema20 < x.ema50) & (x.ema20.shift(1) >= x.ema50.shift(1)), -1, 0)), index=x.index)
    elif strategy == "sweep":
        signal[x.sweep_long & trend_up] = 1
        signal[x.sweep_short & trend_down] = -1
    elif strategy == "sweep_displacement":
        signal[x.sweep_long & trend_up & displacement_up] = 1
        signal[x.sweep_short & trend_down & displacement_down] = -1
    elif strategy == "confluence":
        # Reproducible candle-only approximation: sweep + trend + displacement + rolling VWAP proxy + DXY direction.
        typical=(x.high+x.low+x.close)/3
        vol=x.volume.fillna(0).clip(lower=0)
        vol_sum=vol.rolling(48,min_periods=12).sum()
        weighted=(typical*vol).rolling(48,min_periods=12).sum()
        proxy=typical.rolling(48,min_periods=12).mean()
        x["vwap_proxy"]=np.where(vol_sum>0,weighted/vol_sum,proxy)
        dollar_up=pd.Series(False,index=x.index); dollar_down=pd.Series(False,index=x.index)
        if dxy_df is not None and not dxy_df.empty:
            dx=indicators(dxy_df)
            aligned=dx[["close","ema20","ema50"]].reindex(x.index,method="ffill")
            dollar_up=(aligned.ema20>aligned.ema50)&(aligned.close>aligned.ema50)
            dollar_down=(aligned.ema20<aligned.ema50)&(aligned.close<aligned.ema50)
        signal[x.sweep_long & trend_up & displacement_up & (x.close>x.vwap_proxy) & dollar_down] = 1
        signal[x.sweep_short & trend_down & displacement_down & (x.close<x.vwap_proxy) & dollar_up] = -1
    else:
        raise ValueError("Stratégie inconnue. Utilise ema, sweep, sweep_displacement ou confluence.")
    x["signal"] = signal
    return x

def run_backtest(days, cost_pips, strategy):
    # Yahoo restricts intraday history: use 30m for <=60d, 1h for longer tests.
    if days <= 60:
        interval, period, bars_per_day = "30m", "60d", 48
    else:
        interval, period, bars_per_day = "60m", ("1y" if days <= 365 else "2y"), 24
    raw = get_candles(interval=interval, period=period)
    dxy_raw = None
    if strategy == "confluence":
        try: dxy_raw = get_candles(symbol="DX-Y.NYB", interval=interval, period=period)
        except MarketDataError: dxy_raw = None
        if dxy_raw is None:
            raise MarketDataError("Le backtest Confluence nécessite aussi l'historique DXY; la source ne le fournit pas actuellement.")
    test_raw=raw.tail(days*bars_per_day + 260).copy()
    df = _make_signals(test_raw, strategy, dxy_raw).dropna(subset=["atr", "ema20", "ema50"]).reset_index(drop=True)
    trades, i = [], 1
    while i < len(df)-1:
        side = int(df.signal.iloc[i])
        if side == 0:
            i += 1; continue
        entry = float(df.open.iloc[i+1])  # next bar open prevents same-bar look-ahead
        atr = float(df.atr.iloc[i])
        if not math.isfinite(atr) or atr <= 0:
            i += 1; continue
        risk = 1.2*atr
        sl = entry-side*risk
        tp = entry+side*1.8*atr
        exit_price = float(df.close.iloc[min(i+12, len(df)-1)])
        outcome = None
        for j in range(i+1, min(i+13, len(df))):
            hi, lo = float(df.high.iloc[j]), float(df.low.iloc[j])
            stop_hit = lo <= sl if side == 1 else hi >= sl
            target_hit = hi >= tp if side == 1 else lo <= tp
            if stop_hit and target_hit:
                outcome, exit_price = -1.0, sl  # conservative same-bar assumption
                break
            if stop_hit:
                outcome, exit_price = -1.0, sl
                break
            if target_hit:
                outcome, exit_price = 1.5, tp
                break
        if outcome is None:
            outcome = side*(exit_price-entry)/risk
        outcome -= cost_pips/(risk*10000)
        trades.append({"side": "LONG" if side == 1 else "SHORT", "entry": round(entry, 5),
                       "exit": round(exit_price, 5), "r_multiple": round(float(outcome), 3),
                       "time": str(df.iloc[i+1].get("time", i+1))})
        i += 12  # no overlapping positions in this simple test
    rs = [t["r_multiple"] for t in trades]
    wins, losses = [r for r in rs if r > 0], [r for r in rs if r < 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    curve = np.cumsum(rs) if rs else np.array([])
    peak = np.maximum.accumulate(np.insert(curve, 0, 0))[1:] if rs else np.array([])
    drawdown = peak-curve if rs else np.array([])
    return {"strategy": strategy, "method": {
                "ema": "Croisement EMA20/EMA50, entrée à l'ouverture de la bougie suivante",
                "sweep": "Sweep/reclaim 20 bougies + tendance EMA20/EMA50, entrée à l'ouverture suivante",
                "sweep_displacement": "Sweep/reclaim + tendance + displacement >= 1.2 ATR, proxy PA/OB, entrée suivante",
                "confluence": "Sweep + tendance + displacement + VWAP/proxy + filtre DXY aligné, entrée suivante"
            }[strategy],
            "interval": interval, "period_days_requested": days, "bars": int(len(df)), "trades": len(rs),
            "win_rate_pct": round(100*len(wins)/len(rs), 2) if rs else None,
            "expectancy_R": round(float(np.mean(rs)), 3) if rs else None,
            "profit_factor": round(gross_win/gross_loss, 3) if gross_loss else (None if not gross_win else "infinite_no_losses"),
            "net_R": round(float(sum(rs)), 3), "max_drawdown_R": round(float(drawdown.max()), 3) if rs else None,
            "cost_assumption_pips": cost_pips, "trades_preview": trades[-20:],
            "limitations": [
                "Test approximatif sur bougies publiques Yahoo, pas de données tick ni bid/ask historique.",
                "Coût constant en pips; glissement, commissions, rollover et variations de spread ne sont pas modélisés.",
                "Le proxy displacement n'est pas un vrai backtest complet des zones Order Block/FVG.",
                "La stratégie Confluence emploie un proxy VWAP si le volume FX manque et un filtre DXY basé sur bougies, pas du tick/order flow.",
                "Si stop et cible sont touchés dans la même bougie, le stop est considéré touché en premier.",
                "Résultats exploratoires : optimiser sur l'historique peut surajuster la stratégie."
            ]}

@app.route("/")
def home():
    return render_template("index.html", app_name=APP_NAME)

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "app": APP_NAME, "utc": datetime.now(timezone.utc).isoformat(),
                    "cache_ttl_seconds": CACHE_TTL, "note": "La santé du serveur ne garantit pas la disponibilité de la source de marché."})

@app.route("/api/analysis")
def api_analysis():
    try:
        return jsonify(build_analysis())
    except MarketDataError as exc:
        return jsonify({"error": str(exc), "status": "data_unavailable", "generated_at": datetime.now(timezone.utc).isoformat()}), 503
    except Exception:
        app.logger.exception("Analysis failed")
        return jsonify({"error": "Erreur interne pendant l'analyse. Consulte les logs de l'hébergeur."}), 500

@app.route("/api/backtest")
def backtest():
    try:
        days = min(max(int(request.args.get("days", "30")), 5), 365)
        cost_pips = min(max(float(request.args.get("cost_pips", "1.2")), 0), 10)
        strategy = request.args.get("strategy", "ema").strip().lower()
        if strategy not in {"ema", "sweep", "sweep_displacement", "confluence"}:
            return jsonify({"error": "Stratégie invalide. Choisis ema, sweep, sweep_displacement ou confluence."}), 400
        return jsonify(run_backtest(days, cost_pips, strategy))
    except MarketDataError as exc:
        return jsonify({"error": str(exc), "status": "data_unavailable"}), 503
    except (ValueError, OverflowError) as exc:
        return jsonify({"error": f"Paramètre invalide : {exc}"}), 400
    except Exception:
        app.logger.exception("Backtest failed")
        return jsonify({"error": "Backtest impossible actuellement; consulte les logs de l'hébergeur."}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
