"""
ماسح السوق — نسخة مبسطة جدًا (إشارات كثيرة)
الشروط الأساسية فقط: score >= 1.0 + ربح أول هدف >= 1%
"""

import os
import json
import time
import datetime as dt
import concurrent.futures
import requests

# ---------- الإعدادات ----------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
INTERVAL = os.environ.get("SCAN_INTERVAL", "1h")
DEPTH = int(os.environ.get("SCAN_DEPTH", "200"))  # زيادة إلى 200
SCAN_LIMIT = 400
LIQUIDITY_FLOOR = 500_000  # تخفيض من 1M إلى 500K لزيادة العملات

EXCLUDE_SUFFIX = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
EXCLUDE_SYMS = {"USDCUSDT","FDUSDUSDT","TUSDUSDT","DAIUSDT","USDPUSDT",
                "EURUSDT","GBPUSDT","AEURUSDT","BFUSDUSDT"}

BASE_URL = "https://data-api.binance.vision/api/v3"

# ---------- إعدادات المؤشرات ----------
ADX_PERIOD = 14
DIVERGENCE_LOOKBACK = 20
DIVERGENCE_PIVOT_SPAN = 3
RESISTANCE_LOOKBACK = 50
RESISTANCE_PIVOT_SPAN = 3
RESISTANCE_PROXIMITY_PCT = 1.0  # تخفيض من 1.5 إلى 1.0 (أقل حساسية)
OBV_TREND_WINDOW = 10

# ---------- فيبوناتشي (تشخيصي) ----------
FIB_LOOKBACK = 50
FIB_LEVELS = (0.382, 0.5, 0.618, 0.786)
FIB_PROXIMITY_PCT = 1.0

# ---------- فلتر الإرهاق (معطل) ----------
EXTENSION_EMA_PERIOD = 50
EXTENSION_ATR_THRESHOLD = float(os.environ.get("EXTENSION_ATR_THRESHOLD", "999"))

# ---------- الحد الأدنى للربح ----------
MIN_PROFIT_PCT = float(os.environ.get("MIN_PROFIT_PCT", "1.0"))

# ---------- الإشارات المبكرة ----------
SQUEEZE_LOOKBACK = 20
SQUEEZE_RATIO_THRESHOLD = 0.6
ACCUM_WINDOW = 20
ACCUM_PRICE_MAX_MOVE_PCT = 4.0
ACCUM_FLOW_RATIO_MIN = 0.3

# ---------- إشارة الانفجار ----------
BREAKOUT_LOOKBACK = 10
BREAKOUT_VOL_MULT = 1.3
BREAKOUT_MIN_ATR_PCT = 0.08

EARLY_SL_ATR_MULT = 2.0

# ---------------- دوال المؤشرات الفنية ----------------

def ema(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values, period=14):
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain, avg_loss = gains / period, losses / period
    out = [None] * period
    out.append(100 - (100 / (1 + (avg_gain / (avg_loss or 1e-9)))))
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        gain = d if d > 0 else 0
        loss = -d if d < 0 else 0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out.append(100 - (100 / (1 + avg_gain / (avg_loss or 1e-9))))
    return out


def macd(values):
    e12, e26 = ema(values, 12), ema(values, 26)
    macd_line = [a - b for a, b in zip(e12, e26)]
    return macd_line, ema(macd_line, 9)


def bollinger(values, period=20, mult=2):
    upper, lower = [], []
    for i in range(len(values)):
        if i < period - 1:
            upper.append(None); lower.append(None); continue
        window = values[i - period + 1:i + 1]
        mean = sum(window) / period
        sd = (sum((x - mean) ** 2 for x in window) / period) ** 0.5
        upper.append(mean + mult * sd)
        lower.append(mean - mult * sd)
    return upper, lower


def rolling_avg(values, period):
    out = [None] * len(values)
    for i in range(period, len(values)):
        out[i] = sum(values[i - period:i]) / period
    return out


def adx(highs, lows, closes, period=ADX_PERIOD):
    n = len(closes)
    out = [None] * n
    if n <= period * 2:
        return out
    tr = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    tr_sum = sum(tr[1:period + 1])
    plus_sum = sum(plus_dm[1:period + 1])
    minus_sum = sum(minus_dm[1:period + 1])
    dx = [None] * n
    for i in range(period + 1, n):
        tr_sum = tr_sum - (tr_sum / period) + tr[i]
        plus_sum = plus_sum - (plus_sum / period) + plus_dm[i]
        minus_sum = minus_sum - (minus_sum / period) + minus_dm[i]
        pdi = 100 * plus_sum / tr_sum if tr_sum else 0
        mdi = 100 * minus_sum / tr_sum if tr_sum else 0
        dx[i] = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0
    start = period * 2
    valid_dx = [x for x in dx[period + 1:start + 1] if x is not None]
    if not valid_dx:
        return out
    out[start] = sum(valid_dx) / len(valid_dx)
    for i in range(start + 1, n):
        if out[i - 1] is None or dx[i] is None:
            continue
        out[i] = (out[i - 1] * (period - 1) + dx[i]) / period
    return out


def obv(closes, vols):
    out = [0.0] * len(closes)
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            out[i] = out[i - 1] + vols[i]
        elif closes[i] < closes[i - 1]:
            out[i] = out[i - 1] - vols[i]
        else:
            out[i] = out[i - 1]
    return out


def obv_confirms_trend(obv_vals, trend_up, window=OBV_TREND_WINDOW):
    if len(obv_vals) <= window:
        return False
    obv_slope_up = obv_vals[-1] > obv_vals[-1 - window]
    return obv_slope_up == trend_up


def volatility_squeeze(bb_upper, bb_lower, closes, lookback=SQUEEZE_LOOKBACK):
    n = len(closes)
    if n <= lookback or bb_upper[-1] is None or bb_lower[-1] is None:
        return False
    widths = []
    for i in range(n - lookback, n):
        if bb_upper[i] is None or bb_lower[i] is None:
            continue
        widths.append((bb_upper[i] - bb_lower[i]) / closes[i])
    if len(widths) < lookback * 0.5:
        return False
    avg_width = sum(widths) / len(widths)
    if avg_width == 0:
        return False
    return widths[-1] <= avg_width * SQUEEZE_RATIO_THRESHOLD


def silent_accumulation(closes, vols, obv_vals, window=ACCUM_WINDOW):
    n = len(closes)
    if n <= window:
        return False
    price_change_pct = abs(closes[-1] - closes[-1 - window]) / closes[-1 - window] * 100
    vol_sum = sum(vols[-window:])
    if vol_sum == 0:
        return False
    flow_ratio = (obv_vals[-1] - obv_vals[-1 - window]) / vol_sum
    return price_change_pct <= ACCUM_PRICE_MAX_MOVE_PCT and flow_ratio >= ACCUM_FLOW_RATIO_MIN


def bullish_divergence(closes, rsi_vals, lookback=DIVERGENCE_LOOKBACK, pivot_span=DIVERGENCE_PIVOT_SPAN):
    n = len(closes)
    if n < lookback + pivot_span * 2:
        return False
    start = n - lookback
    lows_idx = []
    for i in range(max(start, pivot_span), n - pivot_span):
        if rsi_vals[i] is None:
            continue
        window = closes[i - pivot_span:i + pivot_span + 1]
        if closes[i] == min(window):
            lows_idx.append(i)
    if len(lows_idx) < 2:
        return False
    i1, i2 = lows_idx[-2], lows_idx[-1]
    if rsi_vals[i1] is None or rsi_vals[i2] is None:
        return False
    price_lower_low = closes[i2] < closes[i1]
    rsi_higher_low = rsi_vals[i2] > rsi_vals[i1]
    return price_lower_low and rsi_higher_low


def nearest_resistance(highs, closes, lookback=RESISTANCE_LOOKBACK, pivot_span=RESISTANCE_PIVOT_SPAN):
    n = len(highs)
    window_n = min(lookback, n)
    start = n - window_n
    price = closes[-1]
    pivots = []
    for i in range(max(start, pivot_span), n - pivot_span):
        window = highs[i - pivot_span:i + pivot_span + 1]
        if highs[i] == max(window):
            pivots.append(highs[i])
    above = [p for p in pivots if p > price]
    return min(above) if above else None


def fibonacci_levels(highs, lows, lookback=FIB_LOOKBACK):
    n = len(highs)
    window_n = min(lookback, n)
    start = n - window_n
    if window_n < 2:
        return {}
    swing_high = max(highs[start:n])
    swing_low = min(lows[start:n])
    diff = swing_high - swing_low
    if diff <= 0:
        return {}
    return {lvl: swing_high - diff * lvl for lvl in FIB_LEVELS}


def nearest_fib_level(price, levels, proximity_pct=FIB_PROXIMITY_PCT):
    best = None
    for lvl, lvl_price in levels.items():
        if lvl_price <= 0:
            continue
        dist_pct = abs(price - lvl_price) / lvl_price * 100
        if dist_pct <= proximity_pct and (best is None or dist_pct < best[2]):
            best = (lvl, lvl_price, dist_pct)
    if best:
        return best[0], best[1]
    return None, None


def momentum_strength(macd_line, signal, rsi_vals, i):
    if i < 1 or macd_line[i] is None or signal[i] is None or rsi_vals[i] is None:
        return False
    hist_now = macd_line[i] - signal[i]
    hist_prev = macd_line[i - 1] - signal[i - 1]
    macd_bull = hist_now > 0 and hist_now > hist_prev
    rsi_rising = rsi_vals[i] > rsi_vals[i - 1] and 45 <= rsi_vals[i] <= 65
    return macd_bull and rsi_rising


def breakout_detect(highs, closes, vols, lookback=BREAKOUT_LOOKBACK, vol_mult=BREAKOUT_VOL_MULT):
    n = len(closes)
    if n < lookback + 5:
        return False
    recent_high = max(highs[-lookback-1:-1])
    prev_vol_avg = sum(vols[-lookback-1:-1]) / lookback
    if prev_vol_avg == 0:
        return False
    return closes[-1] > recent_high and vols[-1] > prev_vol_avg * vol_mult


def breakout_quality(ind, i):
    if i < 1:
        return 0, {}
    ema7 = ind.get("ema7")
    ema14 = ind.get("ema14")
    if not ema7 or not ema14 or i >= len(ema7) or ema7[i] is None:
        return 0, {}
    details = {}
    score = 0
    if ema7[i] > ema14[i]:
        score += 1
        details["trend_support"] = True
    if ind["macd"][i] is not None and ind["signal"][i] is not None:
        if ind["macd"][i] > ind["signal"][i]:
            score += 1
            details["macd_bull"] = True
    if ind["rsi"][i] is not None and 40 <= ind["rsi"][i] <= 70:
        score += 1
        details["rsi_ok"] = True
    return score, details


def atr_value_at(ind, i, period=14):
    trs = []
    start = max(1, i - period + 1)
    for j in range(start, i + 1):
        prev_close = ind["closes"][j - 1]
        tr = max(ind["highs"][j] - ind["lows"][j],
                  abs(ind["highs"][j] - prev_close),
                  abs(ind["lows"][j] - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0


def overextended(ind, i, trend_up):
    ema50 = ind.get("ema50")
    if not ema50 or i >= len(ema50) or ema50[i] is None:
        return False
    atrv = atr_value_at(ind, i)
    if atrv <= 0:
        return False
    price = ind["closes"][i]
    distance = (price - ema50[i]) / atrv if trend_up else (ema50[i] - price) / atrv
    return distance >= EXTENSION_ATR_THRESHOLD


def compute_indicators(klines):
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    vols = [float(k[5]) for k in klines]
    macd_line, signal = macd(closes)
    bb_upper, bb_lower = bollinger(closes)
    return {
        "closes": closes, "highs": highs, "lows": lows, "vols": vols,
        "ema7": ema(closes, 7), "ema14": ema(closes, 14),
        "ema50": ema(closes, EXTENSION_EMA_PERIOD),
        "rsi": rsi(closes),
        "macd": macd_line, "signal": signal,
        "bb_upper": bb_upper, "bb_lower": bb_lower,
        "vol_avg": rolling_avg(vols, 20),
        "adx": adx(highs, lows, closes),
        "obv": obv(closes, vols),
    }


def score_at(i, ind, apply_extra_filters=True):
    if i < 16 or ind["bb_upper"][i] is None or ind["rsi"][i] is None or ind["vol_avg"][i] is None:
        return None
    trend_up = ind["ema7"][i] > ind["ema14"][i]
    rv = ind["rsi"][i]
    rsi_state = 1 if rv < 35 else (-1 if rv > 65 else 0)
    macd_bull = ind["macd"][i] > ind["signal"][i]
    price = ind["closes"][i]
    bb_state = 1 if price <= ind["bb_lower"][i] else (-1 if price >= ind["bb_upper"][i] else 0)
    vol_confirm = ind["vols"][i] > ind["vol_avg"][i] * 1.1
    trend_dir = 1 if trend_up else -1
    vol_score = trend_dir * 0.5 if vol_confirm else 0
    score = trend_dir + rsi_state + (1 if macd_bull else -1) + bb_state + vol_score

    divergence = bullish_divergence(ind["closes"][:i + 1], ind["rsi"][:i + 1])
    resistance = nearest_resistance(ind["highs"][:i + 1], ind["closes"][:i + 1])
    near_resistance = False
    if resistance:
        dist_pct = (resistance - price) / price * 100
        near_resistance = 0 <= dist_pct <= RESISTANCE_PROXIMITY_PCT

    obv_confirm = obv_confirms_trend(ind["obv"][:i + 1], trend_up)
    extended = overextended(ind, i, trend_up)

    fib_map = fibonacci_levels(ind["highs"][:i + 1], ind["lows"][:i + 1])
    fib_level, fib_level_price = nearest_fib_level(price, fib_map)
    fib_support = fib_level is not None and fib_level >= 0.5 and trend_up

    if apply_extra_filters:
        if divergence:
            score += 1
        if near_resistance:
            score -= 1
        if obv_confirm:
            score += trend_dir * 0.5

    return {
        "score": score, "trend_up": trend_up, "vol_confirm": vol_confirm, "rv": rv,
        "adx_val": ind["adx"][i] if i < len(ind["adx"]) else None,
        "divergence": divergence,
        "near_resistance": near_resistance, "resistance": resistance,
        "obv_confirm": obv_confirm,
        "extended": extended,
        "fib_level": fib_level,
        "fib_support": fib_support,
        "rsi_state": rsi_state,
        "macd_bull": macd_bull,
        "bb_state": bb_state,
    }


def atr_percent(ind, period=14):
    return atr_value(ind, period) / ind["closes"][-1] * 100


def atr_value(ind, period=14):
    n = len(ind["closes"])
    trs = []
    for i in range(n - period, n):
        prev_close = ind["closes"][i - 1] if i > 0 else ind["closes"][i]
        tr = max(ind["highs"][i] - ind["lows"][i],
                  abs(ind["highs"][i] - prev_close),
                  abs(ind["lows"][i] - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs)


# ---------------- جلب البيانات ----------------

def meets_min_profit(entry, tps, min_pct=MIN_PROFIT_PCT):
    if not entry or not tps:
        return False
    tp1_profit_pct = (tps[0] - entry) / entry * 100
    return tp1_profit_pct >= min_pct


def _request_with_retry(url, params=None, timeout=20, retries=3, backoff=1.5):
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.exceptions.HTTPError(f"status {r.status_code}")
            r.raise_for_status()
            return r
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise last_err


def fetch_ticker24h():
    r = _request_with_retry(f"{BASE_URL}/ticker/24hr")
    return r.json()


def fetch_prices_map(tickers):
    out = {}
    for t in tickers:
        try:
            out[t["symbol"]] = float(t["lastPrice"])
        except Exception:
            continue
    return out


def fetch_klines(symbol, interval, limit=SCAN_LIMIT):
    r = _request_with_retry(f"{BASE_URL}/klines",
                             params={"symbol": symbol, "interval": interval, "limit": limit})
    return r.json()


def drop_unclosed_candle(klines):
    if not klines:
        return klines
    now_ms = time.time() * 1000
    close_time = klines[-1][6]
    if close_time > now_ms:
        return klines[:-1]
    return klines


# ---------------- تحليل عملة واحدة ----------------

def analyze_symbol(t, interval):
    symbol = t["symbol"]
    try:
        klines = fetch_klines(symbol, interval, SCAN_LIMIT)
        klines = drop_unclosed_candle(klines)
        if len(klines) < 60:
            return None
        ind = compute_indicators(klines)
        last = len(ind["closes"]) - 1
        r = score_at(last, ind)
        if not r:
            return None

        prev_r = score_at(last - 1, ind)
        persistent = bool(prev_r) and (prev_r["score"] > 0) == (r["score"] > 0) and abs(prev_r["score"]) >= 0.5

        final_score = r["score"]

        squeeze = volatility_squeeze(ind["bb_upper"], ind["bb_lower"], ind["closes"])
        accumulation = silent_accumulation(ind["closes"], ind["vols"], ind["obv"])

        early_entry = early_sl = None
        early_tps = []
        early_confidence = None
        early_source = None
        early_factors = []
        momentum = momentum_strength(ind["macd"], ind["signal"], ind["rsi"], last)
        conditions_met = sum([squeeze, accumulation, r["divergence"], momentum])
        
        if squeeze or accumulation:
            if conditions_met >= 3:
                early_confidence = "مؤكدة قوية"
            elif conditions_met == 2:
                early_confidence = "مؤكدة"
            else:
                early_confidence = "احتمالية"

            if accumulation:
                early_factors.append("accumulation")
            if r["divergence"]:
                early_factors.append("divergence")
            if momentum:
                early_factors.append("momentum")
            if squeeze:
                early_factors.append("squeeze")
            early_factors.sort()
            early_source = "+".join(early_factors)

            early_entry = ind["closes"][last]
            atrv = atr_value(ind)
            atr_risk = atrv * EARLY_SL_ATR_MULT
            min_risk_for_target = early_entry * (MIN_PROFIT_PCT / 100)
            early_risk = max(atr_risk, min_risk_for_target)
            early_sl = early_entry - early_risk
            early_tp_count = conditions_met
            raw_tps = [early_entry + early_risk * i for i in range(1, early_tp_count + 1)]
            resistance = r.get("resistance")

            if resistance:
                trimmed = []
                for tp in raw_tps:
                    if tp >= resistance:
                        trimmed.append(resistance)
                        break
                    trimmed.append(tp)
                early_tps = trimmed
            else:
                early_tps = raw_tps

        breakout = breakout_detect(ind["highs"], ind["closes"], ind["vols"])
        breakout_score, breakout_details = 0, {}
        breakout_entry = breakout_sl = None
        breakout_tps = []
        if breakout:
            breakout_score, breakout_details = breakout_quality(ind, last)
            if breakout_score >= 1 and r["atr_pct"] >= BREAKOUT_MIN_ATR_PCT:
                breakout_entry = ind["closes"][last]
                atrv = atr_value(ind)
                atr_risk = atrv * 1.8
                min_risk_for_target = breakout_entry * (MIN_PROFIT_PCT / 100)
                risk = max(atr_risk, min_risk_for_target)
                breakout_sl = breakout_entry - risk
                if breakout_score >= 3:
                    tp_count = 3
                elif breakout_score >= 2:
                    tp_count = 2
                else:
                    tp_count = 1
                raw_tps = [breakout_entry + risk * i for i in range(1, tp_count + 1)]
                resistance = r.get("resistance")
                if resistance:
                    trimmed = []
                    for tp in raw_tps:
                        if tp >= resistance:
                            trimmed.append(resistance)
                            break
                        trimmed.append(tp)
                    breakout_tps = trimmed
                else:
                    breakout_tps = raw_tps

        entry = sl = None
        tps = []
        if final_score >= 1:
            entry = ind["closes"][last]
            atrv = atr_value(ind)
            sl = entry - atrv * 1.5
            risk = entry - sl

            if final_score >= 3.5:
                tp_count = 4
            elif final_score >= 2.5:
                tp_count = 3
            elif final_score >= 1.5:
                tp_count = 2
            else:
                tp_count = 1

            tps = [entry + risk * i for i in range(1, tp_count + 1)]

        return {
            "symbol": symbol,
            "price": ind["closes"][last],
            "change_pct": float(t["priceChangePercent"]),
            "score": final_score,
            "trend_up": r["trend_up"],
            "vol_confirm": r["vol_confirm"],
            "atr_pct": atr_percent(ind),
            "persistent": persistent,
            "divergence": r["divergence"],
            "near_resistance": r["near_resistance"],
            "obv_confirm": r["obv_confirm"],
            "extended": r["extended"],
            "fib_level": r["fib_level"],
            "fib_support": r["fib_support"],
            "rsi_state": r["rsi_state"],
            "macd_bull": r["macd_bull"],
            "bb_state": r["bb_state"],
            "squeeze": squeeze,
            "accumulation": accumulation,
            "momentum": momentum,
            "breakout": breakout,
            "breakout_score": breakout_score,
            "breakout_details": breakout_details,
            "entry": entry, "sl": sl, "tps": tps,
            "early_entry": early_entry, "early_sl": early_sl, "early_tps": early_tps,
            "early_confidence": early_confidence, "early_source": early_source,
            "early_factors": early_factors,
            "breakout_entry": breakout_entry, "breakout_sl": breakout_sl, "breakout_tps": breakout_tps,
        }
    except Exception as e:
        print(f"[تخطي] {symbol}: {e}")
        return None


# ---------------- المسح الكامل ----------------

def run_scan(tickers=None):
    if tickers is None:
        tickers = fetch_ticker24h()

    liquid = [
        t for t in tickers
        if t["symbol"].endswith("USDT")
        and not t["symbol"].endswith(EXCLUDE_SUFFIX)
        and t["symbol"] not in EXCLUDE_SYMS
        and float(t["quoteVolume"]) >= LIQUIDITY_FLOOR
    ]
    
    by_volume = sorted(liquid, key=lambda t: float(t["quoteVolume"]), reverse=True)
    by_momentum = sorted(liquid, key=lambda t: abs(float(t["priceChangePercent"])), reverse=True)
    volume_rank = {t["symbol"]: i for i, t in enumerate(by_volume)}
    momentum_rank = {t["symbol"]: i for i, t in enumerate(by_momentum)}
    combined = sorted(liquid, key=lambda t: volume_rank[t["symbol"]] + momentum_rank[t["symbol"]])
    shortlist = combined[:DEPTH]

    print(f"سيولة كافية: {len(liquid)} عملة | فحص عميق: {len(shortlist)} عملة | فريم: {INTERVAL}")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:  # 3 فقط لتجنب الحظر
        futures = [pool.submit(analyze_symbol, t, INTERVAL) for t in shortlist]
        for f in concurrent.futures.as_completed(futures):
            r = f.result()
            if r:
                results.append(r)

    return results


# ---------------- تيليجرام ----------------

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ TELEGRAM_TOKEN أو TELEGRAM_CHAT_ID غير موجودين — تخطي الإرسال.")
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
        if not resp.ok:
            print("فشل إرسال تيليجرام:", resp.text)
            return None
        return resp.json().get("result", {}).get("message_id")
    except Exception as e:
        print("خطأ إرسال تيليجرام:", e)
        return None


def _escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def edit_telegram_strike(message_id, original_text, result_text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not message_id:
        return
    new_text = f"<s>{_escape_html(original_text)}</s>\n\n{_escape_html(result_text)}"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    try:
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "message_id": message_id,
            "text": new_text,
            "parse_mode": "HTML",
        }, timeout=15)
        if not resp.ok:
            print("فشل تعديل رسالة تيليجرام:", resp.text)
    except Exception as e:
        print("خطأ تعديل رسالة تيليجرام:", e)


def delete_telegram_message(message_id):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not message_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteMessage"
    try:
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id}, timeout=15)
        if not resp.ok:
            print("فشل حذف رسالة تيليجرام:", resp.text)
    except Exception as e:
        print("خطأ حذف رسالة تيليجرام:", e)


def edit_telegram_append(message_id, original_text, extra_lines):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not message_id:
        return
    new_text = original_text + "\n\n" + "\n".join(extra_lines)
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    try:
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "message_id": message_id,
            "text": new_text,
        }, timeout=15)
        if not resp.ok:
            print("فشل تعديل رسالة تيليجرام:", resp.text)
    except Exception as e:
        print("خطأ تعديل رسالة تيليجرام:", e)


def format_tp_line(pos, tp_index):
    entry = pos["entry"]
    tp = pos["tps"][tp_index]
    pct_gain = (tp - entry) / entry * 100
    tp_label = f"TP{tp_index + 1}"
    return f"✅ تحقق {tp_label}: {tp:.6g} (+{pct_gain:.2f}%)"


def build_progress_text(pos):
    base = pos.get("alert_text", "")
    hit_sorted = sorted(pos.get("hit_tps", []))
    if not hit_sorted:
        return base
    lines = [format_tp_line(pos, j) for j in hit_sorted]
    return base + "\n\n" + "\n".join(lines)


def format_alert(r, market_caution=False):
    is_buy = r["score"] >= 1.0  # تخفيض من 1.5 إلى 1.0
    dot = "🟢" if is_buy else "🔴"
    title = "إشارة شراء" if is_buy else "تجنب شراء"

    lines = [
        f"{dot} {title}",
        r['symbol'].replace('USDT', '/USDT'),
    ]

    if r.get("entry") is not None:
        lines.append(f"الدخول: {r['entry']:.6g}")
        for i, tp in enumerate(r.get("tps", []), start=1):
            lines.append(f"TP{i}: {tp:.6g}")
        lines.append(f"وقف الخسارة: {r['sl']:.6g}")
    else:
        lines.append("لا توجد خطة دخول (تحذير فقط)")

    return "\n".join(lines)


def format_early_alert(r):
    confidence = r.get("early_confidence")
    dot = "🟢" if confidence == "مؤكدة قوية" else ("🟣" if confidence == "مؤكدة" else "🔵")
    title = f"إشارة {confidence}" if confidence else "إشارة مبكرة"

    factors = r.get("early_factors") or []
    source_label = "+".join(factors)

    lines = [
        f"{dot} {title}",
        r['symbol'].replace('USDT', '/USDT'),
    ]
    if source_label:
        lines.append(f"المصدر: {source_label}")

    if r.get("early_entry") is not None:
        lines.append(f"الدخول : {r['early_entry']:.6g}")
        for i, tp in enumerate(r.get("early_tps", []), start=1):
            lines.append(f"TP {i}: {tp:.6g}")
        lines.append(f"SL : {r['early_sl']:.6g}")

    return "\n".join(lines)


def format_breakout_alert(r):
    b_score = r.get("breakout_score", 0)
    dot = "🟠" if b_score >= 3 else ("🟡" if b_score >= 2 else "⚪")
    title = "إشارة انفجار"
    details = r.get("breakout_details", {})
    factors = []
    if details.get("trend_support"):
        factors.append("trend_support")
    if details.get("macd_bull"):
        factors.append("macd_bull")
    if details.get("rsi_ok"):
        factors.append("rsi_ok")
    source_label = "+".join(factors)

    lines = [
        f"{dot} {title}",
        r['symbol'].replace('USDT', '/USDT'),
    ]
    if source_label:
        lines.append(f"المصدر: {source_label}")

    if r.get("breakout_entry") is not None:
        lines.append(f"الدخول: {r['breakout_entry']:.6g}")
        for i, tp in enumerate(r.get("breakout_tps", []), start=1):
            lines.append(f"TP{i}: {tp:.6g}")
        lines.append(f"SL: {r['breakout_sl']:.6g}")

    return "\n".join(lines)


# ---------------- إدارة الحالة عبر Gist ----------------

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")
GIST_FILENAME = "alerted_state.json"
POSITIONS_GIST_FILE = "open_positions.json"
CLOSED_GIST_FILE = "closed_trades.json"
STATS_GIST_FILE = "stats.json"
ACTIVE_HISTORY_SIZE = 150
ARCHIVE_PREFIX = "closed_trades_archive_"
ARCHIVE_CHUNK_SIZE = 150
DOM_SHIFT_THRESHOLD = float(os.environ.get("DOM_SHIFT_THRESHOLD", "0.3"))


def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


class GistFetchError(Exception):
    pass


def _gist_get_all_files():
    if not GIST_TOKEN or not GIST_ID:
        print("⚠️ GIST_TOKEN أو GIST_ID غير موجودين — سيعمل البوت بذاكرة فارغة هذا التشغيل.")
        return {}
    try:
        r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=_gist_headers(), timeout=15)
        r.raise_for_status()
        return r.json().get("files", {})
    except Exception as e:
        raise GistFetchError(f"تعذّر قراءة ملفات Gist: {e}") from e


def _gist_get_file(filename, gist_files):
    if filename not in gist_files:
        return None
    return gist_files[filename]["content"]


def archive_overflow(overflow_trades, gist_files):
    if not overflow_trades:
        return {}

    archive_names = sorted(fn for fn in gist_files if fn.startswith(ARCHIVE_PREFIX))
    if archive_names:
        last_name = archive_names[-1]
        idx = int(last_name[len(ARCHIVE_PREFIX):].replace(".json", ""))
        try:
            last_content = json.loads(gist_files[last_name].get("content") or "[]")
        except Exception:
            last_content = []
    else:
        idx = 1
        last_content = []

    files_to_write = {}
    remaining = list(overflow_trades)

    space = ARCHIVE_CHUNK_SIZE - len(last_content)
    if space > 0 and remaining:
        last_content.extend(remaining[:space])
        remaining = remaining[space:]
        files_to_write[f"{ARCHIVE_PREFIX}{idx:04d}.json"] = json.dumps(last_content, ensure_ascii=False, separators=(',', ':'))

    while remaining:
        idx += 1
        chunk = remaining[:ARCHIVE_CHUNK_SIZE]
        remaining = remaining[ARCHIVE_CHUNK_SIZE:]
        files_to_write[f"{ARCHIVE_PREFIX}{idx:04d}.json"] = json.dumps(chunk, ensure_ascii=False, separators=(',', ':'))

    return files_to_write


def _gist_patch_files(files_dict):
    if not GIST_TOKEN or not GIST_ID:
        print("⚠️ GIST_TOKEN أو GIST_ID غير موجودين — تخطي الحفظ.")
        return False

    payload = {"files": {fn: {"content": content} for fn, content in files_dict.items()}}
    payload_size = len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))
    print(f"💾 حجم Payload للحفظ في Gist: {payload_size:,} بايت | ملفات: {list(files_dict.keys())}")

    last_err = None
    for attempt in range(1, 4):
        try:
            r = requests.patch(
                f"https://api.github.com/gists/{GIST_ID}",
                headers=_gist_headers(),
                json=payload,
                timeout=20
            )
            if r.ok:
                print(f"✅ تم الحفظ في Gist بنجاح (محاولة {attempt})")
                return True
            if r.status_code == 429:
                wait = 5 * attempt
                print(f"⏳ Rate limit (429) — انتظار {wait} ثانية...")
                time.sleep(wait)
                continue
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            print(f"⚠️ فشل حفظ Gist (محاولة {attempt}): {last_err}")
        except Exception as e:
            last_err = str(e)
            print(f"⚠️ خطأ شبكي بحفظ Gist (محاولة {attempt}): {last_err}")
        if attempt < 3:
            time.sleep(2 * attempt)

    print(f"❌ فشل الحفظ في Gist نهائيًا بعد 3 محاولات: {last_err}")
    print(f"   ⚠️ الصفقات المفتوحة لم تُحفظ — ستُفقد في التشغيلة القادمة!")
    return False


def load_state(gist_files):
    content = _gist_get_file(GIST_FILENAME, gist_files)
    if not content:
        return set(), None
    try:
        data = json.loads(content)
        return set(data.get("alerted", [])), data.get("btc_dominance_prev")
    except Exception as e:
        print(f"تعذّر تحليل حالة Gist ({e}) — سيبدأ البوت بذاكرة فارغة.")
        return set(), None


def load_positions(gist_files):
    content = _gist_get_file(POSITIONS_GIST_FILE, gist_files)
    if not content:
        return []
    try:
        return json.loads(content)
    except Exception as e:
        print(f"تعذّر تحليل open_positions من Gist ({e})")
        return []


def load_closed(gist_files):
    content = _gist_get_file(CLOSED_GIST_FILE, gist_files)
    if not content:
        return []
    try:
        return json.loads(content)
    except Exception:
        return []


def compute_stats(history):
    if not history:
        return None

    total = len(history)
    wins = losses = neutral = 0
    pnl_list, durations = [], []
    by_type, by_score, by_reason = {}, {}, {}

    for t in history:
        reason = t.get("closed_reason", "UNKNOWN")
        by_reason[reason] = by_reason.get(reason, 0) + 1

        hit = len(t.get("hit_tps") or [])
        entry, exit_price = t.get("entry"), t.get("exit_price")
        if entry and exit_price:
            pnl_list.append((exit_price - entry) / entry * 100)

        if reason == "ALL_TP" or hit > 0:
            wins += 1
            outcome = "win"
        elif reason == "SL" and hit == 0:
            losses += 1
            outcome = "loss"
        else:
            neutral += 1
            outcome = "neutral"

        try:
            t0 = dt.datetime.strptime(t["opened_at"], "%Y-%m-%d %H:%M:%S")
            t1 = dt.datetime.strptime(t["closed_at"], "%Y-%m-%d %H:%M:%S")
            durations.append((t1 - t0).total_seconds() / 3600)
        except Exception:
            pass

        ttype = t.get("type", "official")
        b1 = by_type.setdefault(ttype, {"total": 0, "win": 0, "loss": 0, "neutral": 0})
        b1["total"] += 1
        b1[outcome] += 1

        score = t.get("score")
        if score is not None:
            key = str(int(score)) if isinstance(score, (int, float)) else "?"
            b2 = by_score.setdefault(key, {"total": 0, "win": 0, "loss": 0, "neutral": 0})
            b2["total"] += 1
            b2[outcome] += 1

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "neutral": neutral,
        "win_rate_pct": round(wins / total * 100, 1),
        "avg_pnl_pct": round(sum(pnl_list) / len(pnl_list), 2) if pnl_list else None,
        "avg_duration_hours": round(sum(durations) / len(durations), 1) if durations else None,
        "by_type": by_type,
        "by_score": by_score,
        "by_reason": by_reason,
        "computed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_all_state(alerted_symbols, btc_dominance, positions, closed_delta, gist_files):
    files = {
        GIST_FILENAME: json.dumps(
            {"alerted": sorted(alerted_symbols), "btc_dominance_prev": btc_dominance},
            ensure_ascii=False, separators=(',', ':')
        ),
    }

    try:
        old_positions = json.loads(_gist_get_file(POSITIONS_GIST_FILE, gist_files) or "[]")
    except Exception:
        old_positions = []
    if old_positions != positions:
        files[POSITIONS_GIST_FILE] = json.dumps(positions, ensure_ascii=False, separators=(',', ':'))

    stats = None
    if closed_delta:
        try:
            history = json.loads(gist_files.get(CLOSED_GIST_FILE, {}).get("content") or "[]")
        except Exception:
            history = []
        history.extend(closed_delta)

        if len(history) > ACTIVE_HISTORY_SIZE:
            overflow = history[:-ACTIVE_HISTORY_SIZE]
            history = history[-ACTIVE_HISTORY_SIZE:]
            files.update(archive_overflow(overflow, gist_files))

        files[CLOSED_GIST_FILE] = json.dumps(history, ensure_ascii=False, separators=(',', ':'))

        stats = compute_stats(history)
        if stats:
            files[STATS_GIST_FILE] = json.dumps(stats, ensure_ascii=False, separators=(',', ':'))

    saved_ok = _gist_patch_files(files)
    if not saved_ok:
        print("❌ لم يُحفظ شيء في Gist — الصفقات المفتوحة والسجل المغلق غير محفوظين!")
    return stats


# ---------------- تتبع الصفقات المفتوحة ----------------

def open_new_positions(positions, fresh_signals):
    for r in fresh_signals:
        if r.get("entry") is None:
            continue
        positions.append({
            "symbol": r["symbol"],
            "entry": r["entry"],
            "sl": r["sl"],
            "tps": r["tps"],
            "hit_tps": [],
            "tp_notify_ids": [None] * len(r["tps"]),
            "score": r["score"],
            "trend_up": r["trend_up"],
            "interval": INTERVAL,
            "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "type": "official",
            "alert_message_id": r.get("_msg_id"),
            "alert_text": r.get("_alert_text"),
        })


def open_new_early_positions(positions, fresh_early_signals):
    for r in fresh_early_signals:
        if r.get("early_entry") is None:
            continue
        positions.append({
            "symbol": r["symbol"],
            "entry": r["early_entry"],
            "sl": r["early_sl"],
            "tps": r["early_tps"],
            "hit_tps": [],
            "tp_notify_ids": [None] * len(r["early_tps"]),
            "score": r["score"],
            "trend_up": r["trend_up"],
            "interval": INTERVAL,
            "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "type": "early",
            "confidence": r.get("early_confidence"),
            "silent": r.get("early_confidence") == "احتمالية",
            "source": r.get("early_source"),
            "factors": r.get("early_factors"),
            "alert_message_id": r.get("_msg_id"),
            "alert_text": r.get("_alert_text"),
        })


def open_new_breakout_positions(positions, fresh_breakout_signals):
    for r in fresh_breakout_signals:
        if r.get("breakout_entry") is None:
            continue
        positions.append({
            "symbol": r["symbol"],
            "entry": r["breakout_entry"],
            "sl": r["breakout_sl"],
            "tps": r["breakout_tps"],
            "hit_tps": [],
            "tp_notify_ids": [None] * len(r["breakout_tps"]),
            "score": r["breakout_score"],
            "trend_up": r["trend_up"],
            "interval": INTERVAL,
            "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "type": "breakout",
            "breakout_details": r.get("breakout_details"),
            "alert_message_id": r.get("_msg_id"),
            "alert_text": r.get("_alert_text"),
        })


TIME_STOP_HOURS = float(os.environ.get("TIME_STOP_HOURS", "96"))


def _hours_since(opened_at_str):
    try:
        opened = time.strptime(opened_at_str, "%Y-%m-%d %H:%M:%S")
        opened_epoch = time.mktime(opened)
        return (time.time() - opened_epoch) / 3600
    except Exception:
        return 0


def format_duration(hours):
    total_minutes = round(hours * 60)
    days, rem_minutes = divmod(total_minutes, 24 * 60)
    hrs, minutes = divmod(rem_minutes, 60)

    def hours_word(n):
        if n == 1:
            return "ساعة"
        if n == 2:
            return "ساعتين"
        if 3 <= n <= 10:
            return f"{n} ساعات"
        return f"{n} ساعة"

    parts = []
    if days:
        parts.append("يوم" if days == 1 else ("يومين" if days == 2 else f"{days} أيام"))
    if hrs:
        parts.append(hours_word(hrs))
    if not parts:
        parts.append(f"{minutes} دقيقة")
    return " و".join(parts)


def format_sl_hit(pos, price):
    entry = pos["entry"]
    sl = pos["sl"]
    pct_drop = (sl - entry) / entry * 100
    duration = format_duration(_hours_since(pos["opened_at"]))
    type_labels = {"early": "❌ (إشارة مبكرة) ", "breakout": "❌ (إشارة انفجار) "}
    header = type_labels.get(pos.get("type"), "❌ ")
    return (
        f"{header}{pos['symbol'].replace('USDT', '/USDT')}\n"
        f"سعر الدخول: {entry:.6g}\n"
        f"SL: {sl:.6g}\n"
        f"نسبة النزول: {pct_drop:.2f}%\n"
        f"المدة الزمنية لضرب وقف الخسارة: {duration}"
    )


def format_tp_hit(pos, tp_index, price):
    entry = pos["entry"]
    tp = pos["tps"][tp_index]
    pct_gain = (tp - entry) / entry * 100
    duration = format_duration(_hours_since(pos["opened_at"]))
    type_labels = {"early": "✅ (إشارة مبكرة) ", "breakout": "✅ (إشارة انفجار) "}
    header = type_labels.get(pos.get("type"), "✅ ")
    tp_label = f"TP{tp_index + 1}"
    return (
        f"{header}{pos['symbol'].replace('USDT', '/USDT')}\n"
        f"سعر الدخول: {entry:.6g}\n"
        f"{tp_label}: {tp:.6g}\n"
        f"نسبة الصعود: +{pct_gain:.2f}%\n"
        f"المدة الزمنية لتحقيق الهدف: {duration}"
    )


def check_open_positions(positions, price_map):
    still_open, closed_now = [], []

    for pos in positions:
        price = price_map.get(pos["symbol"])
        if price is None:
            still_open.append(pos)
            continue

        if price <= pos["sl"]:
            if not pos.get("silent"):
                result_text = format_sl_hit(pos, price)
                send_telegram(result_text)
                edit_telegram_strike(pos.get("alert_message_id"), build_progress_text(pos), result_text)
                time.sleep(1)
            pos["closed_reason"] = "SL"
            pos["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            pos["exit_price"] = price
            closed_now.append(pos)
            continue

        newly_hit = [i for i, tp in enumerate(pos["tps"]) if i not in pos["hit_tps"] and price >= tp]
        if newly_hit:
            if "tp_notify_ids" not in pos or len(pos["tp_notify_ids"]) != len(pos["tps"]):
                pos["tp_notify_ids"] = [None] * len(pos["tps"])

            for i in newly_hit:
                if not pos.get("silent"):
                    tp_text = format_tp_hit(pos, i, price)
                    msg_id = send_telegram(tp_text)
                    pos["tp_notify_ids"][i] = msg_id
                    time.sleep(1)

                    prev_index = i - 1
                    if prev_index >= 0 and pos["tp_notify_ids"][prev_index]:
                        delete_telegram_message(pos["tp_notify_ids"][prev_index])
                        pos["tp_notify_ids"][prev_index] = None

                pos["hit_tps"].append(i)

                if not pos.get("silent"):
                    hit_sorted = sorted(pos["hit_tps"])
                    lines = [format_tp_line(pos, j) for j in hit_sorted]
                    edit_telegram_append(pos.get("alert_message_id"), pos.get("alert_text", ""), lines)

            if pos["sl"] < pos["entry"]:
                pos["sl"] = pos["entry"]

        if len(pos["hit_tps"]) >= len(pos["tps"]):
            if not pos.get("silent"):
                all_tp_text = "🏁 تحققت جميع الأهداف"
                edit_telegram_strike(pos.get("alert_message_id"), build_progress_text(pos), all_tp_text)
            pos["closed_reason"] = "ALL_TP"
            pos["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            pos["exit_price"] = price
            closed_now.append(pos)
            continue

        hours_open = _hours_since(pos["opened_at"])
        if hours_open >= TIME_STOP_HOURS:
            pct_change = (price - pos["entry"]) / pos["entry"] * 100
            status = "بربح" if pct_change > 0 else ("بخسارة" if pct_change < 0 else "بدون تغيير")
            if not pos.get("silent"):
                expired_text = (
                    f"⏱️ انتهت صلاحية المراقبة (سقف زمني) — متوقفة {status}\n{pos['symbol'].replace('USDT','/USDT')}\n"
                    f"الدخول: {pos['entry']:.6g} | الحالي: {price:.6g} | مدة المراقبة: {hours_open:.0f}س\n"
                    f"النسبة: {pct_change:+.2f}%"
                )
                send_telegram(expired_text)
                edit_telegram_strike(pos.get("alert_message_id"), build_progress_text(pos), expired_text)
                time.sleep(1)
            pos["closed_reason"] = "EXPIRED"
            pos["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            pos["exit_price"] = price
            closed_now.append(pos)
            continue

        still_open.append(pos)

    if closed_now:
        print(f"صفقات أُغلقت هذا المسح: {len(closed_now)}")

    return still_open, closed_now


# ---------------- BTC Dominance ----------------

def fetch_btc_dominance():
    r = _request_with_retry("https://api.coingecko.com/api/v3/global")
    return r.json()["data"]["market_cap_percentage"]["btc"]


# ---------------- التشغيل الرئيسي ----------------

def main():
    print(f"بدء المسح — {time.strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        gist_files = _gist_get_all_files()
    except GistFetchError as e:
        print(f"⛔ {e} — تم إيقاف هذا التشغيل بالكامل بدل الكتابة فوق الحالة الصحيحة.")
        return

    tickers = fetch_ticker24h()
    price_map = fetch_prices_map(tickers)

    print(f"📊 price_map يحتوي على {len(price_map)} رمز")

    open_positions = load_positions(gist_files)
    print(f"📋 الصفقات المفتوحة المحمّلة من Gist: {len(open_positions)}")
    if open_positions:
        missing = [p["symbol"] for p in open_positions if p["symbol"] not in price_map]
        if missing:
            print(f"⚠️ رموز مفقودة من price_map: {missing}")
    open_positions, closed_now = check_open_positions(open_positions, price_map)
    print(f"🔒 صفقات متبقية مفتوحة: {len(open_positions)} | أُغلقت الآن: {len(closed_now)}")

    results = run_scan(tickers)

    # الإشارة الرسمية: تخفيف الفلاتر
    strong = [
        r for r in results
        if r["score"] >= 1.0  # تخفيض من 1.5 إلى 1.0
        and r["vol_confirm"]  # الإبقاء على تأكيد الحجم فقط
        and meets_min_profit(r["entry"], r["tps"])
    ]
    strong_symbols = {r["symbol"] for r in strong}

    # تشخيص
    _score_ok = [r for r in results if r["score"] >= 1.0]
    _also_vol = [r for r in _score_ok if r["vol_confirm"]]
    _final = [r for r in _also_vol if meets_min_profit(r["entry"], r["tps"])]
    print(
        "🔍 تشخيص فلترة الرسمية: "
        f"score>=1.0: {len(_score_ok)} | "
        f"+حجم: {len(_also_vol)} | "
        f"+ربح أدنى 1% (strong نهائي): {len(_final)}"
    )

    # إشارات مبكرة
    early_eligible = [
        r for r in results
        if r["score"] < 1.0 and (r["squeeze"] or r["accumulation"])
        and r.get("early_confidence") is not None
        and meets_min_profit(r["early_entry"], r["early_tps"])
    ]
    early_keys = {f"{r['symbol']}:early" for r in early_eligible}

    # إشارات انفجار
    breakout_eligible = [
        r for r in results
        if r.get("breakout_entry") is not None
        and r["symbol"] not in strong_symbols
        and meets_min_profit(r["breakout_entry"], r["breakout_tps"])
    ]
    breakout_keys = {f"{r['symbol']}:breakout" for r in breakout_eligible}

    prev_alerted, prev_dominance = load_state(gist_files)
    fresh = [r for r in strong if r["symbol"] not in prev_alerted]
    fresh_early = [r for r in early_eligible if f"{r['symbol']}:early" not in prev_alerted]
    fresh_breakout = [r for r in breakout_eligible if f"{r['symbol']}:breakout" not in prev_alerted]

    btc_dominance = None
    market_caution = False
    try:
        btc_dominance = fetch_btc_dominance()
        if prev_dominance is not None:
            shift = btc_dominance - prev_dominance
            market_caution = abs(shift) >= DOM_SHIFT_THRESHOLD
            print(f"BTC Dominance: {btc_dominance:.2f}% (تغيّر {shift:+.2f} نقطة)")
        else:
            print(f"BTC Dominance: {btc_dominance:.2f}% (أول قراءة)")
    except Exception as e:
        print("تعذّر جلب BTC Dominance:", e)

    print(f"إشارات قوية حاليًا: {len(strong)} | جديدة: {len(fresh)} | "
          f"إشارات مبكرة جديدة: {len(fresh_early)} | إشارات انفجار جديدة: {len(fresh_breakout)}")

    for r in fresh:
        caution = market_caution and not r["symbol"].startswith("BTC")
        alert_text = format_alert(r, caution)
        r["_msg_id"] = send_telegram(alert_text)
        r["_alert_text"] = alert_text
        time.sleep(1)

    for r in fresh_early:
        if r.get("early_confidence") == "احتمالية":
            continue
        alert_text = format_early_alert(r)
        r["_msg_id"] = send_telegram(alert_text)
        r["_alert_text"] = alert_text
        time.sleep(1)

    for r in fresh_breakout:
        alert_text = format_breakout_alert(r)
        r["_msg_id"] = send_telegram(alert_text)
        r["_alert_text"] = alert_text
        time.sleep(1)

    open_new_positions(open_positions, fresh)
    open_new_early_positions(open_positions, fresh_early)
    open_new_breakout_positions(open_positions, fresh_breakout)

    save_all_state(strong_symbols | early_keys | breakout_keys, btc_dominance, open_positions, closed_now, gist_files)

    print("انتهى المسح.")


if __name__ == "__main__":
    main()
