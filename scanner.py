"""
ماسح السوق — نسخة البايثون (تعمل بجدولة تلقائية عبر GitHub Actions)
نفس منطق أداة HTML: فلترة سيولة/حركة -> تحليل عميق -> تأكيد فريم أعلى -> استقرار -> تنبيه تيليجرام

يضيف أيضًا مسارًا مستقلاً لـ"إشارات مبكرة" (انضغاط تقلب / تراكم صامت) لعملات لم تصل بعد
لإشارة شراء كاملة، كتحذير رادار بدون خطة دخول مؤكدة — لتفادي مشكلة "شراء القمة" حيث
الإشارة الرسمية تصل بعد ما الحركة صارت واضحة للجميع.

يضيف كذلك فلتر "إرهاق/امتداد زائد" (Overextension) يعاقب درجة الإشارات الرسمية نفسها لو
السعر بعيد جدًا عن EMA50 بوحدات ATR — لمعالجة نفس مشكلة "شراء القمة" من جهة الإشارة
الرسمية مباشرة، وليس فقط عبر تحذير مبكر منفصل.
"""

import os
import json
import time
import concurrent.futures
import requests

# ---------- الإعدادات (تُقرأ من متغيرات البيئة / GitHub Secrets) ----------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
INTERVAL = os.environ.get("SCAN_INTERVAL", "1h")          # 15m / 1h / 4h / 1d
DEPTH = int(os.environ.get("SCAN_DEPTH", "40"))            # عدد العملات للفحص العميق
SCAN_LIMIT = 400                                            # عدد الشموع التاريخية لكل عملة
LIQUIDITY_FLOOR = 1_000_000                                 # أدنى سيولة 24س بالدولار

EXCLUDE_SUFFIX = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
EXCLUDE_SYMS = {"USDCUSDT","FDUSDUSDT","TUSDUSDT","DAIUSDT","USDPUSDT",
                "EURUSDT","GBPUSDT","AEURUSDT","BFUSDUSDT"}

HTF_MAP = {"15m": "1h", "1h": "4h", "4h": "1d", "1d": "1w"}

BASE_URL = "https://data-api.binance.vision/api/v3"

# ---------- إعدادات فلاتر التحليل الإضافية (ADX / الانحراف / المقاومة / OBV) ----------
ADX_PERIOD = 14
ADX_THRESHOLD = 20              # تحت هذا المستوى يُعتبر السوق عرضيًا (بلا اتجاه واضح)
DIVERGENCE_LOOKBACK = 20        # عدد الشموع للبحث فيها عن قيعان سعرية للمقارنة مع RSI
DIVERGENCE_PIVOT_SPAN = 3       # عدد الشموع على كل جانب لاعتبار نقطة "قاع محلي"
RESISTANCE_LOOKBACK = 50        # عدد الشموع للبحث فيها عن أقرب مقاومة سابقة
RESISTANCE_PIVOT_SPAN = 3       # عدد الشموع على كل جانب لاعتبار نقطة "قمة محلية"
RESISTANCE_PROXIMITY_PCT = 1.5  # لو السعر أقرب من هذه النسبة% لمقاومة فوقه -> تحذير
OBV_TREND_WINDOW = 10           # عدد الشموع لقياس اتجاه OBV مقابل اتجاه السعر

# ---------- إعدادات فلتر الإرهاق/الامتداد الزائد (Overextension) ----------
EXTENSION_EMA_PERIOD = 50       # المتوسط المتحرك المرجعي لقياس "المسافة المقطوعة" عن خط الأساس
EXTENSION_ATR_THRESHOLD = float(os.environ.get("EXTENSION_ATR_THRESHOLD", "3.0"))
# المسافة بين السعر وEMA50 بوحدات ATR — فوق هذا الحد يُعتبر السعر ممتدًا بشكل مفرط (احتمال شراء متأخر)

# ---------- إعدادات الإشارات المبكرة (انضغاط تقلب / تراكم صامت) ----------
SQUEEZE_LOOKBACK = 20           # عدد الشموع لحساب متوسط عرض نطاق Bollinger
SQUEEZE_RATIO_THRESHOLD = 0.6   # عرض النطاق الحالي <= هذه النسبة من المتوسط -> يُعتبر انضغاطًا
ACCUM_WINDOW = 20               # عدد الشموع لقياس التراكم الصامت
ACCUM_PRICE_MAX_MOVE_PCT = 4.0  # أقصى تحرك سعري% خلال النافذة كي يُعتبر السعر "شبه ثابت"
ACCUM_FLOW_RATIO_MIN = 0.3      # أدنى نسبة صافي تدفق شراء (OBV/حجم) كي يُعتبر تراكمًا واضحًا

# ---------------- إعدادات أهداف الإشارات المبكرة (تقديرية، أقل ثقة من الإشارة الرسمية) ----------------
# وقف خسارة أوسع من الإشارة الرسمية (1.5×ATR) لأن نقطة الدخول أقل دقة والتقلب حولها أعلى
EARLY_SL_ATR_MULT = 2.0
# عدد الأهداف والثقة يعتمدان مباشرة على عدد الشروط المتحققة (squeeze / accumulation / divergence / momentum):
# شرط واحد = احتمالية (هدف واحد)، شرطان = مؤكدة (هدفان)، 3 فأكثر = مؤكدة قوية (3-4 أهداف)


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
    """
    مؤشر قوة الاتجاه (ADX) — يميّز السوق المتجه بوضوح عن السوق العرضي المتذبذب.
    قيمة أقل من ~20 تعني غالبًا سوقًا بلا اتجاه واضح، حيث تكثر الإشارات الكاذبة.
    يرجع قائمة بنفس طول closes، بقيم None قبل اكتمال فترة الحساب.
    """
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
    """
    On-Balance Volume — يجمع الحجم مع اتجاه السعر، لكشف هل الحجم يدعم الحركة فعليًا
    أم أن الصعود/الهبوط يحدث بحجم ضعيف (أقل موثوقية).
    """
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
    """يتحقق هل اتجاه OBV خلال آخر window شمعة يتماشى مع اتجاه السعر (EMA9/21)."""
    if len(obv_vals) <= window:
        return False
    obv_slope_up = obv_vals[-1] > obv_vals[-1 - window]
    return obv_slope_up == trend_up


def volatility_squeeze(bb_upper, bb_lower, closes, lookback=SQUEEZE_LOOKBACK):
    """
    يكشف انضغاط تقلب (Squeeze): عرض نطاق Bollinger الحالي أضيق بشكل ملحوظ من متوسطه
    خلال آخر lookback شمعة — غالبًا يسبق حركة سعرية قوية (بالاتجاهين)، فهو مؤشر
    "ترقّب" وليس اتجاهًا بحد ذاته، ويُستخدم كإشارة مبكرة قبل تأكيد الاتجاه الكامل.
    """
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
    """
    يكشف تراكم صامت: صافي تدفق الشراء (OBV) نسبة لإجمالي الحجم المتداول خلال النافذة
    يميل بوضوح لضغط شراء، بينما السعر نفسه بالكاد تحرك -- إشارة على تجميع مركز
    قبل انعكاس سعري محتمل، دون انتظار تأكيد الاتجاه الكامل بالمؤشرات اللحظية.
    """
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
    """
    يكشف انحراف صعودي: السعر يصنع قاعًا أدنى من القاع السابق، بينما RSI يصنع قاعًا أعلى —
    من أقوى إشارات احتمال الانعكاس للأعلى عند المحترفين.
    """
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
    """يرجع أقرب مستوى مقاومة (قمة سعرية سابقة) فوق السعر الحالي، أو None لو لا توجد."""
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


def momentum_strength(macd_line, signal, rsi_vals, i):
    """
    قوة الزخم: تتحقق لما يكون MACD فوق خط الإشارة وهيستوغرام الفرق بينهما يتسع
    (الزخم يتسارع لا يتباطأ)، مع RSI في منطقة صاعدة (بين 45 و65: زخم بدون تشبع شرائي).
    """
    if i < 1 or macd_line[i] is None or signal[i] is None or rsi_vals[i] is None:
        return False
    hist_now = macd_line[i] - signal[i]
    hist_prev = macd_line[i - 1] - signal[i - 1]
    macd_bull = hist_now > 0 and hist_now > hist_prev
    rsi_rising = rsi_vals[i] > rsi_vals[i - 1] and 45 <= rsi_vals[i] <= 65
    return macd_bull and rsi_rising


def atr_value_at(ind, i, period=14):
    """
    نفس فكرة atr_value لكن عند شمعة i محددة (وليس دائمًا آخر شمعة) — يُستخدم لقياس
    الإرهاق/الامتداد عند نقطة زمنية معيّنة، ويسمح لنفس المنطق يشتغل حيًا وبالاختبار الرجعي.
    """
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
    """
    يكشف امتدادًا سعريًا مفرطًا: المسافة بين السعر الحالي وEMA50 بوحدات ATR فوق عتبة معيّنة،
    بمعنى أن العملة صعدت (أو هبطت) كثيرًا خلال فترة قصيرة نسبيًا — احتمال دخول متأخر
    (شراء القمة) حتى لو باقي المؤشرات اللحظية تبدو إيجابية.
    """
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
        "ema9": ema(closes, 9), "ema21": ema(closes, 21),
        "ema50": ema(closes, EXTENSION_EMA_PERIOD),
        "rsi": rsi(closes),
        "macd": macd_line, "signal": signal,
        "bb_upper": bb_upper, "bb_lower": bb_lower,
        "vol_avg": rolling_avg(vols, 20),
        "adx": adx(highs, lows, closes),
        "obv": obv(closes, vols),
    }


def score_at(i, ind, apply_extra_filters=True):
    if i < 26 or ind["bb_upper"][i] is None or ind["rsi"][i] is None or ind["vol_avg"][i] is None:
        return None
    trend_up = ind["ema9"][i] > ind["ema21"][i]
    rv = ind["rsi"][i]
    rsi_state = 1 if rv < 35 else (-1 if rv > 65 else 0)
    macd_bull = ind["macd"][i] > ind["signal"][i]
    price = ind["closes"][i]
    bb_state = 1 if price <= ind["bb_lower"][i] else (-1 if price >= ind["bb_upper"][i] else 0)
    vol_confirm = ind["vols"][i] > ind["vol_avg"][i] * 1.1
    trend_dir = 1 if trend_up else -1
    vol_score = trend_dir * 0.5 if vol_confirm else 0
    score = trend_dir + rsi_state + (1 if macd_bull else -1) + bb_state + vol_score

    # --- فلاتر إضافية لتحسين جودة الإشارة (ADX / انحراف / مقاومة / OBV / إرهاق) ---
    # تُحسب دائمًا للعرض التشخيصي، لكن تُطبَّق على الدرجة فقط لو apply_extra_filters=True
    # (يُستخدم False في الاختبار الرجعي لمقارنة الأداء بدونها)

    adx_val = ind["adx"][i] if i < len(ind["adx"]) else None
    ranging = adx_val is not None and adx_val < ADX_THRESHOLD

    divergence = bullish_divergence(ind["closes"][:i + 1], ind["rsi"][:i + 1])

    resistance = nearest_resistance(ind["highs"][:i + 1], ind["closes"][:i + 1])
    near_resistance = False
    if resistance:
        dist_pct = (resistance - price) / price * 100
        near_resistance = 0 <= dist_pct <= RESISTANCE_PROXIMITY_PCT

    obv_confirm = obv_confirms_trend(ind["obv"][:i + 1], trend_up)

    extended = overextended(ind, i, trend_up)

    if apply_extra_filters:
        if ranging:
            score *= 0.5
        if divergence:
            score += 1
        if near_resistance:
            score -= 1
        if obv_confirm:
            score += trend_dir * 0.5
        if extended:
            score -= trend_dir * 1  # عقوبة على الامتداد المفرط -- احتمال دخول متأخر (شراء القمة)

    return {
        "score": score, "trend_up": trend_up, "vol_confirm": vol_confirm, "rv": rv,
        "adx_val": adx_val, "ranging": ranging,
        "divergence": divergence,
        "near_resistance": near_resistance, "resistance": resistance,
        "obv_confirm": obv_confirm,
        "extended": extended,
    }


def atr_percent(ind, period=14):
    return atr_value(ind, period) / ind["closes"][-1] * 100


def atr_value(ind, period=14):
    """متوسط المدى الحقيقي بالقيمة المطلقة (وحدة السعر نفسها)، يُستخدم لحساب وقف خسارة يتناسب مع تقلب كل عملة."""
    n = len(ind["closes"])
    trs = []
    for i in range(n - period, n):
        prev_close = ind["closes"][i - 1] if i > 0 else ind["closes"][i]
        tr = max(ind["highs"][i] - ind["lows"][i],
                  abs(ind["highs"][i] - prev_close),
                  abs(ind["lows"][i] - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs)


# ---------------- جلب البيانات من Binance ----------------

def _request_with_retry(url, params=None, timeout=20, retries=3, backoff=1.5):
    """طلب HTTP مع إعادة محاولة تلقائية عند فشل الشبكة أو ضغط مؤقت من Binance (429/5xx)."""
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
                time.sleep(backoff * (attempt + 1))  # انتظار متزايد بين المحاولات
    raise last_err


def fetch_ticker24h():
    r = _request_with_retry(f"{BASE_URL}/ticker/24hr")
    return r.json()


def fetch_prices_map(tickers):
    """يبني قاموسًا {رمز: آخر سعر} من نفس بيانات ticker24h بدون طلب إضافي."""
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
    """
    يستبعد آخر شمعة إذا كانت لسا مفتوحة (لم تُغلق بعد وقت التشغيل)، لتفادي تحليل
    بيانات ناقصة قابلة للتغيّر (Repainting) — Binance ترجع الشمعة الجارية كآخر عنصر دائمًا.
    عنصر الشمعة: [open_time, open, high, low, close, volume, close_time, ...]
    """
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
        persistent = bool(prev_r) and (prev_r["score"] > 0) == (r["score"] > 0) and abs(prev_r["score"]) >= 1.5

        final_score = r["score"]
        htf_checked, htf_aligned = False, None
        if abs(r["score"]) >= 1:
            htf = HTF_MAP.get(interval)
            if htf:
                try:
                    htf_klines = fetch_klines(symbol, htf, 60)
                    htf_klines = drop_unclosed_candle(htf_klines)
                    htf_closes = [float(k[4]) for k in htf_klines]
                    htf_up = ema(htf_closes, 9)[-1] > ema(htf_closes, 21)[-1]
                    trend_dir = 1 if r["trend_up"] else -1
                    htf_aligned = htf_up == r["trend_up"]
                    final_score += (trend_dir * 0.5) if htf_aligned else (-trend_dir * 0.5)
                    htf_checked = True
                except Exception:
                    pass

        # إشارات مبكرة (انضغاط تقلب / تراكم صامت) — مستقلة عن الدرجة الرسمية، تُحسب دائمًا
        # للعرض، وتُستخدم لاحقًا فقط لعملات لم تصل بعد لإشارة شراء كاملة
        squeeze = volatility_squeeze(ind["bb_upper"], ind["bb_lower"], ind["closes"])
        accumulation = silent_accumulation(ind["closes"], ind["vols"], ind["obv"])

        # أهداف تقديرية للإشارة المبكرة نفسها (وليس فقط تحذير بدون أرقام):
        # وقف خسارة أوسع (ATR×2) لأن الدخول أقل تأكيدًا، وعدد أهداف حسب مستوى الثقة
        # (شرط واحد = احتمالية وهدف واحد، شرطان فأكثر = مؤكدة وهدفان)، مع تقليم أي هدف
        # يتجاوز أقرب مقاومة معروفة كي لا نضع هدفًا خلف حاجز سعري واضح.
        early_entry = early_sl = None
        early_tps = []
        early_confidence = None
        momentum = momentum_strength(ind["macd"], ind["signal"], ind["rsi"], last)
        if squeeze or accumulation:
            conditions_met = sum([squeeze, accumulation, r["divergence"], momentum])
            if conditions_met >= 3:
                early_confidence = "مؤكدة قوية"
            elif conditions_met >= 2:
                early_confidence = "مؤكدة"
            else:
                early_confidence = "احتمالية"
            early_entry = ind["closes"][last]
            atrv = atr_value(ind)
            early_sl = early_entry - atrv * EARLY_SL_ATR_MULT
            early_risk = early_entry - early_sl
            early_tp_count = conditions_met  # عدد الأهداف = عدد الشروط المتحققة فعليًا لهاي العملة (1 إلى 4)
            raw_tps = [early_entry + early_risk * i for i in range(1, early_tp_count + 1)]
            resistance = r.get("resistance")
            early_tps = [min(tp, resistance) for tp in raw_tps] if resistance else raw_tps

        # خطة دخول (شراء فقط — السوق الفوري لا يدعم فتح صفقة بيع مكشوفة)، محسوبة ديناميكيًا حسب التحليل:
        # وقف الخسارة من التقلب الفعلي (ATR) للعملة، وعدد الأهداف حسب قوة درجة التوافق
        entry = sl = None
        tps = []
        if final_score >= 1:
            entry = ind["closes"][last]
            atrv = atr_value(ind)
            sl = entry - atrv * 1.5
            risk = entry - sl

            if final_score >= 4:
                tp_count = 4
            elif final_score >= 3:
                tp_count = 3
            elif final_score >= 2.5:
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
            "htf_checked": htf_checked,
            "htf_aligned": htf_aligned,
            "ranging": r["ranging"],
            "divergence": r["divergence"],
            "near_resistance": r["near_resistance"],
            "obv_confirm": r["obv_confirm"],
            "extended": r["extended"],
            "squeeze": squeeze,
            "accumulation": accumulation,
            "momentum": momentum,
            "entry": entry, "sl": sl, "tps": tps,
            "early_entry": early_entry, "early_sl": early_sl, "early_tps": early_tps,
            "early_confidence": early_confidence,
        }
    except Exception as e:
        print(f"[تخطي] {symbol}: {e}")
        return None


# ---------------- المسح الكامل (مرحلتين) ----------------

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
    # ترتيب مركّب: يجمع بين رتبة السيولة الحالية ورتبة قوة الحركة، بدل الاعتماد على الحركة وحدها
    # (عملة عالية السيولة لكن حركتها المئوية بسيطة قد تكون أهم من عملة صغيرة تحركت كثيرًا نسبيًا)
    by_volume = sorted(liquid, key=lambda t: float(t["quoteVolume"]), reverse=True)
    by_momentum = sorted(liquid, key=lambda t: abs(float(t["priceChangePercent"])), reverse=True)
    volume_rank = {t["symbol"]: i for i, t in enumerate(by_volume)}
    momentum_rank = {t["symbol"]: i for i, t in enumerate(by_momentum)}
    combined = sorted(liquid, key=lambda t: volume_rank[t["symbol"]] + momentum_rank[t["symbol"]])
    shortlist = combined[:DEPTH]

    print(f"سيولة كافية: {len(liquid)} عملة | فحص عميق: {len(shortlist)} عملة | فريم: {INTERVAL}")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(analyze_symbol, t, INTERVAL) for t in shortlist]
        for f in concurrent.futures.as_completed(futures):
            r = f.result()
            if r:
                results.append(r)

    return results


# ---------------- تيليجرام ----------------

def send_telegram(text):
    """يرسل رسالة تيليجرام جديدة، ويرجع message_id الخاص فيها (أو None عند الفشل) —
    يُستخدم لاحقًا لتعديل نفس الرسالة (شطبها + إضافة النتيجة) عند إغلاق الصفقة."""
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
    """يهرب رموز HTML الخاصة قبل الإرسال بوضع parse_mode=HTML (تفاديًا لكسر التنسيق)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def edit_telegram_strike(message_id, original_text, result_text):
    """
    يعدّل رسالة تيليجرام الأصلية (الإشارة) بعد إغلاق الصفقة: يشطب نصها الأصلي (Strikethrough)
    ويضيف نتيجة الإغلاق تحته بنفس الرسالة — بالإضافة إلى رسالة النتيجة الجديدة المنفصلة،
    وليس بديلاً عنها.
    """
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
    """يحذف رسالة تيليجرام سابقة — يُستخدم لحذف إشعار هدف سابق عند تحقق هدف جديد بنفس الصفقة."""
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
    """
    يعدّل رسالة الإشارة الأصلية بإضافة سطر مختصر تحت نصها لكل هدف تحقق حتى الآن (تراكميًا،
    الأسطر السابقة تبقى كما هي ويُضاف الجديد تحتها) — بدون شطب النص، لأن هذا ليس إغلاقًا نهائيًا
    بمعنى "شطب واستبدال" بل تحديثًا مستمرًا لنفس رسالة الإشارة مع كل هدف.
    """
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


def tp_ordinal(i):
    words = ["الأول", "الثاني", "الثالث", "الرابع"]
    return words[i] if i < len(words) else f"رقم {i + 1}"


def format_tp_line(pos, tp_index):
    """سطر مختصر لهدف واحد متحقق (يُستخدم بالتعديل التراكمي على رسالة الإشارة الأصلية فقط)."""
    entry = pos["entry"]
    tp = pos["tps"][tp_index]
    pct_gain = (tp - entry) / entry * 100
    is_early = pos.get("type") == "early"
    ordinal = tp_ordinal(tp_index)
    label = f"الهدف التقديري {ordinal}" if is_early else f"الهدف {ordinal}"
    return f"✅ تحقق {label}: {tp:.6g} (+{pct_gain:.2f}%)"


def format_alert(r, market_caution=False):
    is_buy = r["score"] >= 2.5
    dot = "🟢" if is_buy else "🔴"
    title = "إشارة شراء" if is_buy else "تجنب شراء"

    badges = []
    if r["persistent"]:
        badges.append("مستقرة")
    if r["htf_checked"]:
        badges.append("متوافقة مع فريم أعلى" if r["htf_aligned"] else "تعاكس فريم أعلى")
    if r.get("divergence"):
        badges.append("انحراف صعودي مؤكد")
    if r.get("obv_confirm"):
        badges.append("حجم داعم (OBV)")
    if r.get("ranging"):
        badges.append("⚠️ سوق عرضي (ADX ضعيف)")
    if r.get("near_resistance"):
        badges.append("⚠️ قريب من مقاومة قوية")
    if r.get("extended"):
        badges.append("⚠️ حركة ممتدة (احتمال شراء متأخر)")
    badge_txt = f" ({', '.join(badges)})" if badges else ""

    lines = [
        f"{dot} {title}",
        r['symbol'].replace('USDT', '/USDT'),
        f"الدرجة: {r['score']:.1f} | فريم: {INTERVAL}{badge_txt}",
        f"السعر: {r['price']:.6g}",
    ]

    if r.get("entry") is not None:
        lines.append(f"الدخول: {r['entry']:.6g}")
        for i, tp in enumerate(r.get("tps", []), start=1):
            lines.append(f"TP{i}: {tp:.6g}")
        lines.append(f"وقف الخسارة: {r['sl']:.6g}")
    else:
        # السوق الفوري لا يدعم فتح صفقة بيع مكشوفة — فلا توجد خطة دخول لإشارات "تجنب شراء"
        lines.append("لا توجد خطة دخول (تحذير فقط)")

    if market_caution:
        lines.append("⚠️ سيطرة BTC (Dominance) تتحرك بقوة الآن — إشارات العملات البديلة أقل موثوقية مؤقتًا")

    return "\n".join(lines)


def format_early_alert(r):
    """
    تنبيه رادار مبكر: انضغاط تقلب و/أو تراكم صامت لعملة لم تصل بعد لإشارة شراء كاملة.
    يعرض أهدافًا تقديرية (وقف خسارة أوسع من الرسمية + هدف/هدفين حسب مستوى الثقة)،
    وتُتابَع تلقائيًا (TP/SL) ضمن نفس آلية الصفقات المفتوحة — لكنها تبقى أقل تأكيدًا
    من الإشارة الرسمية.
    """
    badges = []
    if r.get("squeeze"):
        badges.append("انضغاط تقلب (Squeeze)")
    if r.get("accumulation"):
        badges.append("تراكم صامت (OBV)")
    if r.get("divergence"):
        badges.append("انحراف صعودي")
    if r.get("momentum"):
        badges.append("قوة زخم")
    if r.get("extended"):
        badges.append("⚠️ حركة ممتدة (احتمال فوات الفرصة)")
    badge_txt = ", ".join(badges)

    confidence = r.get("early_confidence")
    dot = "🟢" if confidence == "مؤكدة قوية" else ("🟣" if confidence == "مؤكدة" else "🔵")
    title = f"إشارة مبكرة — {confidence}" if confidence else "إشارة مبكرة"

    lines = [
        f"{dot} {title}",
        r['symbol'].replace('USDT', '/USDT'),
        f"المؤشرات: {badge_txt}",
        f"الدرجة الحالية: {r['score']:.1f} | فريم: {INTERVAL}",
        f"السعر الحالي: {r['price']:.6g}",
    ]

    if r.get("early_entry") is not None:
        lines.append(f"الدخول التقديري: {r['early_entry']:.6g}")
        for i, tp in enumerate(r.get("early_tps", []), start=1):
            lines.append(f"هدف تقديري {i}: {tp:.6g}")
        lines.append(f"وقف خسارة تقديري: {r['early_sl']:.6g}")

    lines.append("⚠️ أهداف تقديرية أقل ثقة من الإشارة الرسمية — البوت سيتابعها تلقائيًا ويُشعرك عند تحقق هدف أو ضرب وقف الخسارة")
    return "\n".join(lines)


# ---------------- إدارة الحالة عبر GitHub Gist (بديل عن الكتابة داخل المستودع) ----------------

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")
GIST_FILENAME = "alerted_state.json"
POSITIONS_GIST_FILE = "open_positions.json"   # الصفقات المفتوحة قيد المتابعة (نفس الـ Gist، ملف منفصل)
CLOSED_GIST_FILE = "closed_trades.json"       # سجل الصفقات المغلقة (لإحصائية الأداء)
MAX_CLOSED_HISTORY = 300                      # سقف لعدد الصفقات المؤرشفة كي لا يتضخم الـ Gist بلا حدود
DOM_SHIFT_THRESHOLD = float(os.environ.get("DOM_SHIFT_THRESHOLD", "0.3"))  # نقطة مئوية خلال دورة تشغيل واحدة


def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def _gist_get_file(filename):
    """يقرأ محتوى ملف واحد داخل الـ Gist (يرجع None لو غير موجود أو حصل خطأ)."""
    if not GIST_TOKEN or not GIST_ID:
        return None
    try:
        r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=_gist_headers(), timeout=15)
        r.raise_for_status()
        files = r.json().get("files", {})
        if filename not in files:
            return None
        return files[filename]["content"]
    except Exception as e:
        print(f"تعذّر قراءة {filename} من Gist ({e})")
        return None


def _gist_patch_files(files_dict):
    """يحفظ عدة ملفات دفعة واحدة داخل نفس الـ Gist (الملفات غير المذكورة تبقى كما هي)."""
    if not GIST_TOKEN or not GIST_ID:
        return
    payload = {"files": {fn: {"content": content} for fn, content in files_dict.items()}}
    try:
        r = requests.patch(f"https://api.github.com/gists/{GIST_ID}", headers=_gist_headers(),
                            json=payload, timeout=15)
        if not r.ok:
            print("فشل حفظ الحالة في Gist:", r.text)
    except Exception as e:
        print("خطأ حفظ الحالة في Gist:", e)


def load_state():
    """يحمّل ذاكرة الإشارات المرسلة وآخر قيمة BTC Dominance من Gist خاص، بدل ملف داخل المستودع."""
    if not GIST_TOKEN or not GIST_ID:
        print("⚠️ GIST_TOKEN أو GIST_ID غير موجودين — سيبدأ البوت بذاكرة فارغة هذا التشغيل.")
        return set(), None
    content = _gist_get_file(GIST_FILENAME)
    if not content:
        return set(), None
    try:
        data = json.loads(content)
        return set(data.get("alerted", [])), data.get("btc_dominance_prev")
    except Exception as e:
        print(f"تعذّر تحليل حالة Gist ({e}) — سيبدأ البوت بذاكرة فارغة.")
        return set(), None


def load_positions():
    """يحمّل الصفقات المفتوحة قيد المتابعة من الـ Gist."""
    content = _gist_get_file(POSITIONS_GIST_FILE)
    if not content:
        return []
    try:
        return json.loads(content)
    except Exception as e:
        print(f"تعذّر تحليل open_positions من Gist ({e})")
        return []


def load_closed():
    content = _gist_get_file(CLOSED_GIST_FILE)
    if not content:
        return []
    try:
        return json.loads(content)
    except Exception:
        return []


def save_all_state(alerted_symbols, btc_dominance, positions, closed_delta):
    """
    يحفظ في نفس الطلب: حالة التنبيهات + BTC Dominance + الصفقات المفتوحة،
    ويُلحق أي صفقات أُغلقت هذا التشغيل بسجل closed_trades (مع سقف للحجم).
    """
    files = {
        GIST_FILENAME: json.dumps(
            {"alerted": sorted(alerted_symbols), "btc_dominance_prev": btc_dominance},
            ensure_ascii=False
        ),
        POSITIONS_GIST_FILE: json.dumps(positions, ensure_ascii=False, indent=2),
    }
    if closed_delta:
        history = load_closed()
        history.extend(closed_delta)
        if len(history) > MAX_CLOSED_HISTORY:
            history = history[-MAX_CLOSED_HISTORY:]
        files[CLOSED_GIST_FILE] = json.dumps(history, ensure_ascii=False, indent=2)

    _gist_patch_files(files)


# ---------------- تتبع الصفقات المفتوحة (TP / SL) ----------------

def open_new_positions(positions, fresh_signals):
    """يضيف كل إشارة شراء جديدة أُرسلت كصفقة مفتوحة قيد المتابعة. يُعدّل القائمة في المكان (in place)."""
    for r in fresh_signals:
        if r.get("entry") is None:
            continue  # لا خطة دخول (تجنب شراء) -> لا داعي لتتبعها
        positions.append({
            "symbol": r["symbol"],
            "entry": r["entry"],
            "sl": r["sl"],
            "tps": r["tps"],
            "hit_tps": [],
            "tp_notify_ids": [None] * len(r["tps"]),
            "score": r["score"],
            "trend_up": r["trend_up"],   # اتجاه EMA9/21 وقت فتح الصفقة، يُستخدم لاحقًا لكشف انعكاس الإشارة
            "interval": INTERVAL,
            "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "type": "official",
            # حقول تشخيصية: أي عوامل كانت حاضرة وقت الدخول -> تحليل لاحق لأثر كل عامل على النجاح/الفشل
            "squeeze": r.get("squeeze"),
            "accumulation": r.get("accumulation"),
            "divergence": r.get("divergence"),
            "extended": r.get("extended"),
            # message_id ونص رسالة الإشارة الأصلية -> تُستخدم لاحقًا لتعديل نفس الرسالة (شطب + نتيجة) عند الإغلاق
            "alert_message_id": r.get("_msg_id"),
            "alert_text": r.get("_alert_text"),
        })


def open_new_early_positions(positions, fresh_early_signals):
    """
    يفتح متابعة تلقائية (TP/SL) لإشارات مبكرة توفّرت لها أهداف تقديرية، بنفس آلية
    الصفقات الرسمية لكن بحقل type="early" يُستخدم لاحقًا لتمييز رسائل النتيجة.
    """
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
            # نفس الحقول التشخيصية للإشارات المبكرة، عشان نعرف أي مزيج (squeeze/accumulation/divergence)
            # فرّق فعليًا بين "احتمالية" ناجحة و"مؤكدة" فاشلة، بدل ما نكتفي بتصنيف الثقة العام
            "squeeze": r.get("squeeze"),
            "accumulation": r.get("accumulation"),
            "divergence": r.get("divergence"),
            "extended": r.get("extended"),
            "alert_message_id": r.get("_msg_id"),
            "alert_text": r.get("_alert_text"),
        })


TIME_STOP_HOURS = float(os.environ.get("TIME_STOP_HOURS", "96"))  # سقف زمني أقصى (شبكة أمان) قبل اعتبار الصفقة منتهية الصلاحية — افتراضيًا 4 أيام


def _hours_since(opened_at_str):
    try:
        opened = time.strptime(opened_at_str, "%Y-%m-%d %H:%M:%S")
        opened_epoch = time.mktime(opened)
        return (time.time() - opened_epoch) / 3600
    except Exception:
        return 0


def format_duration(hours):
    """يحوّل عدد الساعات لصيغة مقروءة: أيام + ساعات، بجمع عربي مبسّط."""
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
    is_early = pos.get("type") == "early"
    header = "❌ (إشارة مبكرة) " if is_early else "❌ "
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
    is_early = pos.get("type") == "early"
    header = "✅ (إشارة مبكرة) " if is_early else "✅ "
    tp_label = f"هدف تقديري {tp_index + 1}" if is_early else f"TP{tp_index + 1}"
    return (
        f"{header}{pos['symbol'].replace('USDT', '/USDT')}\n"
        f"سعر الدخول: {entry:.6g}\n"
        f"{tp_label}: {tp:.6g}\n"
        f"نسبة الصعود: +{pct_gain:.2f}%\n"
        f"المدة الزمنية لتحقيق الهدف: {duration}"
    )


def format_invalidated(pos, price):
    """نتيجة إغلاق محايدة لصفقة انعكس اتجاهها قبل تحقيق أي هدف أو ضرب وقف خسارة."""
    entry = pos["entry"]
    pct = (price - entry) / entry * 100
    duration = format_duration(_hours_since(pos["opened_at"]))
    is_early = pos.get("type") == "early"
    header = "⚪ (إشارة مبكرة) " if is_early else "⚪ "
    return (
        f"{header}{pos['symbol'].replace('USDT', '/USDT')}\n"
        f"انعكس الاتجاه قبل تحقيق أي هدف\n"
        f"الدخول: {entry:.6g} | الخروج: {price:.6g}\n"
        f"النتيجة الصافية: {pct:+.2f}%\n"
        f"المدة الزمنية: {duration}"
    )


def trend_reversed(symbol, interval, original_trend_up):
    """
    يفحص هل انعكس اتجاه EMA9/21 منذ فتح الصفقة (إبطال الإشارة الأصلية).
    يرجع True/False عند نجاح الفحص، أو None لو تعذّر الجلب (لا نغلق الصفقة بالخطأ في هذه الحالة).
    """
    try:
        klines = fetch_klines(symbol, interval, limit=30)
        klines = drop_unclosed_candle(klines)
        closes = [float(k[4]) for k in klines]
        if len(closes) < 22:
            return None
        current_trend_up = ema(closes, 9)[-1] > ema(closes, 21)[-1]
        return current_trend_up != original_trend_up
    except Exception:
        return None


def check_open_positions(positions, price_map):
    """
    يقارن الصفقات المفتوحة بالسعر الحالي، يرسل إشعار تيليجرام عند تحقق هدف أو ضرب وقف خسارة،
    وينقل SL لنقطة الدخول (Breakeven) بمجرد لمس أول هدف. الإغلاق بسبب انعكاس الاتجاه (EMA)
    أو انتهاء السقف الزمني يبقى فعّالاً لإدارة المخاطر، لكن بدون إرسال إشعار تيليجرام له.
    يرجع (الصفقات المتبقية مفتوحة، الصفقات التي أُغلقت الآن).
    """
    still_open, closed_now = [], []

    for pos in positions:
        price = price_map.get(pos["symbol"])
        if price is None:
            still_open.append(pos)
            continue

        if price <= pos["sl"]:
            result_text = format_sl_hit(pos, price)
            send_telegram(result_text)
            edit_telegram_strike(pos.get("alert_message_id"), pos.get("alert_text", ""), result_text)
            pos["closed_reason"] = "SL"
            pos["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            pos["exit_price"] = price
            closed_now.append(pos)
            time.sleep(1)
            continue

        newly_hit = [i for i, tp in enumerate(pos["tps"]) if i not in pos["hit_tps"] and price >= tp]
        if newly_hit:
            if "tp_notify_ids" not in pos or len(pos["tp_notify_ids"]) != len(pos["tps"]):
                pos["tp_notify_ids"] = [None] * len(pos["tps"])  # توافق مع صفقات فُتحت قبل هذا التحديث

            for i in newly_hit:
                tp_text = format_tp_hit(pos, i, price)
                msg_id = send_telegram(tp_text)
                pos["tp_notify_ids"][i] = msg_id
                time.sleep(1)

                # احذف إشعار الهدف السابق المستقل (إن وُجد) كي لا تتراكم إشعارات منفصلة لكل هدف
                prev_index = i - 1
                if prev_index >= 0 and pos["tp_notify_ids"][prev_index]:
                    delete_telegram_message(pos["tp_notify_ids"][prev_index])
                    pos["tp_notify_ids"][prev_index] = None

                pos["hit_tps"].append(i)

                # عدّل رسالة الإشارة الأصلية تراكميًا: كل الأهداف المتحققة حتى الآن، كل واحد بسطره الخاص
                hit_sorted = sorted(pos["hit_tps"])
                lines = [format_tp_line(pos, j) for j in hit_sorted]
                edit_telegram_append(pos.get("alert_message_id"), pos.get("alert_text", ""), lines)

            if pos["sl"] < pos["entry"]:
                pos["sl"] = pos["entry"]  # نقل SL لنقطة التعادل بعد أول هدف محقق

        if len(pos["hit_tps"]) >= len(pos["tps"]):
            # كل الأهداف تحققت -> إغلاق داخلي للصفقة (بدون رسالة/تعديل إضافي، لأن كل هدف أُرسل وعُدّل بالتراكم أعلاه)
            pos["closed_reason"] = "ALL_TP"
            pos["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            pos["exit_price"] = price
            closed_now.append(pos)
            continue

        # لم يتحقق TP ولا SL بعد -> افحص إبطال الإشارة (انعكاس الاتجاه) قبل السقف الزمني.
        # لا تُرسل رسالة جديدة صاخبة (حسب طلب سابق)، لكن الرسالة الأصلية تُعدَّل (شطب + نتيجة محايدة)
        # كي تبقى كل صفقة مرئية النتيجة بالمحادثة، بدل ما تختفي بصمت.
        reversed_signal = trend_reversed(pos["symbol"], pos.get("interval", INTERVAL), pos["trend_up"])
        if reversed_signal:
            edit_telegram_strike(pos.get("alert_message_id"), pos.get("alert_text", ""),
                                  format_invalidated(pos, price))
            pos["closed_reason"] = "INVALIDATED"
            pos["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            pos["exit_price"] = price
            closed_now.append(pos)
            continue

        hours_open = _hours_since(pos["opened_at"])
        if hours_open >= TIME_STOP_HOURS:
            expired_text = (
                f"⏱️ انتهت صلاحية المراقبة (سقف زمني)\n{pos['symbol'].replace('USDT','/USDT')}\n"
                f"الدخول: {pos['entry']:.6g} | الحالي: {price:.6g} | مدة المراقبة: {hours_open:.0f}س"
            )
            send_telegram(expired_text)
            edit_telegram_strike(pos.get("alert_message_id"), pos.get("alert_text", ""), expired_text)
            pos["closed_reason"] = "EXPIRED"
            pos["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            pos["exit_price"] = price
            closed_now.append(pos)
            time.sleep(1)
            continue

        still_open.append(pos)

    if closed_now:
        print(f"صفقات أُغلقت هذا المسح: {len(closed_now)}")

    return still_open, closed_now


# ---------------- BTC Dominance (تحذير جودة إشارات العملات البديلة) ----------------


def fetch_btc_dominance():
    r = _request_with_retry("https://api.coingecko.com/api/v3/global")
    return r.json()["data"]["market_cap_percentage"]["btc"]


# ---------------- التشغيل الرئيسي ----------------

def main():
    print(f"بدء المسح — {time.strftime('%Y-%m-%d %H:%M:%S')}")

    tickers = fetch_ticker24h()
    price_map = fetch_prices_map(tickers)

    # قبل أي مسح جديد: تفقّد الصفقات المفتوحة سابقًا مقابل السعر الحالي (TP / SL)
    open_positions = load_positions()
    open_positions, closed_now = check_open_positions(open_positions, price_map)

    results = run_scan(tickers)

    strong = [
        r for r in results
        if r["score"] >= 2.5 and r["vol_confirm"] and r["atr_pct"] >= 0.12 and r["persistent"]
        and not r["ranging"] and not r["near_resistance"]
    ]
    strong_symbols = {r["symbol"] for r in strong}

    # إشارات مبكرة (انضغاط تقلب / تراكم صامت) لعملات لم تصل بعد لإشارة شراء كاملة —
    # تُميَّز بمفتاح منفصل (":early") في ذاكرة التنبيهات كي لا تتعارض مع إشارات الشراء الرسمية
    early_eligible = [
        r for r in results
        if r["score"] < 2.5 and (r["squeeze"] or r["accumulation"])
    ]
    early_keys = {f"{r['symbol']}:early" for r in early_eligible}

    prev_alerted, prev_dominance = load_state()
    fresh = [r for r in strong if r["symbol"] not in prev_alerted]
    fresh_early = [r for r in early_eligible if f"{r['symbol']}:early" not in prev_alerted]

    # تتبّع BTC Dominance: تحذير إضافي لو تحركت بقوة منذ آخر تشغيل (إشارات العملات البديلة تصير أقل موثوقية)
    btc_dominance = None
    market_caution = False
    try:
        btc_dominance = fetch_btc_dominance()
        if prev_dominance is not None:
            shift = btc_dominance - prev_dominance
            market_caution = abs(shift) >= DOM_SHIFT_THRESHOLD
            print(f"BTC Dominance: {btc_dominance:.2f}% (تغيّر {shift:+.2f} نقطة منذ آخر تشغيل)"
                  + (" — تحذير سوق مفعّل" if market_caution else ""))
        else:
            print(f"BTC Dominance: {btc_dominance:.2f}% (أول قراءة، لا مقارنة بعد)")
    except Exception as e:
        print("تعذّر جلب BTC Dominance:", e)

    print(f"إشارات قوية حاليًا: {len(strong)} | جديدة (لم تُرسل قبل): {len(fresh)} | "
          f"إشارات مبكرة جديدة: {len(fresh_early)}")

    for r in fresh:
        caution = market_caution and not r["symbol"].startswith("BTC")
        alert_text = format_alert(r, caution)
        r["_msg_id"] = send_telegram(alert_text)
        r["_alert_text"] = alert_text
        time.sleep(1)  # تجنب تجاوز حد تيليجرام لعدد الرسائل بالثانية

    for r in fresh_early:
        alert_text = format_early_alert(r)
        r["_msg_id"] = send_telegram(alert_text)
        r["_alert_text"] = alert_text
        time.sleep(1)

    # تسجيل الإشارات الجديدة كصفقات مفتوحة قيد المتابعة لاحقًا (رسمية + مبكرة)
    open_new_positions(open_positions, fresh)
    open_new_early_positions(open_positions, fresh_early)

    # حفظ موحّد: ذاكرة الإشارات (رسمية + مبكرة) + BTC Dominance + الصفقات المفتوحة + أرشيف الصفقات المغلقة حديثًا
    save_all_state(strong_symbols | early_keys, btc_dominance, open_positions, closed_now)
    print("انتهى المسح.")


if __name__ == "__main__":
    main()
