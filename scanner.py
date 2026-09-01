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
import datetime as dt
import concurrent.futures
import requests

# ---------- الإعدادات (تُقرأ من متغيرات البيئة / GitHub Secrets) ----------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
INTERVAL = os.environ.get("SCAN_INTERVAL", "1h")          # 15m / 1h / 4h / 1d
DEPTH = int(os.environ.get("SCAN_DEPTH", "60"))            # عدد العملات للفحص العميق
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

# ---------- إعدادات مستويات ارتداد فيبوناتشي (Fibonacci Retracement) ----------
# حقل تشخيصي فقط حاليًا (بنفس منهج market_regime وoverextension عند إضافتهما أول مرة):
# يُحسب ويُحفظ بكل صفقة، بدون التأثير على الدرجة أو قرار الإرسال، لحين تراكم بيانات
# كافية لمعرفة هل التقاطع مع مستويات فيبوناتشي يحسّن دقة الإشارات فعليًا أو لا.
FIB_LOOKBACK = 50               # عدد الشموع للبحث فيها عن أعلى قمة وأدنى قاع (swing) لحساب المستويات
FIB_LEVELS = (0.382, 0.5, 0.618, 0.786)   # النسب الكلاسيكية المستخدمة بالارتداد
FIB_PROXIMITY_PCT = 1.0         # لو السعر أقرب من هذه النسبة% لمستوى فيبوناتشي -> يُعتبر عنده

# ---------- إعدادات مستويات امتداد فيبوناتشي (Fibonacci Extension) ----------
# تُستخدم فقط لإعطاء هدف ثاني حقيقي للإشارة المبكرة في الحالة الوحيدة التي كانت تبقى
# بهدف واحد (شرط واحد فقط متحقق: accumulation أو squeeze لوحده) — بدل هدف مبني فقط على
# مضاعف ATR، نبحث عن أقرب مستوى امتداد فيبوناتشي (1.272/1.618) فوق نفس الحركة السعرية
# الأخيرة (swing)، وإن وُجد ومنطقي (فوق TP1 ولا يتجاوز أقرب مقاومة) نضيفه كـTP2.
FIB_EXTENSION_LEVELS = (1.272, 1.618)

# ---------- إعدادات فلتر الإرهاق/الامتداد الزائد (Overextension) ----------
EXTENSION_EMA_PERIOD = 50       # المتوسط المتحرك المرجعي لقياس "المسافة المقطوعة" عن خط الأساس
EXTENSION_ATR_THRESHOLD = float(os.environ.get("EXTENSION_ATR_THRESHOLD", "3.0"))
# المسافة بين السعر وEMA50 بوحدات ATR — فوق هذا الحد يُعتبر السعر ممتدًا بشكل مفرط (احتمال شراء متأخر)

# استبعاد صريح للإشارات الرسمية ذات الدرجة العالية جدًا (score >= هذا الحد):
# official_success_factors.py أكّد على عينة 246 صفقة أن هذه الفئة الأضعف بثبات
# (25.6% من الصفقات الفاشلة مقابل 10.9% فقط من الناجحة) — قبل كان يُعاقَب بالدرجة
# فقط (extension penalty)، الآن يُرفض الإرسال نهائيًا لو تجاوزها.
MAX_OFFICIAL_SCORE = float(os.environ.get("MAX_OFFICIAL_SCORE", "3.5"))

# رقم نسخة منطق فلترة الإشارة الرسمية — يُحفظ مع كل صفقة رسمية جديدة كي يمكن لاحقًا
# فصل أداء "قبل" و"بعد" أي تعديل على شروط strong بدقة، بدل الاعتماد على تاريخ الفتح يدويًا.
# 1 = المنطق القديم (ranging/near_resistance كشروط رفض، بدون بوابتي htf_aligned/market_regime)
# 2 = المنطق الحالي: htf_aligned=True وmarket_regime="trending_up" بوابتان إلزاميتان،
#     وحُذف شرط "not ranging" (أصبح تكرارًا لـmarket_regime على فريم أعلى)
OFFICIAL_LOGIC_VERSION = 2

# ---------- إعدادات الإشارات المبكرة (انضغاط تقلب / تراكم صامت) ----------
SQUEEZE_LOOKBACK = 20           # عدد الشموع لحساب متوسط عرض نطاق Bollinger
SQUEEZE_RATIO_THRESHOLD = 0.6   # عرض النطاق الحالي <= هذه النسبة من المتوسط -> يُعتبر انضغاطًا
ACCUM_WINDOW = 20               # عدد الشموع لقياس التراكم الصامت
ACCUM_PRICE_MAX_MOVE_PCT = 4.0  # أقصى تحرك سعري% خلال النافذة كي يُعتبر السعر "شبه ثابت"
ACCUM_FLOW_RATIO_MIN = 0.3      # أدنى نسبة صافي تدفق شراء (OBV/حجم) كي يُعتبر تراكمًا واضحًا

# ---------- إعدادات إشارة الانفجار (Breakout) ----------
BREAKOUT_LOOKBACK = 10          # عدد الشموع للبحث فيها عن أعلى قمة سابقة قبل الاختراق
BREAKOUT_VOL_MULT = 1.3         # الحجم الحالي يجب أن يتجاوز متوسط الحجم بهذا المضاعف
BREAKOUT_MIN_ATR_PCT = 0.08     # أدنى نسبة تقلب (ATR%) لقبول إشارة الانفجار

# ---------------- إعدادات الحد الأدنى لنسبة الربح المستهدفة ----------------
# لا تُرسل أي إشارة (رسمية أو مبكرة) إلا لو كانت نسبة الربح المتوقعة عند أول هدف (TP1)
# مقارنة بسعر الدخول >= هذه النسبة% — لتفادي إشارات ذات هدف قريب جدًا لا يستحق الدخول
MIN_PROFIT_PCT = float(os.environ.get("MIN_PROFIT_PCT", "1.0"))

# ---------------- إعدادات أهداف الإشارات المبكرة (تقديرية، أقل ثقة من الإشارة الرسمية) ----------------
# وقف خسارة أوسع من الإشارة الرسمية (1.5×ATR) لأن نقطة الدخول أقل دقة والتقلب حولها أعلى
EARLY_SL_ATR_MULT = 2.0
# عدد الأهداف يعتمد على عدد الشروط المتحققة (squeeze / accumulation / divergence / momentum: 1-4 أهداف).
# الثقة (بعد تعديل مبني على بيانات فعلية بتاريخ 2026-08-18): accumulation حاضر -> مؤكدة
# مباشرة (حتى لو وحيدًا)؛ squeeze حاضر بدون accumulation -> يحتاج شرطًا إضافيًا وإلا
# لا تُطلق إشارة أصلاً؛ 3 شروط فأكثر (بأي مزيج) -> مؤكدة قوية.


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


# عتبة ADX المستخدمة لتصنيف نظام السوق على الفريم الأعلى (منفصلة عن ADX_THRESHOLD
# المستخدم لتصنيف "ranging" على فريم التحليل الأساسي، لإتاحة ضبط كل مستوى لوحده لاحقًا)
MARKET_REGIME_ADX_THRESHOLD = float(os.environ.get("MARKET_REGIME_ADX_THRESHOLD", "25"))


def classify_market_regime(htf_highs, htf_lows, htf_closes, htf_up):
    """
    يصنف حالة السوق على الفريم الأعلى: trending_up / trending_down / ranging.
    يعتمد على ADX (قوة الاتجاه) محسوبًا على نفس شموع الفريم الأعلى المستخدمة أصلاً
    لحساب htf_aligned (بدون أي طلب بيانات إضافي)، + اتجاه EMA7/14 (htf_up) لتحديد الجهة.
    يرجع None لو العينة غير كافية لحساب ADX بثقة.
    """
    adx_series = adx(htf_highs, htf_lows, htf_closes)
    htf_adx_val = next((v for v in reversed(adx_series) if v is not None), None)
    if htf_adx_val is None:
        return None
    if htf_adx_val >= MARKET_REGIME_ADX_THRESHOLD:
        return "trending_up" if htf_up else "trending_down"
    return "ranging"


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


def fibonacci_levels(highs, lows, lookback=FIB_LOOKBACK):
    """
    يحسب مستويات ارتداد فيبوناتشي بين أعلى قمة وأدنى قاع خلال آخر lookback شمعة.
    يرجع قاموسًا {نسبة: سعر المستوى} (0 = القمة، 1 = القاع). لا يحدد الدالة نفسها
    هل المستوى "دعم" أو "مقاومة" -- هذا يُقرَّر عند القراءة حسب اتجاه الترند الحالي
    (نفس فكرة nearest_resistance اللي تُقرأ بسياقات مختلفة).
    """
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


def fibonacci_extension_levels(highs, lows, lookback=FIB_LOOKBACK):
    """
    يحسب مستويات امتداد فيبوناتشي (1.272 / 1.618) فوق نفس حركة swing المستخدمة
    بـfibonacci_levels — تُستخدم لإيجاد هدف ثاني حقيقي (مبني على نسبة سعرية معروفة
    بالسوق) للإشارة المبكرة في حالة الشرط الواحد، بدل الاكتفاء بمضاعف ATR فقط.
    يرجع قاموسًا {نسبة: سعر المستوى}، أو {} لو العينة غير كافية.
    """
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
    return {lvl: swing_low + diff * lvl for lvl in FIB_EXTENSION_LEVELS}


def nearest_fib_level(price, levels, proximity_pct=FIB_PROXIMITY_PCT):
    """
    يرجع (النسبة, سعر المستوى) لأقرب مستوى فيبوناتشي للسعر الحالي ضمن هامش
    proximity_pct%، أو (None, None) لو ما في مستوى قريب بما يكفي.
    """
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


def breakout_detect(highs, closes, vols, lookback=BREAKOUT_LOOKBACK, vol_mult=BREAKOUT_VOL_MULT):
    """
    إشارة انفجار: اختراق أعلى قمة خلال آخر lookback شمعة (بدون احتساب الشمعة الحالية)
    مصحوبًا بحجم يتجاوز متوسط الحجم السابق بمضاعف vol_mult.
    """
    n = len(closes)
    if n < lookback + 5:
        return False
    recent_high = max(highs[-lookback-1:-1])
    prev_vol_avg = sum(vols[-lookback-1:-1]) / lookback
    if prev_vol_avg == 0:
        return False
    return closes[-1] > recent_high and vols[-1] > prev_vol_avg * vol_mult


def breakout_quality(ind, i):
    """
    تقييم جودة إشارة الانفجار (0 إلى 3): دعم الاتجاه (EMA7>EMA14)، تأكيد MACD،
    وRSI في منطقة صحية (لا تشبع بيعي ولا شرائي).
    """
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

    # فيبوناتشي: تقاطع السعر مع مستوى ارتداد أثناء اتجاه صاعد (0.5 فأعمق) يُعتبر
    # منطقة دعم كلاسيكية قبل استئناف الصعود -- تشخيصي فقط حاليًا، لا يؤثر على الدرجة
    fib_map = fibonacci_levels(ind["highs"][:i + 1], ind["lows"][:i + 1])
    fib_level, fib_level_price = nearest_fib_level(price, fib_map)
    fib_support = fib_level is not None and fib_level >= 0.5 and trend_up

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
        "fib_level": fib_level,
        "fib_support": fib_support,
        # حقول أساسية إضافية للحفظ التشخيصي (rsi_state: 1 تشبع بيعي / -1 تشبع شرائي / 0 محايد،
        # bb_state: 1 عند الحد السفلي / -1 عند الحد العلوي / 0 منتصف النطاق)
        "rsi_state": rsi_state,
        "macd_bull": macd_bull,
        "bb_state": bb_state,
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

def meets_min_profit(entry, tps, min_pct=MIN_PROFIT_PCT):
    """
    يتحقق أن أقرب هدف (TP1) يحقق نسبة ربح >= الحد الأدنى المطلوب (MIN_PROFIT_PCT)
    مقارنة بسعر الدخول. يُستخدم لتصفية أي إشارة (رسمية أو مبكرة) قبل اعتبارها
    مؤهلة للإرسال، بصرف النظر عن مصدرها (accumulation / squeeze / كليهما).
    """
    if not entry or not tps:
        return False
    tp1_profit_pct = (tps[0] - entry) / entry * 100
    return tp1_profit_pct >= min_pct


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
        persistent = bool(prev_r) and (prev_r["score"] > 0) == (r["score"] > 0) and abs(prev_r["score"]) >= 0.5

        final_score = r["score"]
        htf_checked, htf_aligned = False, None
        market_regime = None
        if abs(r["score"]) >= 0.5:
            htf = HTF_MAP.get(interval)
            if htf:
                try:
                    htf_klines = fetch_klines(symbol, htf, 60)
                    htf_klines = drop_unclosed_candle(htf_klines)
                    htf_highs = [float(k[2]) for k in htf_klines]
                    htf_lows = [float(k[3]) for k in htf_klines]
                    htf_closes = [float(k[4]) for k in htf_klines]
                    htf_up = ema(htf_closes, 7)[-1] > ema(htf_closes, 14)[-1]
                    trend_dir = 1 if r["trend_up"] else -1
                    htf_aligned = htf_up == r["trend_up"]
                    final_score += (trend_dir * 0.5) if htf_aligned else (-trend_dir * 0.5)
                    htf_checked = True
                    # تصنيف نظام السوق (trending_up/trending_down/ranging) على نفس شموع
                    # الفريم الأعلى أعلاه — بدون أي طلب بيانات إضافي عن الشبكة
                    market_regime = classify_market_regime(htf_highs, htf_lows, htf_closes, htf_up)
                except Exception:
                    pass

        # إشارات مبكرة (انضغاط تقلب / تراكم صامت) — مستقلة عن الدرجة الرسمية، تُحسب دائمًا
        # للعرض، وتُستخدم لاحقًا فقط لعملات لم تصل بعد لإشارة شراء كاملة
        squeeze = volatility_squeeze(ind["bb_upper"], ind["bb_lower"], ind["closes"])
        accumulation = silent_accumulation(ind["closes"], ind["vols"], ind["obv"])

        # أهداف تقديرية للإشارة المبكرة نفسها (وليس فقط تحذير بدون أرقام):
        # وقف خسارة أوسع (ATR×2) لأن الدخول أقل تأكيدًا، وعدد أهداف حسب مستوى الثقة،
        # مع تقليم أي هدف يتجاوز أقرب مقاومة معروفة كي لا نضع هدفًا خلف حاجز سعري واضح.
        #
        # وزن الثقة مبني على بيانات فعلية (score_breakdown_by_factor.py على 212 صفقة
        # مبكرة مغلقة): accumulation لوحده أثبت نجاحًا عاليًا وثابتًا (83-100%) بغض
        # النظر عن أي شرط إضافي، بينما squeeze لوحده (بدون accumulation) كان أضعف من
        # عدم وجوده أصلاً عند نفس مستوى الدرجة. لذلك:
        #   - accumulation حاضر -> "مؤكدة" مباشرة حتى لو كان الشرط الوحيد (أو "مؤكدة
        #     قوية" لو توفرت 3 شروط فأكثر)
        #   - squeeze حاضر بدون accumulation -> يحتاج شرطًا إضافيًا (divergence أو
        #     momentum) ليُطلق إشارة أصلاً؛ squeeze وحيدًا لا يكفي بعد الآن (لا إشارة)
        #
        # حقل early_factors (مضاف): قائمة كل العوامل الأربعة الفعلية الحاضرة فعليًا
        # (squeeze/accumulation/divergence/momentum) — منفصل عن early_source (مصدر
        # الإطلاق التاريخي المستخدم بتحليلات الأداء السابقة) لتفادي كسر استمرارية
        # تحليل score_breakdown_by_factor.py وclassify_early_score، مع حل مشكلة أن
        # "المصدر" المعروض كان يذكر فقط accumulation/squeeze حتى لو كان عدد الأهداف
        # (2 أو 3) ناتجًا فعليًا عن مساهمة divergence/momentum أيضًا.
        early_entry = early_sl = None
        early_tps = []
        early_confidence = None
        early_source = None
        early_factors = []
        fib_extension_used = False
        momentum = momentum_strength(ind["macd"], ind["signal"], ind["rsi"], last)
        if squeeze or accumulation:
            conditions_met = sum([squeeze, accumulation, r["divergence"], momentum])
            valid_early = accumulation or (squeeze and conditions_met >= 2)
            if valid_early:
                early_confidence = "مؤكدة قوية" if conditions_met >= 3 else "مؤكدة"
                if accumulation and squeeze:
                    early_source = "accumulation+squeeze"
                elif accumulation:
                    early_source = "accumulation"
                else:
                    early_source = "squeeze"
                if accumulation:
                    early_factors.append("accumulation")
                if squeeze:
                    early_factors.append("squeeze")
                if r["divergence"]:
                    early_factors.append("divergence")
                if momentum:
                    early_factors.append("momentum")

                early_entry = ind["closes"][last]
                atrv = atr_value(ind)
                atr_risk = atrv * EARLY_SL_ATR_MULT
                min_risk_for_target = early_entry * (MIN_PROFIT_PCT / 100)
                early_risk = max(atr_risk, min_risk_for_target)
                early_sl = early_entry - early_risk
                early_tp_count = conditions_met  # عدد الأهداف = عدد الشروط المتحققة فعليًا لهاي العملة (1 إلى 4)
                raw_tps = [early_entry + early_risk * i for i in range(1, early_tp_count + 1)]
                resistance = r.get("resistance")

                # فيبوناتشي كهدف ثاني حقيقي: فقط لو الإشارة أصلاً بشرط واحد متحقق (هدف
                # واحد بس بالمنطق القديم) ولقينا مستوى امتداد منطقي فوق TP1 ولا يتجاوز
                # أقرب مقاومة معروفة (لو موجودة) — وإلا تبقى الإشارة بهدف واحد كالسابق.
                if early_tp_count == 1:
                    ext_levels = fibonacci_extension_levels(ind["highs"][:last + 1], ind["lows"][:last + 1])
                    ext_candidates = sorted(p for p in ext_levels.values() if p > raw_tps[0])
                    if resistance:
                        ext_candidates = [p for p in ext_candidates if p < resistance]
                    if ext_candidates:
                        raw_tps.append(ext_candidates[0])
                        fib_extension_used = True
                        early_factors.append("fibonacci")

                if resistance:
                    # نوقف توليد الأهداف عند أول هدف يتجاوز أقرب مقاومة بدل تقليم كل هدف
                    # لنفس سقف المقاومة — التقليم القديم كان يجعل TP1 وTP2 يتساويان بالضبط
                    # كلما تجاوز أكثر من هدف نفس المقاومة معًا.
                    trimmed = []
                    for tp in raw_tps:
                        if tp >= resistance:
                            trimmed.append(resistance)
                            break
                        trimmed.append(tp)
                    early_tps = trimmed
                else:
                    early_tps = raw_tps

        # إشارة انفجار (Breakout) — اختراق قمة سابقة مع تأكيد حجم، مستقلة عن الدرجة الرسمية
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

        # خطة دخول (شراء فقط — السوق الفوري لا يدعم فتح صفقة بيع مكشوفة)، محسوبة ديناميكيًا حسب التحليل:
        # وقف الخسارة من التقلب الفعلي (ATR) للعملة، وعدد الأهداف حسب قوة درجة التوافق
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
            "htf_checked": htf_checked,
            "htf_aligned": htf_aligned,
            "market_regime": market_regime,
            "ranging": r["ranging"],
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
            "early_factors": early_factors, "fib_extension_used": fib_extension_used,
            "breakout_entry": breakout_entry, "breakout_sl": breakout_sl, "breakout_tps": breakout_tps,
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
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
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


def format_tp_line(pos, tp_index):
    """سطر مختصر لهدف واحد متحقق (يُستخدم بالتعديل التراكمي على رسالة الإشارة الأصلية فقط) —
    يقتطع فقط سطر التحقق من هذا الهدف، ولا يعيد كتابة الرسالة كاملة."""
    entry = pos["entry"]
    tp = pos["tps"][tp_index]
    pct_gain = (tp - entry) / entry * 100
    tp_label = f"TP{tp_index + 1}"
    return f"✅ تحقق {tp_label}: {tp:.6g} (+{pct_gain:.2f}%)"


def build_progress_text(pos):
    """
    النص الأساسي الذي يُبنى عليه أي تعديل نهائي للرسالة (SL / انعكاس / انتهاء وقت):
    النص الأصلي + سطر لكل هدف تحقق قبل الإغلاق (إن وُجد)، حتى ما يضيع سجل الأهداف
    المتحققة سابقًا عند الشطب النهائي.
    """
    base = pos.get("alert_text", "")
    hit_sorted = sorted(pos.get("hit_tps", []))
    if not hit_sorted:
        return base
    lines = [format_tp_line(pos, j) for j in hit_sorted]
    return base + "\n\n" + "\n".join(lines)


def classify_official_score(score):
    """
    يحوّل درجة الإشارة الرسمية إلى تصنيف نصي (ضعيفة/متوسطة/قوية) بناءً على نجاح فعلي
    محسوب من 354 صفقة مغلقة (score_ranges.py، أوت 2026):
      1.5           -> قوية   (70.7% نجاح، 167 صفقة)
      2.0 - 2.5     -> متوسطة (49.0% نجاح، 49 صفقة)
      3.0 فأعلى     -> ضعيفة  (40.4% نجاح، 57 صفقة)
    ملاحظة: هذه نطاقات ثابتة مبنية على تحليل تاريخي، لا تُحدَّث تلقائيًا — يُنصح بإعادة
    تشغيل score_ranges.py دوريًا (كل شهر تقريبًا) ومراجعة هذه الحدود يدويًا إذا تغيّرت.
    """
    if score <= 1.5:
        return "قوية"
    elif score <= 2.5:
        return "متوسطة"
    else:
        return "ضعيفة"


def classify_early_score(source, conditions_met=None):
    """
    يحوّل قوة الإشارة المبكرة إلى تصنيف نصي (ضعيفة/متوسطة/قوية).
    مبني على المصدر (لا على رقم الدرجة، لأن score_breakdown_by_factor.py أثبت أن الدرجة
    السالبة/الموجبة لا تعني شيئًا ثابتًا بمعزل عن المصدر)، مُوحَّد الآن مع early_confidence
    كي لا يظهر تناقض ("مؤكدة قوية" مع تصنيف "متوسطة" بنفس الرسالة كما كان سابقًا):
    أي إشارة عندها 3 شروط فأكثر (نفس عتبة "مؤكدة قوية") تُصنَّف "قوية" بصرف النظر عن
    المصدر، لأن تزامن عدة عوامل معًا مؤشر جودة إضافي بحد ذاته.
    نسب النجاح الفعلية (trade_stats.py، 300 صفقة، أوت 2026):
      تراكم صامت (accumulation)          -> قوية   (90% نجاح، 87 صفقة)
      تراكم صامت + انضغاط تقلب (مزيج)     -> قوية   (المصدر الأقوى يحدد التصنيف)
      انضغاط تقلب فقط (squeeze)          -> متوسطة (73% نجاح، 146 صفقة)
    """
    if source in ("accumulation", "accumulation+squeeze"):
        return "قوية"
    if conditions_met is not None and conditions_met >= 3:
        return "قوية"
    if source == "squeeze":
        return "متوسطة"
    return "متوسطة"


def format_alert(r, market_caution=False):
    is_buy = r["score"] >= 1.5
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
    if r.get("fib_support"):
        badges.append(f"دعم فيبوناتشي {r['fib_level']}")
    badge_txt = f" ({', '.join(badges)})" if badges else ""

    lines = [
        f"{dot} {title}",
        r['symbol'].replace('USDT', '/USDT'),
        f"القوة: {classify_official_score(r['score'])} ({r['score']:.1f}) | فريم: {INTERVAL}{badge_txt}",
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


# تسميات كل عامل من عوامل الإشارة المبكرة (تُستخدم لبناء سطر "المصدر" كاملاً بترتيب ثابت،
# بدل الاكتفاء بذكر accumulation/squeeze فقط كما كان سابقًا)
EARLY_FACTOR_LABELS = {
    "accumulation": "تراكم صامت",
    "squeeze": "انضغاط تقلب",
    "divergence": "انحراف صعودي",
    "momentum": "زخم",
    "fibonacci": "فيبوناتشي",
}


def format_early_alert(r):
    """
    تنبيه رادار مبكر: انضغاط تقلب و/أو تراكم صامت لعملة لم تصل بعد لإشارة شراء كاملة.
    يعرض أهدافًا تقديرية (وقف خسارة أوسع من الرسمية + عدد أهداف متغير حسب مستوى الثقة)،
    وتُتابَع تلقائيًا (TP/SL) ضمن نفس آلية الصفقات المفتوحة — لكنها تبقى أقل تأكيدًا
    من الإشارة الرسمية. قالب مختصر: بدون سطر المؤشرات وبدون السعر الحالي المنفصل،
    مع الإبقاء فقط على تحذير الحركة الممتدة عند انطباقه.

    سطر "المصدر" يعرض الآن كل العوامل الفعلية المساهمة (accumulation/squeeze/divergence/
    momentum/fibonacci) وليس فقط accumulation/squeeze كما كان سابقًا — لتفادي التناقض
    بين عدد الأهداف المعروضة ومصدر واحد أو اثنين فقط مذكورين بالرسالة.
    """
    confidence = r.get("early_confidence")
    dot = "🟢" if confidence == "مؤكدة قوية" else ("🟣" if confidence == "مؤكدة" else "🔵")
    title = f"إشارة مبكرة - {confidence}" if confidence else "إشارة مبكرة"

    factors = r.get("early_factors") or []
    source_label = " + ".join(EARLY_FACTOR_LABELS[f] for f in factors if f in EARLY_FACTOR_LABELS)

    conditions_met = len([f for f in factors if f != "fibonacci"])
    strength_label = classify_early_score(r.get("early_source"), conditions_met)

    lines = [
        f"{dot} {title}",
        r['symbol'].replace('USDT', '/USDT'),
        f"القوة: {strength_label} ({r['score']:.1f}) | فريم: {INTERVAL}",
    ]
    if source_label:
        lines.append(f"المصدر: {source_label}")

    if r.get("early_entry") is not None:
        lines.append(f"الدخول : {r['early_entry']:.6g}")
        for i, tp in enumerate(r.get("early_tps", []), start=1):
            lines.append(f"TP {i}: {tp:.6g}")
        lines.append(f"SL : {r['early_sl']:.6g}")

    if r.get("extended"):
        lines.append("⚠️ حركة ممتدة")
    if r.get("fib_support"):
        lines.append(f"دعم فيبوناتشي {r['fib_level']}")

    return "\n".join(lines)


def format_breakout_alert(r):
    """إشارة انفجار زخم: اختراق قمة سابقة مع تأكيد حجم — تدخل مبكرًا مع بداية الزخم."""
    b_score = r.get("breakout_score", 0)
    dot = "🟠" if b_score >= 3 else ("🟡" if b_score >= 2 else "⚪")
    title = "إشارة انفجار زخم"
    details = r.get("breakout_details", {})
    badges = []
    if details.get("trend_support"):
        badges.append("EMA7/14 داعم")
    if details.get("macd_bull"):
        badges.append("MACD إيجابي")
    if details.get("rsi_ok"):
        badges.append("RSI مناسب")
    badge_txt = f" ({', '.join(badges)})" if badges else ""
    lines = [
        f"{dot} {title}{badge_txt}",
        r['symbol'].replace('USDT', '/USDT'),
        f"جودة الاختراق: {b_score}/3 | فريم: {INTERVAL}",
        f"السعر: {r['price']:.6g}",
    ]
    if r.get("breakout_entry") is not None:
        lines.append(f"الدخول: {r['breakout_entry']:.6g}")
        for i, tp in enumerate(r.get("breakout_tps", []), start=1):
            lines.append(f"TP{i}: {tp:.6g}")
        lines.append(f"SL: {r['breakout_sl']:.6g}")
    if r.get("extended"):
        lines.append("⚠️ حركة ممتدة")
    if r.get("fib_support"):
        lines.append(f"دعم فيبوناتشي {r['fib_level']}")
    lines.append("💡 هذه الإشارة تدخل مع بداية الزخم — أسرع من الرسمية لكن أقل تأكيداً")
    return "\n".join(lines)


# ---------------- إدارة الحالة عبر GitHub Gist (بديل عن الكتابة داخل المستودع) ----------------

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")
GIST_FILENAME = "alerted_state.json"
POSITIONS_GIST_FILE = "open_positions.json"   # الصفقات المفتوحة قيد المتابعة (نفس الـ Gist، ملف منفصل)
CLOSED_GIST_FILE = "closed_trades.json"       # السجل "النشط": أحدث الصفقات فقط (قراءة سريعة، دائمًا صغير وآمن)
STATS_GIST_FILE = "stats.json"                # إحصائيات أداء محسوبة دوريًا من closed_trades (خيار 3: تتبع فقط)
ACTIVE_HISTORY_SIZE = 400                     # عدد الصفقات المحفوظة في السجل النشط قبل ترحيل الأقدم للأرشيف
ARCHIVE_PREFIX = "closed_trades_archive_"     # بادئة ملفات الأرشيف المرقّمة (كل ملف محدود الحجم، بلا سقف على عددها)
ARCHIVE_CHUNK_SIZE = 400                      # حد أقصى للصفقات في كل ملف أرشيف (يبقيه دائمًا تحت حد GitHub ~1MB بأمان)
DOM_SHIFT_THRESHOLD = float(os.environ.get("DOM_SHIFT_THRESHOLD", "0.3"))  # نقطة مئوية خلال دورة تشغيل واحدة


def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def _gist_get_all_files():
    """يقرأ قاموس كل ملفات الـ Gist دفعة واحدة (اسم -> بيانات الملف بما فيها content)."""
    if not GIST_TOKEN or not GIST_ID:
        return {}
    try:
        r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=_gist_headers(), timeout=15)
        r.raise_for_status()
        return r.json().get("files", {})
    except Exception as e:
        print(f"تعذّر قراءة ملفات Gist ({e})")
        return {}


def _gist_get_file(filename):
    """يقرأ محتوى ملف واحد داخل الـ Gist (يرجع None لو غير موجود أو حصل خطأ)."""
    files = _gist_get_all_files()
    if filename not in files:
        return None
    return files[filename]["content"]


def archive_overflow(overflow_trades, gist_files):
    """
    يوزّع الصفقات القديمة الفائضة (التي خرجت من السجل النشط) على ملفات أرشيف مرقّمة
    (closed_trades_archive_0001.json, 0002.json, ...)، كل ملف محدود بـARCHIVE_CHUNK_SIZE
    صفقة كحد أقصى — هذا يضمن نموًا غير محدود إجمالاً (يمكن الوصول لملايين الصفقات عبر
    آلاف الملفات الصغيرة) بلا أن يصطدم أي ملف منفرد بحد GitHub لحجم المحتوى (~1MB) الذي
    يسبب بتر البيانات بصمت.
    """
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

    # أكمل آخر ملف أرشيف موجود إن كان فيه مكان فارغ
    space = ARCHIVE_CHUNK_SIZE - len(last_content)
    if space > 0 and remaining:
        last_content.extend(remaining[:space])
        remaining = remaining[space:]
        files_to_write[f"{ARCHIVE_PREFIX}{idx:04d}.json"] = json.dumps(last_content, ensure_ascii=False, indent=2)

    # أنشئ ملفات أرشيف جديدة للباقي (بلا أي سقف على عدد الملفات)
    while remaining:
        idx += 1
        chunk = remaining[:ARCHIVE_CHUNK_SIZE]
        remaining = remaining[ARCHIVE_CHUNK_SIZE:]
        files_to_write[f"{ARCHIVE_PREFIX}{idx:04d}.json"] = json.dumps(chunk, ensure_ascii=False, indent=2)

    return files_to_write


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


def compute_stats(history):
    """
    يحسب إحصائيات أداء بحتة من سجل الصفقات المغلقة (خيار 3: تتبع فقط، بدون أي
    تعديل تلقائي على منطق الفحص/الدخول/الأوزان). لا يُستخدم الناتج هنا لتغيير
    أي قرار في البوت — فقط للعرض والمراقبة اليدوية.
    """
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


def save_all_state(alerted_symbols, btc_dominance, positions, closed_delta):
    """
    يحفظ في نفس الطلب: حالة التنبيهات + BTC Dominance + الصفقات المفتوحة،
    ويُلحق أي صفقات أُغلقت هذا التشغيل بسجل closed_trades (مع سقف للحجم).
    كما يحسب إحصائيات أداء (stats.json) من السجل المحدَّث — تتبع فقط، بدون
    أي تأثير على منطق الفحص أو الدخول. يرجع الإحصائيات (أو None) للاستخدام
    الاختياري في إرسال تقرير دوري.
    """
    files = {
        GIST_FILENAME: json.dumps(
            {"alerted": sorted(alerted_symbols), "btc_dominance_prev": btc_dominance},
            ensure_ascii=False
        ),
        POSITIONS_GIST_FILE: json.dumps(positions, ensure_ascii=False, indent=2),
    }

    stats = None
    if closed_delta:
        # نجلب كل ملفات الـ Gist دفعة واحدة (نحتاجها للسجل النشط ولملفات الأرشيف معًا)
        gist_files = _gist_get_all_files()
        try:
            history = json.loads(gist_files.get(CLOSED_GIST_FILE, {}).get("content") or "[]")
        except Exception:
            history = []
        history.extend(closed_delta)

        # إذا تجاوز السجل النشط الحد، تُرحَّل أقدم الصفقات لملفات الأرشيف بدل حذفها نهائيًا —
        # لا يُفقد أي شيء، والسجل النشط يبقى دائمًا صغيرًا وسريع القراءة
        if len(history) > ACTIVE_HISTORY_SIZE:
            overflow = history[:-ACTIVE_HISTORY_SIZE]
            history = history[-ACTIVE_HISTORY_SIZE:]
            files.update(archive_overflow(overflow, gist_files))

        files[CLOSED_GIST_FILE] = json.dumps(history, ensure_ascii=False, indent=2)

        # ملاحظة: الإحصائيات الآنية تُحسب من السجل النشط فقط (آخر ACTIVE_HISTORY_SIZE صفقة)
        # للتقرير الفوري — التحليل الشامل الكامل يحتاج قراءة السجل النشط + كل ملفات الأرشيف
        stats = compute_stats(history)
        if stats:
            files[STATS_GIST_FILE] = json.dumps(stats, ensure_ascii=False, indent=2)

    _gist_patch_files(files)
    return stats


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
            # مؤشرات الدرجة الأساسية (توسيع تشخيصي) -> لمعرفة مزيج المؤشرات الأساسية وراء
            # كل إشارة، حتى الصفقات التي لا يوجد فيها أي عامل إضافي أعلاه
            "rsi_state": r.get("rsi_state"),
            "macd_bull": r.get("macd_bull"),
            "bb_state": r.get("bb_state"),
            "vol_confirm": r.get("vol_confirm"),
            "ranging": r.get("ranging"),
            "near_resistance": r.get("near_resistance"),
            "obv_confirm": r.get("obv_confirm"),
            "htf_aligned": r.get("htf_aligned"),
            "market_regime": r.get("market_regime"),
            "fib_level": r.get("fib_level"),
            "fib_support": r.get("fib_support"),
            "logic_version": OFFICIAL_LOGIC_VERSION,
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
            # مصدر الإشارة الأساسي (accumulation / squeeze / accumulation+squeeze) — يبقى
            # كما هو لاستمرارية تحليلات الأداء السابقة (classify_early_score/score_breakdown)
            "source": r.get("early_source"),
            # كل العوامل الفعلية المساهمة (squeeze/accumulation/divergence/momentum/fibonacci) —
            # حقل جديد منفصل، يسمح بمعرفة هل فيبوناتشي أو divergence/momentum ساهموا فعليًا
            # بدون المساس باستمرارية حقل source أعلاه
            "factors": r.get("early_factors"),
            "fib_extension_used": r.get("fib_extension_used"),
            "squeeze": r.get("squeeze"),
            "accumulation": r.get("accumulation"),
            "divergence": r.get("divergence"),
            "extended": r.get("extended"),
            "rsi_state": r.get("rsi_state"),
            "macd_bull": r.get("macd_bull"),
            "bb_state": r.get("bb_state"),
            "vol_confirm": r.get("vol_confirm"),
            "ranging": r.get("ranging"),
            "near_resistance": r.get("near_resistance"),
            "obv_confirm": r.get("obv_confirm"),
            "htf_aligned": r.get("htf_aligned"),
            "market_regime": r.get("market_regime"),
            "fib_level": r.get("fib_level"),
            "fib_support": r.get("fib_support"),
            "alert_message_id": r.get("_msg_id"),
            "alert_text": r.get("_alert_text"),
        })


def open_new_breakout_positions(positions, fresh_breakout_signals):
    """
    يفتح متابعة تلقائية (TP/SL) لإشارات الانفجار (breakout) بنفس آلية الصفقات
    الرسمية/المبكرة، بحقل type="breakout" يُستخدم لاحقًا لتمييز رسائل النتيجة.
    """
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
            "extended": r.get("extended"),
            "rsi_state": r.get("rsi_state"),
            "macd_bull": r.get("macd_bull"),
            "bb_state": r.get("bb_state"),
            "vol_confirm": r.get("vol_confirm"),
            "ranging": r.get("ranging"),
            "near_resistance": r.get("near_resistance"),
            "obv_confirm": r.get("obv_confirm"),
            "htf_aligned": r.get("htf_aligned"),
            "market_regime": r.get("market_regime"),
            "fib_level": r.get("fib_level"),
            "fib_support": r.get("fib_support"),
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
            edit_telegram_strike(pos.get("alert_message_id"), build_progress_text(pos), result_text)
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
            # كل الأهداف تحققت -> إغلاق نهائي: نشطب رسالة الإشارة الأصلية (بما فيها كل أسطر
            # الأهداف المتراكمة) تمامًا كما يحصل عند SL/EXPIRED، بدل تركها بدون شطب نهائي
            all_tp_text = "🏁 تحققت جميع الأهداف"
            edit_telegram_strike(pos.get("alert_message_id"), build_progress_text(pos), all_tp_text)
            pos["closed_reason"] = "ALL_TP"
            pos["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            pos["exit_price"] = price
            closed_now.append(pos)
            continue

        # لم يتحقق TP ولا SL بعد -> الصفقة تبقى مفتوحة لغاية تحقق أحد الأهداف أو ضرب
        # وقف الخسارة (لا يوجد إغلاق مبكر بسبب انعكاس الاتجاه بعد الآن)، إلا لو تجاوزت
        # السقف الزمني الأقصى (شبكة أمان فقط).
        hours_open = _hours_since(pos["opened_at"])
        if hours_open >= TIME_STOP_HOURS:
            pct_change = (price - pos["entry"]) / pos["entry"] * 100
            status = "بربح" if pct_change > 0 else ("بخسارة" if pct_change < 0 else "بدون تغيير")
            expired_text = (
                f"⏱️ انتهت صلاحية المراقبة (سقف زمني) — متوقفة {status}\n{pos['symbol'].replace('USDT','/USDT')}\n"
                f"الدخول: {pos['entry']:.6g} | الحالي: {price:.6g} | مدة المراقبة: {hours_open:.0f}س\n"
                f"النسبة: {pct_change:+.2f}%"
            )
            send_telegram(expired_text)
            edit_telegram_strike(pos.get("alert_message_id"), build_progress_text(pos), expired_text)
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

    # شرطا htf_aligned وmarket_regime="trending_up" أصبحا بوابتين إلزاميتين (مو مجرد
    # تعديل بالدرجة كما كانا سابقًا). السبب: تحليل official_success_factors.py +
    # نموذج ML التجريبي (train_model.py) أكّدا أن هذين العاملين هما الأقوى تفسيرًا
    # لنجاح/فشل الإشارة الرسمية (market_regime كان أهم ميزة بالنموذج بـfeature
    # importance=0.376، وhtf_aligned=True كان حاضرًا بـ100% من الصفقات الناجحة
    # مقابل 84% فقط من الفاشلة). الفكرة: بدل الاعتماد على تزامن المؤشرات اللحظية
    # فقط (الذي يميل لإعطاء أعلى درجة عند القمة تحديدًا = "شراء القمة")، نستعير
    # مبدأ الإشارة المبكرة الناجح (انتظار سياق أوسع مؤكد) كشرط دخول إلزامي للرسمية.
    # لو تعذّر تحديد market_regime (بيانات فريم أعلى غير متاحة) نُسقط الإشارة
    # احترازيًا بدل قبولها بدون تأكيد سياق.
    # شرط "not ranging" (ADX على فريم التحليل الأساسي) أُزيل من هنا لأنه أصبح تكرارًا
    # لفكرة market_regime (ADX على الفريم الأعلى، عتبة أعلى وأكثر موثوقية بالبيانات
    # — كان أهم ميزة إطلاقًا بنموذج ML التجريبي). إبقاء الشرطين معًا كان يقلل عدد
    # الإشارات دون فائدة إضافية مؤكدة، بينما market_regime وحده يغطي نفس الفكرة
    # بشكل أدق. الحقل "ranging" يبقى محسوبًا ومحفوظًا للتشخيص فقط، بدون تأثير على الفلترة.
    strong = [
        r for r in results
        if r["score"] >= 1.5 and r["score"] < MAX_OFFICIAL_SCORE
        and r["vol_confirm"] and r["atr_pct"] >= 0.08 and r["persistent"]
        and not r["near_resistance"]
        and r["htf_aligned"] is True
        and r["market_regime"] == "trending_up"
        and meets_min_profit(r["entry"], r["tps"])
    ]
    strong_symbols = {r["symbol"] for r in strong}

    # --- تشخيص مؤقت: قياس أثر كل فلتر لوحده على عدد الإشارات الرسمية المؤهلة ---
    # يطبع فقط بالـ logs (مو بتيليجرام)، للمساعدة بتحديد أي شرط يستبعد الأكثر
    # بأيام السوق المتقلبة (مثل قفزة +7% ببيتكوين بيوم واحد بتاريخ 21-22 أوت).
    _score_ok = [r for r in results if r["score"] >= 1.5]
    _under_cap = [r for r in _score_ok if r["score"] < MAX_OFFICIAL_SCORE]
    _capped_out = [r for r in _score_ok if r["score"] >= MAX_OFFICIAL_SCORE]
    _also_vol_atr_persist = [
        r for r in _under_cap
        if r["vol_confirm"] and r["atr_pct"] >= 0.08 and r["persistent"]
    ]
    _also_not_ranging_res = [
        r for r in _also_vol_atr_persist if not r["near_resistance"]
    ]
    _also_htf_regime = [
        r for r in _also_not_ranging_res
        if r["htf_aligned"] is True and r["market_regime"] == "trending_up"
    ]
    print(
        "🔍 تشخيص فلترة الرسمية: "
        f"score>=1.5: {len(_score_ok)} | "
        f"مستبعدة بسقف MAX_OFFICIAL_SCORE={MAX_OFFICIAL_SCORE}: {len(_capped_out)} "
        f"({', '.join(r['symbol'] for r in _capped_out) or '—'}) | "
        f"تحت السقف: {len(_under_cap)} | "
        f"+حجم/تقلب/استقرار: {len(_also_vol_atr_persist)} | "
        f"+بعيدة عن مقاومة: {len(_also_not_ranging_res)} | "
        f"+htf_aligned وmarket_regime=trending_up: {len(_also_htf_regime)} | "
        f"+ربح أدنى محقق (strong نهائي): {len(strong)}"
    )

    # إشارات مبكرة (انضغاط تقلب / تراكم صامت) لعملات لم تصل بعد لإشارة شراء كاملة —
    # تُميَّز بمفتاح منفصل (":early") في ذاكرة التنبيهات كي لا تتعارض مع إشارات الشراء الرسمية
    # (رسمية أو مبكرة accumulation/squeeze/كليهما) تُصفّى هنا أيضًا بنفس شرط الحد الأدنى
    # لنسبة الربح (MIN_PROFIT_PCT) قبل اعتبارها مؤهلة أصلاً — وليس فقط عند الإرسال —
    # كي لا تُسجَّل كـ"مُنبَّه عليها" في الذاكرة وتُحرَم من الإرسال لاحقًا إن تحسّن ربحها المتوقع
    early_eligible = [
        r for r in results
        if r["score"] < 1.5 and (r["squeeze"] or r["accumulation"])
        and r.get("early_confidence") is not None
        and meets_min_profit(r["early_entry"], r["early_tps"])
    ]
    early_keys = {f"{r['symbol']}:early" for r in early_eligible}

    # إشارات انفجار (breakout) — اختراق قمة سابقة مع تأكيد حجم، مستقلة عن الإشارة الرسمية،
    # تُستبعد العملات اللي أصلاً عندها إشارة رسمية جديدة تجنبًا للتكرار
    breakout_eligible = [
        r for r in results
        if r.get("breakout_entry") is not None
        and r["symbol"] not in strong_symbols
        and meets_min_profit(r["breakout_entry"], r["breakout_tps"])
    ]
    breakout_keys = {f"{r['symbol']}:breakout" for r in breakout_eligible}

    prev_alerted, prev_dominance = load_state()
    fresh = [r for r in strong if r["symbol"] not in prev_alerted]
    fresh_early = [r for r in early_eligible if f"{r['symbol']}:early" not in prev_alerted]
    fresh_breakout = [r for r in breakout_eligible if f"{r['symbol']}:breakout" not in prev_alerted]

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
          f"إشارات مبكرة جديدة: {len(fresh_early)} | إشارات انفجار جديدة: {len(fresh_breakout)}")

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

    for r in fresh_breakout:
        alert_text = format_breakout_alert(r)
        r["_msg_id"] = send_telegram(alert_text)
        r["_alert_text"] = alert_text
        time.sleep(1)

    # تسجيل الإشارات الجديدة كصفقات مفتوحة قيد المتابعة لاحقًا (رسمية + مبكرة + انفجار)
    open_new_positions(open_positions, fresh)
    open_new_early_positions(open_positions, fresh_early)
    open_new_breakout_positions(open_positions, fresh_breakout)

    # حفظ موحّد: ذاكرة الإشارات (رسمية + مبكرة + انفجار) + BTC Dominance + الصفقات المفتوحة + أرشيف الصفقات المغلقة حديثًا
    # + إحصائيات أداء محسوبة من السجل المحدَّث (خيار 3: تتبع فقط، بدون تعديل تلقائي على منطق البوت)
    save_all_state(strong_symbols | early_keys | breakout_keys, btc_dominance, open_positions, closed_now)

    print("انتهى المسح.")


if __name__ == "__main__":
    main()
