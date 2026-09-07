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
import sys
import json
import time
import traceback
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

# ---------- إعدادات إشارة الانفجار (Breakout) ----------
BREAKOUT_LOOKBACK = 10          # عدد الشموع للبحث فيها عن أعلى قمة سابقة قبل الاختراق
BREAKOUT_VOL_MULT = 1.3         # الحجم الحالي يجب أن يتجاوز متوسط الحجم بهذا المضاعف
BREAKOUT_MIN_ATR_PCT = 0.08     # أدنى نسبة تقلب (ATR%) لقبول إشارة الانفجار

# ---------- إعدادات الإشارة التجريبية (Ichimoku + تأكيد حجم + دعم/مقاومة + MFI + فلتر ATR) ----------
ICHIMOKU_TENKAN_PERIOD = 9
ICHIMOKU_KIJUN_PERIOD = 26
ICHIMOKU_CROSS_LOOKBACK = 3      # عدد الشموع للبحث فيها عن تقاطع تينكان/كيجون حديث (وليس قديمًا انتهى زخمه)
MFI_PERIOD = 14
MFI_MIN = 20                     # تحت هذا المستوى تدفق أموال ضعيف جدًا (تشبع بيعي) رغم أي تقاطع
MFI_MAX = 75                     # فوق هذا المستوى تدفق شرائي مبالغ فيه (خطر ارتداد قريب)
EXPERIMENTAL_VOL_MULT = 1.1      # الحجم الحالي يجب أن يتجاوز متوسطه بهذا المضاعف لتأكيد الإشارة
EXPERIMENTAL_MIN_ATR_PCT = float(os.environ.get("EXPERIMENTAL_MIN_ATR_PCT", "0.1"))
# أدنى نسبة تقلب (ATR%) لقبول الإشارة التجريبية -- تفادي الدخول بأسواق شبه راكدة الحركة
EXPERIMENTAL_SL_ATR_MULT = float(os.environ.get("EXPERIMENTAL_SL_ATR_MULT", "1.5"))

# ---------------- إعدادات الحد الأدنى لنسبة الربح المستهدفة ----------------
# لا تُرسل أي إشارة (رسمية أو مبكرة) إلا لو كانت نسبة الربح المتوقعة عند أول هدف (TP1)
# مقارنة بسعر الدخول >= هذه النسبة% — لتفادي إشارات ذات هدف قريب جدًا لا يستحق الدخول
MIN_PROFIT_PCT = float(os.environ.get("MIN_PROFIT_PCT", "1.0"))

# ---------------- إعدادات أهداف الإشارات المبكرة (تقديرية، أقل ثقة من الإشارة الرسمية) ----------------
# وقف خسارة أوسع من الإشارة الرسمية (1.5×ATR) لأن نقطة الدخول أقل دقة والتقلب حولها أعلى
EARLY_SL_ATR_MULT = 2.0
# عدد الأهداف والثقة يعتمدان مباشرة على عدد الشروط المتحققة (squeeze / accumulation / divergence / momentum):
# شرط واحد = احتمالية (هدف واحد)، شرطان = مؤكدة (هدفان)، 3 فأكثر = مؤكدة قوية (3-4 أهداف)

# ---------------- إعدادات تحسينات إضافية (2026-09-07) ----------------
# الحد الأدنى لنسبة العائد إلى المخاطرة (Reward:Risk) — يُرفض أي إشارة (أي نوع) لو TP1
# لا يحقق هذه النسبة مقارنة بالمسافة بين الدخول ووقف الخسارة، حتى لو حقق MIN_PROFIT_PCT
MIN_RR_RATIO = float(os.environ.get("MIN_RR_RATIO", "1.5"))

# وزن "الزخم المتفق عليه" (EMA trend + MACD) في نظام score — كانا يُحسبان كعاملين منفصلين
# رغم ارتباطهما الوثيق (كلاهما يقيس نفس ظاهرة الزخم تقريبًا)، فيُعطى الزخم وزنًا مضاعفًا
# بدون قصد. الآن لو اتفقا يُعطيان وزنًا واحدًا مجمّعًا (1.5 بدل 2)، ولو اختلفا فالنتيجة صفر كالسابق.
MOMENTUM_AGREE_WEIGHT = float(os.environ.get("MOMENTUM_AGREE_WEIGHT", "1.5"))

# نسبة "الوسادة" فوق نقطة الدخول عند نقل وقف الخسارة للتعادل (Breakeven) بعد لمس أول هدف —
# بدل نقله لنقطة الدخول تمامًا (قد يُخرج الصفقة بسرعة من رجّة سعرية بسيطة قبل استكمال الحركة
# نحو الأهداف التالية)، يُنقل لمسافة صغيرة فوق الدخول كنسبة من المخاطرة الأصلية للصفقة
BREAKEVEN_BUFFER_RATIO = float(os.environ.get("BREAKEVEN_BUFFER_RATIO", "0.2"))

# نطاق حيادي حول الصفر (%) لتصنيف نتيجة الصفقة النهائية "بدون تغيير حقيقي" بدل ربح/خسارة
# صريحين — يُستخدم في compute_stats لتفادي تصنيف صفقة لمست TP1 ثم رجعت لنقطة قريبة من
# الصفر كـ"فوز" (المشكلة التي حُدِّدت بالمراجعة الخارجية للكود)
BREAKEVEN_BAND_PCT = float(os.environ.get("BREAKEVEN_BAND_PCT", "0.1"))


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


def compute_adx_di(highs, lows, closes, period=ADX_PERIOD):
    """
    مؤشر قوة الاتجاه (ADX) مع خطي الاتجاه +DI/-DI. ADX يقيس قوة الاتجاه فقط (بصرف النظر
    عن اتجاهه)، بينما +DI/-DI يحددان اتجاهه الفعلي: +DI > -DI يعني ضغط شرائي مسيطر،
    والعكس يعني ضغط بيعي مسيطر — يُستخدمان معًا لتأكيد أن اتجاه EMA السريع (trend_up)
    مدعوم فعليًا باتجاه الزخم الأعمق، لا مجرد تقاطع سطحي لمتوسطين.
    يرجع (adx_list, plus_di_list, minus_di_list) بنفس طول closes، بقيم None قبل اكتمال الفترة.
    """
    n = len(closes)
    adx_out = [None] * n
    pdi_out = [None] * n
    mdi_out = [None] * n
    if n <= period * 2:
        return adx_out, pdi_out, mdi_out

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
        pdi_out[i] = pdi
        mdi_out[i] = mdi
        dx[i] = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0

    start = period * 2
    valid_dx = [x for x in dx[period + 1:start + 1] if x is not None]
    if not valid_dx:
        return adx_out, pdi_out, mdi_out
    adx_out[start] = sum(valid_dx) / len(valid_dx)
    for i in range(start + 1, n):
        if adx_out[i - 1] is None or dx[i] is None:
            continue
        adx_out[i] = (adx_out[i - 1] * (period - 1) + dx[i]) / period
    return adx_out, pdi_out, mdi_out


def adx(highs, lows, closes, period=ADX_PERIOD):
    """توافقية مع الاستدعاءات القديمة: يرجع قائمة ADX فقط بدون +DI/-DI."""
    return compute_adx_di(highs, lows, closes, period)[0]


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


def resistance_levels(highs, closes, lookback=RESISTANCE_LOOKBACK, pivot_span=RESISTANCE_PIVOT_SPAN, max_levels=3):
    """
    يرجع حتى max_levels من مستويات المقاومة (قمم سعرية سابقة) فوق السعر الحالي، مرتبة
    تصاعديًا (الأقرب أولاً) — بدل الاكتفاء بأقرب واحدة فقط. يفيد لاحقًا في تحليل
    Reward/Risk الحقيقي عبر عدة حواجز سعرية، وليس فقط أقرب حاجز.
    """
    n = len(highs)
    window_n = min(lookback, n)
    start = n - window_n
    price = closes[-1]
    pivots = []
    for i in range(max(start, pivot_span), n - pivot_span):
        window = highs[i - pivot_span:i + pivot_span + 1]
        if highs[i] == max(window):
            pivots.append(highs[i])
    above = sorted(set(p for p in pivots if p > price))
    return above[:max_levels]


def nearest_resistance(highs, closes, lookback=RESISTANCE_LOOKBACK, pivot_span=RESISTANCE_PIVOT_SPAN):
    """يرجع أقرب مستوى مقاومة (قمة سعرية سابقة) فوق السعر الحالي، أو None لو لا توجد."""
    levels = resistance_levels(highs, closes, lookback, pivot_span, max_levels=1)
    return levels[0] if levels else None


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


def ichimoku_tenkan_kijun(highs, lows, tenkan_period=ICHIMOKU_TENKAN_PERIOD, kijun_period=ICHIMOKU_KIJUN_PERIOD):
    """
    خطا تينكان-سين وكيجون-سين من مؤشر إيشيموكو: كل منهما متوسط (أعلى قمة + أدنى قاع) خلال
    فترته. تقاطع تينكان فوق كيجون يُعتبر إشارة زخم صاعد أقوى من تقاطع MACD في كثير من
    الحالات لأنه مبني مباشرة على نطاق السعر الفعلي (High/Low) لا الإغلاق فقط.
    """
    n = len(highs)
    tenkan = [None] * n
    kijun = [None] * n
    for i in range(n):
        if i >= tenkan_period - 1:
            window_h = highs[i - tenkan_period + 1:i + 1]
            window_l = lows[i - tenkan_period + 1:i + 1]
            tenkan[i] = (max(window_h) + min(window_l)) / 2
        if i >= kijun_period - 1:
            window_h = highs[i - kijun_period + 1:i + 1]
            window_l = lows[i - kijun_period + 1:i + 1]
            kijun[i] = (max(window_h) + min(window_l)) / 2
    return tenkan, kijun


def tenkan_kijun_bullish_cross(tenkan, kijun, i, lookback=ICHIMOKU_CROSS_LOOKBACK):
    """
    يتحقق أن تينكان فوق كيجون حاليًا، وأن التقاطع الفعلي (تينكان يعبر من تحت لفوق كيجون)
    حصل خلال آخر lookback شمعة -- وليس تقاطعًا قديمًا انتهى زخمه ولم يعد "حدثًا" فعليًا.
    """
    if i < 1 or tenkan[i] is None or kijun[i] is None:
        return False
    if tenkan[i] <= kijun[i]:
        return False
    start = max(1, i - lookback + 1)
    for j in range(start, i + 1):
        if tenkan[j - 1] is None or kijun[j - 1] is None:
            continue
        if tenkan[j - 1] <= kijun[j - 1] and tenkan[j] > kijun[j]:
            return True
    return False


def mfi(highs, lows, closes, vols, period=MFI_PERIOD):
    """
    مؤشر تدفق الأموال (Money Flow Index) -- نفس فكرة RSI لكنه يزن كل حركة سعرية بحجم
    التداول المرافق لها، فيجمع بين الزخم والحجم في مؤشر واحد أقوى تشخيصيًا من RSI المجرد.
    """
    n = len(closes)
    out = [None] * n
    if n <= period:
        return out
    typical = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(n)]
    raw_flow = [typical[i] * vols[i] for i in range(n)]
    for i in range(period, n):
        pos_flow = neg_flow = 0.0
        for j in range(i - period + 1, i + 1):
            if typical[j] > typical[j - 1]:
                pos_flow += raw_flow[j]
            elif typical[j] < typical[j - 1]:
                neg_flow += raw_flow[j]
        if neg_flow == 0:
            out[i] = 100.0
        elif pos_flow == 0:
            out[i] = 0.0
        else:
            money_ratio = pos_flow / neg_flow
            out[i] = 100 - (100 / (1 + money_ratio))
    return out


def mfi_bullish_flow(mfi_vals, i):
    """
    تدفق أموال صاعد صحي: MFI بين حد أدنى (ليس بتشبع بيعي حاد يلمّح لضعف مستمر) وحد أعلى
    (ليس بتشبع شرائي مبالغ فيه يهدد بارتداد قريب)، ويتحرك صاعدًا فعليًا لا هابطًا.
    """
    if i < 1 or mfi_vals[i] is None or mfi_vals[i - 1] is None:
        return False
    return MFI_MIN <= mfi_vals[i] <= MFI_MAX and mfi_vals[i] > mfi_vals[i - 1]


def experimental_detect(tenkan, kijun, i):
    """المحفّز الأساسي للإشارة التجريبية: تقاطع تينكان/كيجون صاعد حديث."""
    return tenkan_kijun_bullish_cross(tenkan, kijun, i)


def experimental_quality(ind, i, vol_confirm_flag, mfi_vals):
    """
    تقييم جودة الإشارة التجريبية (0 إلى 3): دعم الاتجاه العام (EMA7>EMA14)، تأكيد الحجم
    (حجم فوق المتوسط + زخم OBV متوافق مع الاتجاه معًا)، وتدفق أموال صاعد صحي (MFI).
    """
    details = {}
    score = 0
    ema7, ema14 = ind.get("ema7"), ind.get("ema14")
    if ema7 and ema14 and i < len(ema7) and ema7[i] is not None and ema7[i] > ema14[i]:
        score += 1
        details["trend_support"] = True
    if vol_confirm_flag:
        score += 1
        details["volume_confirm"] = True
    if mfi_bullish_flow(mfi_vals, i):
        score += 1
        details["mfi_bullish"] = True
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
    tenkan, kijun = ichimoku_tenkan_kijun(highs, lows)
    adx_vals, plus_di_vals, minus_di_vals = compute_adx_di(highs, lows, closes)
    return {
        "closes": closes, "highs": highs, "lows": lows, "vols": vols,
        "ema7": ema(closes, 7), "ema14": ema(closes, 14),
        "ema50": ema(closes, EXTENSION_EMA_PERIOD),
        "rsi": rsi(closes),
        "macd": macd_line, "signal": signal,
        "bb_upper": bb_upper, "bb_lower": bb_lower,
        "vol_avg": rolling_avg(vols, 20),
        "adx": adx_vals, "plus_di": plus_di_vals, "minus_di": minus_di_vals,
        "obv": obv(closes, vols),
        "tenkan": tenkan, "kijun": kijun,
        "mfi": mfi(highs, lows, closes, vols),
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

    # --- الزخم (EMA trend + MACD) كعامل واحد مجمّع بدل عاملين منفصلين ---
    # EMA7>EMA14 وMACD bullish يقيسان نفس ظاهرة الزخم تقريبًا (مترابطان)، فجمعهما كعاملين
    # منفصلين كان يعطي نفس المعلومة وزنًا مضاعفًا حين يتفقان (double counting). الآن: لو
    # اتفقا -> وزن واحد مجمّع (MOMENTUM_AGREE_WEIGHT، افتراضيًا 1.5 بدل 2)، ولو اختلفا -> صفر
    # (تمامًا كالسلوك السابق، فلا تغيير هناك).
    momentum_agree = trend_up == macd_bull
    momentum_component = (trend_dir * MOMENTUM_AGREE_WEIGHT) if momentum_agree else 0

    score = momentum_component + rsi_state + bb_state + vol_score

    # --- فلاتر إضافية لتحسين جودة الإشارة (ADX / انحراف / مقاومة / OBV / إرهاق) ---
    # تُحسب دائمًا للعرض التشخيصي، لكن تُطبَّق على الدرجة فقط لو apply_extra_filters=True
    # (يُستخدم False في الاختبار الرجعي لمقارنة الأداء بدونها)

    adx_val = ind["adx"][i] if i < len(ind["adx"]) else None
    ranging = adx_val is not None and adx_val < ADX_THRESHOLD

    # اتجاه +DI/-DI: ADX وحده يقيس قوة الاتجاه فقط بصرف النظر عن جهته؛ +DI/-DI يؤكدان
    # (أو ينفيان) أن الاتجاه الفعلي المسيطر يتوافق فعلاً مع اتجاه EMA السريع (trend_up)
    plus_di = ind.get("plus_di", [None] * len(ind["closes"]))[i] if i < len(ind.get("plus_di", [])) else None
    minus_di = ind.get("minus_di", [None] * len(ind["closes"]))[i] if i < len(ind.get("minus_di", [])) else None
    if plus_di is not None and minus_di is not None:
        di_confirm = (plus_di > minus_di) if trend_up else (minus_di > plus_di)
    else:
        di_confirm = None

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
        elif di_confirm is False:
            # السوق فعليًا متجه (ADX ليس عرضيًا) لكن +DI/-DI يناقضان اتجاه EMA السريع —
            # عقوبة جزئية (وليست كاملة كالسوق العرضي) لأن الإشارة أقل موثوقية من ظاهرها
            score *= 0.7
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
        "plus_di": plus_di, "minus_di": minus_di, "di_confirm": di_confirm,
        "divergence": divergence,
        "near_resistance": near_resistance, "resistance": resistance,
        "obv_confirm": obv_confirm,
        "extended": extended,
        "momentum_agree": momentum_agree,
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

# تقدير تكلفة العمولة (دخول + خروج) — يُطرح من الربح النظري قبل مقارنته بـMIN_PROFIT_PCT
# عدّل هذا الرقم لو عمولتك الفعلية مختلفة (مثلاً تستخدم BNB لخصم العمولة أو مستوى VIP معين)
TRADING_FEE_PCT = float(os.environ.get("TRADING_FEE_PCT", "0.2"))

# حماية استهلاك حصة Binance بالدقيقة (الحد الفعلي 1200)؛ نتوقف مؤقتًا قبل الوصول له بهامش أمان
BINANCE_WEIGHT_LIMIT = 1200
WEIGHT_SAFETY_MARGIN = int(os.environ.get("WEIGHT_SAFETY_MARGIN", "1000"))


def meets_min_profit(entry, tps, min_pct=MIN_PROFIT_PCT, fee_pct=TRADING_FEE_PCT):
    """
    يتحقق أن أقرب هدف (TP1) يحقق نسبة ربح صافية (بعد خصم تكلفة تقديرية للعمولة
    دخول+خروج عبر TRADING_FEE_PCT) >= الحد الأدنى المطلوب (MIN_PROFIT_PCT)،
    مقارنة بسعر الدخول. يُستخدم لتصفية أي إشارة (رسمية أو مبكرة أو انفجار أو تجريبية)
    قبل اعتبارها مؤهلة للإرسال، بصرف النظر عن مصدرها.
    """
    if not entry or not tps:
        return False
    tp1_profit_pct = (tps[0] - entry) / entry * 100
    net_profit_pct = tp1_profit_pct - fee_pct
    return net_profit_pct >= min_pct


def meets_min_rr(entry, sl, tps, min_rr=MIN_RR_RATIO):
    """
    يتحقق أن نسبة العائد إلى المخاطرة (Reward:Risk) عند آخر هدف فعليًا مُخصَّص لهذي
    الصفقة (وليس TP1) مقارنة بمسافة وقف الخسارة >= الحد الأدنى المطلوب.

    ملاحظة مهمة: TP1 بكل أنواع الإشارات دائمًا = 1R بالضبط بالتصميم (الثقة تُترجم
    لعدد أهداف أكبر لا لمضاعف TP1 أكبر)، فلو قِسنا R:R عند TP1 لكانت النتيجة 1.0
    ثابتة دائمًا مهما كانت الصفقة -- وهذا يرفض كل الإشارات بلا استثناء بشكل غير مقصود.
    لذلك نقيسه عند آخر هدف (tps[-1])، وهو أيضًا يعكس تلقائيًا أي تقليم بسبب مقاومة قريبة.
    """
    if not entry or not sl or not tps:
        return False
    risk = entry - sl
    if risk <= 0:
        return False
    reward = tps[-1] - entry
    if reward <= 0:
        return False
    return (reward / risk) >= min_rr


def _request_with_retry(url, params=None, timeout=20, retries=3, backoff=1.5):
    """
    طلب HTTP مع إعادة محاولة تلقائية عند فشل الشبكة أو ضغط مؤقت من Binance (429/5xx).
    يقرأ أيضًا header الوزن المستهلك (X-MBX-USED-WEIGHT-1M) بعد كل رد ناجح، ويتوقف
    مؤقتًا قبل الاستمرار لو اقترب من حد Binance — بدل انتظار الرفض الفعلي (429) والتعامل
    معه كخطأ لاحقًا.
    """
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)

            used_weight = int(r.headers.get("X-MBX-USED-WEIGHT-1M", 0))
            if used_weight >= WEIGHT_SAFETY_MARGIN:
                wait = 60
                print(f"⚠️ اقتراب من حد Binance (وزن {used_weight}/{BINANCE_WEIGHT_LIMIT}) — انتظار {wait}ث")
                time.sleep(wait)

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

def analyze_symbol(t, interval, error_list=None):
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
        atrp = atr_percent(ind)  # نسبة ATR% محسوبة مرة واحدة، تُستخدم لفلتر الانفجار والتجريبية معًا

        prev_r = score_at(last - 1, ind)
        persistent = bool(prev_r) and (prev_r["score"] > 0) == (r["score"] > 0) and abs(prev_r["score"]) >= 0.5

        final_score = r["score"]
        htf_checked, htf_aligned = False, None
        if abs(r["score"]) >= 0.5:
            htf = HTF_MAP.get(interval)
            if htf:
                try:
                    # تأكيد أقوى من مجرد EMA7>EMA14: نتحقق من هيكل EMA20/EMA50 + ميل EMA50
                    # + موقع السعر منها + ADX>عتبة (اتجاه فعلي قوي وليس تقاطعًا سطحيًا)
                    htf_klines = fetch_klines(symbol, htf, 100)
                    htf_klines = drop_unclosed_candle(htf_klines)
                    htf_closes = [float(k[4]) for k in htf_klines]
                    htf_highs = [float(k[2]) for k in htf_klines]
                    htf_lows = [float(k[3]) for k in htf_klines]
                    if len(htf_closes) >= 56:
                        htf_ema20 = ema(htf_closes, 20)
                        htf_ema50 = ema(htf_closes, 50)
                        htf_adx_vals = adx(htf_highs, htf_lows, htf_closes)
                        htf_adx_last = htf_adx_vals[-1]
                        htf_adx_ok = htf_adx_last is not None and htf_adx_last > ADX_THRESHOLD
                        htf_ema50_rising = htf_ema50[-1] > htf_ema50[-6]
                        htf_bullish_strict = (
                            htf_ema20[-1] > htf_ema50[-1]
                            and htf_ema50_rising
                            and htf_closes[-1] > htf_ema50[-1]
                            and htf_adx_ok
                        )
                        htf_bearish_strict = (
                            htf_ema20[-1] < htf_ema50[-1]
                            and not htf_ema50_rising
                            and htf_closes[-1] < htf_ema50[-1]
                            and htf_adx_ok
                        )
                        trend_dir = 1 if r["trend_up"] else -1
                        htf_aligned = htf_bullish_strict if r["trend_up"] else htf_bearish_strict
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
            if conditions_met >= 1:
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
            if breakout_score >= 1 and atrp >= BREAKOUT_MIN_ATR_PCT:
                breakout_entry = ind["closes"][last]
                atrv = atr_value(ind)
                breakout_sl = breakout_entry - atrv * 1.8
                risk = breakout_entry - breakout_sl
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

        # إشارة تجريبية (Ichimoku Tenkan/Kijun + تأكيد حجم/OBV + MFI) — مستقلة عن الإشارة
        # الرسمية والانفجار، محفّزها تقاطع تينكان/كيجون صاعد حديث. تُرفض كليًا (لا تُطلق حتى
        # بدرجة منخفضة) لو السعر قريب جدًا من مقاومة قوية أو لو التقلب (ATR%) ضعيف جدًا —
        # هذان شرطا استبعاد صريحان وليسا مجرد نقطتي تقييم إضافيتين.
        vol_avg_last = ind["vol_avg"][last]
        vol_ok = vol_avg_last is not None and ind["vols"][last] > vol_avg_last * EXPERIMENTAL_VOL_MULT
        obv_ok = obv_confirms_trend(ind["obv"][:last + 1], r["trend_up"])
        experimental_vol_confirm = vol_ok and obv_ok

        experimental = experimental_detect(ind["tenkan"], ind["kijun"], last)
        experimental_score, experimental_details = 0, {}
        experimental_entry = experimental_sl = None
        experimental_tps = []
        if experimental:
            experimental_score, experimental_details = experimental_quality(
                ind, last, experimental_vol_confirm, ind["mfi"]
            )
            if (
                experimental_score >= 1
                and atrp >= EXPERIMENTAL_MIN_ATR_PCT
                and not r["near_resistance"]
            ):
                experimental_entry = ind["closes"][last]
                atrv = atr_value(ind)
                experimental_sl = experimental_entry - atrv * EXPERIMENTAL_SL_ATR_MULT
                risk = experimental_entry - experimental_sl
                if experimental_score >= 3:
                    tp_count = 3
                elif experimental_score >= 2:
                    tp_count = 2
                else:
                    tp_count = 1
                raw_tps = [experimental_entry + risk * i for i in range(1, tp_count + 1)]
                resistance = r.get("resistance")
                if resistance:
                    trimmed = []
                    for tp in raw_tps:
                        if tp >= resistance:
                            trimmed.append(resistance)
                            break
                        trimmed.append(tp)
                    experimental_tps = trimmed
                else:
                    experimental_tps = raw_tps

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

            raw_tps = [entry + risk * i for i in range(1, tp_count + 1)]
            # تقليم الأهداف عند أول هدف يتجاوز أقرب مقاومة معروفة — نفس المنطق المطبَّق
            # أصلاً على المبكرة/الانفجار/التجريبية، كان غائبًا عن الرسمية سابقًا (ثغرة:
            # كانت أهداف الرسمية تُبنى على ATR فقط بصرف النظر عن هيكل السوق)
            resistance = r.get("resistance")
            if resistance:
                trimmed = []
                for tp in raw_tps:
                    if tp >= resistance:
                        trimmed.append(resistance)
                        break
                    trimmed.append(tp)
                tps = trimmed
            else:
                tps = raw_tps

        return {
            "symbol": symbol,
            "price": ind["closes"][last],
            "change_pct": float(t["priceChangePercent"]),
            "score": final_score,
            "trend_up": r["trend_up"],
            "vol_confirm": r["vol_confirm"],
            "atr_pct": atrp,
            "persistent": persistent,
            "htf_checked": htf_checked,
            "htf_aligned": htf_aligned,
            "ranging": r["ranging"],
            "di_confirm": r["di_confirm"],
            "momentum_agree": r["momentum_agree"],
            "divergence": r["divergence"],
            "near_resistance": r["near_resistance"],
            "resistance_levels": resistance_levels(ind["highs"][:last + 1], ind["closes"][:last + 1]),
            "obv_confirm": r["obv_confirm"],
            "extended": r["extended"],
            "rsi_state": r["rsi_state"],
            "macd_bull": r["macd_bull"],
            "bb_state": r["bb_state"],
            "squeeze": squeeze,
            "accumulation": accumulation,
            "momentum": momentum,
            "breakout": breakout,
            "breakout_score": breakout_score,
            "breakout_details": breakout_details,
            "experimental": experimental,
            "experimental_score": experimental_score,
            "experimental_details": experimental_details,
            "experimental_entry": experimental_entry,
            "experimental_sl": experimental_sl,
            "experimental_tps": experimental_tps,
            "entry": entry, "sl": sl, "tps": tps,
            "early_entry": early_entry, "early_sl": early_sl, "early_tps": early_tps,
            "early_confidence": early_confidence,
            "breakout_entry": breakout_entry, "breakout_sl": breakout_sl, "breakout_tps": breakout_tps,
        }
    except Exception as e:
        print(f"[تخطي] {symbol}: {e}")
        if error_list is not None:
            error_list.append(symbol)
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
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(analyze_symbol, t, INTERVAL, errors) for t in shortlist]
        for f in concurrent.futures.as_completed(futures):
            r = f.result()
            if r:
                results.append(r)

    # نسبة فشل مرتفعة بتحليل الرموز (وليس مجرد "لا إشارة") تلمّح لمشكلة حقيقية
    # (Rate limit من Binance، تغيّر بصيغة البيانات، انقطاع شبكي جزئي...) وليس مجرد سوق هادئ
    if shortlist and len(errors) / len(shortlist) >= 0.3:
        send_admin_alert(
            f"نسبة أخطاء تحليل مرتفعة: {len(errors)} من {len(shortlist)} عملة فشل تحليلها "
            f"({len(errors) / len(shortlist) * 100:.0f}%).\n"
            f"أمثلة: {', '.join(errors[:8])}"
        )

    return results


# ---------------- تيليجرام ----------------

def send_telegram(text, retries=2):
    """يرسل رسالة تيليجرام جديدة، ويرجع message_id الخاص فيها (أو None عند الفشل) —
    يُستخدم لاحقًا لتعديل نفس الرسالة (شطبها + إضافة النتيجة) عند إغلاق الصفقة.
    يعيد المحاولة مرة إضافية عند فشل شبكي/مؤقت قبل الاستسلام، لتقليل احتمال ضياع
    إشعار مهم (دخول/TP/SL) بسبب عطل عابر بشبكة تيليجرام."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ TELEGRAM_TOKEN أو TELEGRAM_CHAT_ID غير موجودين — تخطي الإرسال.")
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
            if resp.ok:
                return resp.json().get("result", {}).get("message_id")
            last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            print(f"فشل إرسال تيليجرام (محاولة {attempt}):", last_err)
        except Exception as e:
            last_err = str(e)
            print(f"خطأ إرسال تيليجرام (محاولة {attempt}):", last_err)
        if attempt < retries:
            time.sleep(2)
    print(f"❌ فشل إرسال تيليجرام نهائيًا بعد {retries} محاولة/محاولات: {last_err}")
    return None


def send_admin_alert(text):
    """تنبيه نظام/عطل — مستقل عن منطق كتم إشارات الشرط الواحد، يُستخدم فقط للإبلاغ
    عن أعطال فعلية بالتشغيل (Gist، تحليل، انهيار غير متوقع...) بدل الاكتفاء بطباعتها
    في لوق GitHub Actions الذي لا يُتابعه أحد يوميًا."""
    print(f"🚨 ADMIN ALERT: {text}")
    send_telegram(f"🚨 تنبيه نظام (Scanner)\n\n{text}")


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
    base = pos.get("alert_text") or ""
    hit_sorted = sorted(pos.get("hit_tps", []))
    if not hit_sorted:
        return base
    lines = [format_tp_line(pos, j) for j in hit_sorted]
    return base + "\n\n" + "\n".join(lines)


def format_alert(r, market_caution=False):
    """
    تنبيه إشارة رسمية — قالب مبسّط: بدون الدرجة/الفريم/السعر/الشارات العربية،
    فقط النوع + الرمز + خطة الدخول (أو تحذير بدون خطة دخول).
    """
    is_buy = r["score"] >= 1.5
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
        # السوق الفوري لا يدعم فتح صفقة بيع مكشوفة — فلا توجد خطة دخول لإشارات "تجنب شراء"
        lines.append("لا توجد خطة دخول (تحذير فقط)")

    return "\n".join(lines)


def format_early_alert(r):
    """
    تنبيه رادار مبكر: انضغاط تقلب و/أو تراكم صامت و/أو انحراف/زخم لعملة لم تصل بعد
    لإشارة شراء كاملة. يعرض أهدافًا تقديرية (وقف خسارة أوسع من الرسمية + عدد أهداف
    متغير حسب مستوى الثقة)، وتُتابَع تلقائيًا (TP/SL) ضمن نفس آلية الصفقات المفتوحة.
    قالب مختصر: النوع + الرمز + المصدر (بالإنجليزية الخام، بدون ترجمة) + الدخول/الأهداف/وقف الخسارة فقط.
    """
    confidence = r.get("early_confidence")
    dot = "🟢" if confidence == "مؤكدة قوية" else ("🟣" if confidence == "مؤكدة" else "🔵")
    title = f"إشارة {confidence}" if confidence else "إشارة مبكرة"

    factors = []
    if r.get("squeeze"):
        factors.append("squeeze")
    if r.get("accumulation"):
        factors.append("accumulation")
    if r.get("divergence"):
        factors.append("divergence")
    if r.get("momentum"):
        factors.append("momentum")
    source_label = "+".join(sorted(factors))

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
    """إشارة انفجار زخم: اختراق قمة سابقة مع تأكيد حجم — تدخل مبكرًا مع بداية الزخم.
    قالب مبسّط بنفس أسلوب الإشارة المبكرة: المصدر بالإنجليزية الخام بدل شارات عربية."""
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


def format_experimental_alert(r):
    """
    إشارة تجريبية: تقاطع Ichimoku Tenkan/Kijun صاعد حديث + تأكيد حجم/OBV + تدفق أموال (MFI)
    صحي، مع استبعاد كامل لو قريبة من مقاومة قوية أو التقلب ضعيف جدًا. قالب نفس أسلوب
    إشارتي المبكرة والانفجار: المصدر بالإنجليزية الخام بدل شارات عربية.
    """
    e_score = r.get("experimental_score", 0)
    dot = "🧪🟢" if e_score >= 3 else ("🧪🟡" if e_score >= 2 else "🧪⚪")
    title = "إشارة تجريبية"
    details = r.get("experimental_details", {})
    factors = ["ichimoku_cross"]
    if details.get("trend_support"):
        factors.append("trend_support")
    if details.get("volume_confirm"):
        factors.append("volume_confirm")
    if details.get("mfi_bullish"):
        factors.append("mfi_bullish")
    source_label = "+".join(factors)

    lines = [
        f"{dot} {title}",
        r['symbol'].replace('USDT', '/USDT'),
    ]
    if source_label:
        lines.append(f"المصدر: {source_label}")

    if r.get("experimental_entry") is not None:
        lines.append(f"الدخول: {r['experimental_entry']:.6g}")
        for i, tp in enumerate(r.get("experimental_tps", []), start=1):
            lines.append(f"TP{i}: {tp:.6g}")
        lines.append(f"SL: {r['experimental_sl']:.6g}")

    return "\n".join(lines)


# ---------------- إدارة الحالة عبر GitHub Gist (بديل عن الكتابة داخل المستودع) ----------------

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")
GIST_FILENAME = "alerted_state.json"
POSITIONS_GIST_FILE = "open_positions.json"   # الصفقات المفتوحة قيد المتابعة (نفس الـ Gist، ملف منفصل)
CLOSED_GIST_FILE = "closed_trades.json"       # السجل "النشط": أحدث الصفقات فقط (قراءة سريعة، دائمًا صغير وآمن)
STATS_GIST_FILE = "stats.json"                # إحصائيات أداء محسوبة دوريًا من closed_trades (خيار 3: تتبع فقط)
ACTIVE_HISTORY_SIZE = 150                     # عدد الصفقات المحفوظة في السجل النشط قبل ترحيل الأقدم للأرشيف
ARCHIVE_PREFIX = "closed_trades_archive_"     # بادئة ملفات الأرشيف المرقّمة داخل كل Gist أرشيف
ARCHIVE_CHUNK_SIZE = 150                      # حد أقصى للصفقات في كل ملف أرشيف (يبقيه دائمًا تحت حد GitHub ~1MB بأمان)
ARCHIVE_CHAIN_FILE = "archive_gists_chain.json"   # بالـGist الرئيسي: قائمة بمعرّفات كل Gists الأرشيف عبر الوقت (الأخير = النشط للكتابة)
MAX_ARCHIVE_FILES_BEFORE_ROTATE = 280         # هامش أمان قبل حد GitHub الفعلي (300 ملف/Gist) — عنده نفتح Gist أرشيف جديد
ARCHIVE_INDEX_FILE = "archive_index.json"     # فهرس صغير بالGist النشط: أي Gist يغطي أي نطاق تاريخ (لتسريع قراءة الإحصائيات لاحقًا)
DOM_SHIFT_THRESHOLD = float(os.environ.get("DOM_SHIFT_THRESHOLD", "0.3"))  # نقطة مئوية خلال دورة تشغيل واحدة


def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


class GistFetchError(Exception):
    """يُرفع عند فشل فعلي (شبكة/انقطاع/rate limit/...) بجلب ملفات الـ Gist.
    مقصود بها التمييز الصريح بين 'فشل الجلب مؤقتًا' و'الملف فارغ فعلاً' —
    بدون هذا التمييز قد يظن البوت أن لا صفقات مفتوحة أصلاً ويحفظ حالة فارغة/ناقصة
    فوق الحالة الحقيقية بالـGist (فقدان صامت للبيانات)."""
    pass


def _gist_get_all_files():
    """يقرأ قاموس كل ملفات الـ Gist دفعة واحدة (اسم -> بيانات الملف بما فيها content).
    يُستدعى مرة واحدة فقط في بداية main() والنتيجة تُمرَّر لبقية الدوال، بدل ما تجلب
    كل دالة (load_state/load_positions/load_closed/save_all_state) نسختها الخاصة —
    هذا يقلل عدد طلبات GitHub API وأي فرصة لفشل جزئي بمنتصف الرن.
    يرفع GistFetchError عند أي خطأ شبكي/HTTP فعلي (بدل إرجاع {} صامتًا)."""
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
    """يقرأ محتوى ملف واحد من قاموس ملفات مُجلب مسبقًا (بدون أي طلب شبكة جديد).
    يرجع None لو الملف غير موجود فعليًا ضمن الملفات المجلوبة بنجاح."""
    if filename not in gist_files:
        return None
    return gist_files[filename]["content"]


def _gist_get_all_files_for(gist_id):
    """مثل _gist_get_all_files لكن لأي معرف Gist — يُستخدم لقراءة ملفات Gist الأرشيف
    النشط لو كان مختلفًا عن الـGist الرئيسي (GIST_ID)."""
    if not GIST_TOKEN or not gist_id:
        return {}
    try:
        r = requests.get(f"https://api.github.com/gists/{gist_id}", headers=_gist_headers(), timeout=15)
        r.raise_for_status()
        return r.json().get("files", {})
    except Exception as e:
        raise GistFetchError(f"تعذّر قراءة ملفات Gist الأرشيف {gist_id}: {e}") from e


def _create_new_gist(description="Market Scanner - أرشيف صفقات إضافي"):
    """ينشئ Gist خاص جديد (يبدأ بملف README بسيط) ويرجع معرفه — يُستخدم عند تدوير الأرشيف."""
    payload = {
        "description": description,
        "public": False,
        "files": {"README.md": {"content": "أرشيف صفقات مغلقة لبوت مسح السوق — يُدار تلقائيًا، لا تعدّل يدويًا."}},
    }
    r = requests.post("https://api.github.com/gists", headers=_gist_headers(), json=payload, timeout=20)
    r.raise_for_status()
    return r.json()["id"]


def get_active_archive_gist(main_gist_files):
    """
    يرجع (معرف Gist الأرشيف النشط حاليًا للكتابة، قائمة سلسلة الـGists، ملفاته الحالية).
    ينشئ Gist أرشيف جديد تلقائيًا لما عدد ملفات الأرشيف بالنشط الحالي يقارب حد GitHub
    الفعلي (300 ملف/Gist) — هذا يمنع فشل الحفظ الصامت بعد تراكم عدد كبير جدًا من
    الصفقات المغلقة على المدى الطويل (كل Gist أرشيف قديم يبقى مقروءًا للأبد، فقط
    التوسع الجديد ينتقل لـGist لاحق).
    """
    try:
        chain_raw = _gist_get_file(ARCHIVE_CHAIN_FILE, main_gist_files)
        chain = json.loads(chain_raw) if chain_raw else []
    except Exception:
        chain = []

    if not chain:
        new_id = _create_new_gist()
        return new_id, [new_id], {}

    active_id = chain[-1]
    active_files = main_gist_files if active_id == GIST_ID else _gist_get_all_files_for(active_id)

    archive_count = sum(1 for fn in active_files if fn.startswith(ARCHIVE_PREFIX))
    if archive_count >= MAX_ARCHIVE_FILES_BEFORE_ROTATE:
        new_id = _create_new_gist()
        chain.append(new_id)
        return new_id, chain, {}

    return active_id, chain, active_files


def archive_overflow(overflow_trades, main_gist_files):
    """
    يوزّع الصفقات القديمة الفائضة (التي خرجت من السجل النشط) على ملفات أرشيف مرقّمة
    (closed_trades_archive_0001.json, 0002.json, ...) داخل Gist أرشيف نشط، كل ملف
    محدود بـARCHIVE_CHUNK_SIZE صفقة كحد أقصى (يبقيه تحت حد GitHub ~1MB بأمان). لما
    عدد الملفات بالGist النشط يقارب 280، يُفتح Gist أرشيف جديد تلقائيًا (get_active_archive_gist)
    بدل تجاوز حد GitHub الفعلي (300 ملف/Gist) الذي يوقف الحفظ نهائيًا.

    يرجع: (معرف Gist الأرشيف المستهدف, قاموس الملفات المطلوب كتابتها فيه,
           قاموس ملفات إضافية للـGist الرئيسي [سلسلة الـGists + الفهرس] إن تغيّرت).
    """
    main_updates = {}
    if not overflow_trades:
        return None, {}, main_updates

    target_gist_id, chain, archive_files = get_active_archive_gist(main_gist_files)

    # لو الـGist تغيّر أو أُنشئ لأول مرة، حدّث سلسلة الـGists بالGist الرئيسي
    try:
        old_chain_raw = _gist_get_file(ARCHIVE_CHAIN_FILE, main_gist_files)
        old_chain = json.loads(old_chain_raw) if old_chain_raw else []
    except Exception:
        old_chain = []
    if chain != old_chain:
        main_updates[ARCHIVE_CHAIN_FILE] = json.dumps(chain, ensure_ascii=False)

    archive_names = sorted(fn for fn in archive_files if fn.startswith(ARCHIVE_PREFIX))
    if archive_names:
        last_name = archive_names[-1]
        idx = int(last_name[len(ARCHIVE_PREFIX):].replace(".json", ""))
        try:
            last_content = json.loads(archive_files[last_name].get("content") or "[]")
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
        files_to_write[f"{ARCHIVE_PREFIX}{idx:04d}.json"] = json.dumps(last_content, ensure_ascii=False, separators=(',', ':'))

    # أنشئ ملفات أرشيف جديدة للباقي ضمن نفس الـGist النشط (حتى لو قارب الحد، نكمل
    # هذي الدورة ونؤجل التدوير الفعلي للدورة القادمة تفاديًا لتعقيد إضافي هنا)
    while remaining:
        idx += 1
        chunk = remaining[:ARCHIVE_CHUNK_SIZE]
        remaining = remaining[ARCHIVE_CHUNK_SIZE:]
        files_to_write[f"{ARCHIVE_PREFIX}{idx:04d}.json"] = json.dumps(chunk, ensure_ascii=False, separators=(',', ':'))

    # تحديث فهرس صغير بالGist الرئيسي: أي Gist يحتوي حاليًا آخر نطاق أرشيف مكتوب
    try:
        index_raw = _gist_get_file(ARCHIVE_INDEX_FILE, main_gist_files)
        index = json.loads(index_raw) if index_raw else {}
    except Exception:
        index = {}
    index[target_gist_id] = {
        "last_updated": __import__("datetime").datetime.utcnow().isoformat(),
        "archive_files_count": len(archive_names) + len(files_to_write) - (1 if space > 0 and overflow_trades else 0),
    }
    main_updates[ARCHIVE_INDEX_FILE] = json.dumps(index, ensure_ascii=False)

    return target_gist_id, files_to_write, main_updates


def _gist_patch_files(files_dict, gist_id=None):
    """يحفظ عدة ملفات دفعة واحدة داخل Gist معيّن (افتراضيًا الـGist الرئيسي GIST_ID،
    أو أي Gist أرشيف آخر لو مُرِّر gist_id صراحة — يُستخدم عند تدوير الأرشيف).
    الملفات غير المذكورة تبقى كما هي. يُعيد المحاولة تلقائيًا عند الفشل، ويطبع حجم
    Payload للتشخيص."""
    target_id = gist_id or GIST_ID
    if not GIST_TOKEN or not target_id:
        print("⚠️ GIST_TOKEN أو معرف الـGist غير موجودين — تخطي الحفظ.")
        return False

    payload = {"files": {fn: {"content": content} for fn, content in files_dict.items()}}
    payload_size = len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))
    print(f"💾 حجم Payload للحفظ في Gist {target_id}: {payload_size:,} بايت | ملفات: {list(files_dict.keys())}")

    last_err = None
    for attempt in range(1, 4):
        try:
            r = requests.patch(
                f"https://api.github.com/gists/{target_id}",
                headers=_gist_headers(),
                json=payload,
                timeout=20
            )
            if r.ok:
                print(f"✅ تم الحفظ في Gist بنجاح (محاولة {attempt})")
                return True
            # rate limit — انتظر أطول
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
    """يحمّل ذاكرة الإشارات المرسلة وآخر قيمة BTC Dominance من قاموس ملفات مُجلب مسبقًا."""
    content = _gist_get_file(GIST_FILENAME, gist_files)
    if not content:
        return set(), None
    try:
        data = json.loads(content)
        return set(data.get("alerted", [])), data.get("btc_dominance_prev")
    except Exception as e:
        print(f"تعذّر تحليل حالة Gist ({e}) — سيبدأ البوت بذاكرة فارغة.")
        send_admin_alert(
            f"تعذّر تحليل ذاكرة الإشارات المرسلة ({GIST_FILENAME}) — سيعمل البوت هذه "
            f"التشغيلة بذاكرة فارغة، وقد يعيد إرسال تنبيهات لإشارات سبق إرسالها.\n"
            f"التفاصيل: {e}"
        )
        return set(), None


class PositionsCorruptedError(Exception):
    """يُرفع عند فشل تحليل JSON الخاص بالصفقات المفتوحة من Gist. بدل إرجاع [] بصمت (وهو ما
    كان يعني عمليًا 'لا صفقات مفتوحة' ثم يُكتب لاحقًا فوق الصفقات الحقيقية في save_all_state),
    نوقف التشغيلة كاملة احترازيًا — تمامًا كمنطق GistFetchError."""
    pass


def load_positions(gist_files):
    """يحمّل الصفقات المفتوحة قيد المتابعة من قاموس ملفات مُجلب مسبقًا.
    يرفع PositionsCorruptedError عند فشل التحليل بدل إرجاع [] بصمت، لأن ذلك قد يؤدي
    لاحقًا لكتابة حالة فارغة فوق صفقات مفتوحة حقيقية (فقدان تتبعها نهائيًا)."""
    content = _gist_get_file(POSITIONS_GIST_FILE, gist_files)
    if not content:
        return []
    try:
        return json.loads(content)
    except Exception as e:
        raise PositionsCorruptedError(f"تعذّر تحليل open_positions من Gist: {e}") from e


def load_closed(gist_files):
    content = _gist_get_file(CLOSED_GIST_FILE, gist_files)
    if not content:
        return []
    try:
        return json.loads(content)
    except Exception:
        return []


def _score_bucket(score):
    """يصنّف الدرجة إلى نطاق مطابق لعتبات القرار الفعلية بالبوت (بدل تقريب عدد صحيح بسيط)."""
    if score is None:
        return "?"
    s = abs(score)
    if s >= 3.5:
        return "3.5+"
    if s >= 2.5:
        return "2.5-3.49"
    if s >= 1.5:
        return "1.5-2.49"
    return "<1.5"


def _new_bucket():
    return {"total": 0, "win": 0, "loss": 0, "neutral": 0, "pnl_sum": 0.0, "pnl_list": []}


def _bump_bucket(bucket, outcome, net_pnl_pct):
    bucket["total"] += 1
    bucket[outcome] += 1
    if net_pnl_pct is not None:
        bucket["pnl_sum"] += net_pnl_pct
        bucket["pnl_list"].append(net_pnl_pct)


def _finalize_bucket(bucket):
    """يحوّل bucket الخام (مجاميع) لملخص جاهز للعرض: نسبة نجاح + متوسط صافي، بدون قائمة pnl الخام."""
    total = bucket["total"]
    pnl_list = bucket.pop("pnl_list")
    bucket["win_rate_pct"] = round(bucket["win"] / total * 100, 1) if total else None
    bucket["avg_pnl_pct"] = round(bucket["pnl_sum"] / len(pnl_list), 2) if pnl_list else None
    bucket.pop("pnl_sum")
    return bucket


def compute_stats(history):
    """
    يحسب إحصائيات أداء من سجل الصفقات المغلقة (خيار 3: تتبع فقط، بدون أي تعديل تلقائي
    على منطق الفحص/الدخول/الأوزان). لا يُستخدم الناتج هنا لتغيير أي قرار في البوت —
    فقط للعرض والمراقبة اليدوية.

    تحديث 2026-09-07: تصنيف الفوز/الخسارة أصبح مبنيًا على الربح/الخسارة الفعلي النهائي
    (net_pnl_pct بعد خصم عمولة تقديرية) بدل الاعتماد على "هل لمست أي TP" — صفقة لمست
    TP1 ثم رجعت وأغلقت قريبًا من الصفر لم تعد تُحسب "فوز" مضلِّل. أُضيفت أيضًا: Profit
    Factor, Expectancy, Average R, Max Drawdown (تقريبي)، نسبة تحقق كل TP على حدة،
    وتحليل حسب كل عامل تشخيصي (squeeze/accumulation/divergence/extended/obv_confirm/
    htf_aligned/di_confirm/momentum_agree/vol_confirm/breakout و experimental details).
    """
    if not history:
        return None

    total = len(history)
    pnl_list, durations, r_list = [], [], []
    wins = losses = neutral = 0
    by_type, by_score, by_reason, by_factor = {}, {}, {}, {}
    tp_hit = {}   # "tp1".."tp4" -> {"hit": n, "total": n}
    equity_curve_points = []  # (closed_at, net_pnl_pct) لحساب Max Drawdown التقريبي

    FACTOR_KEYS = [
        "squeeze", "accumulation", "divergence", "extended", "obv_confirm",
        "htf_aligned", "di_confirm", "momentum_agree", "vol_confirm", "ranging",
    ]

    for t in history:
        reason = t.get("closed_reason", "UNKNOWN")
        by_reason[reason] = by_reason.get(reason, 0) + 1

        entry, exit_price = t.get("entry"), t.get("exit_price")
        net_pnl_pct = None
        if entry and exit_price:
            raw_pct = (exit_price - entry) / entry * 100
            net_pnl_pct = raw_pct - TRADING_FEE_PCT
            pnl_list.append(net_pnl_pct)
            equity_curve_points.append((t.get("closed_at", ""), net_pnl_pct))

        # تصنيف الفوز/الخسارة الآن على أساس الربح/الخسارة الصافي الفعلي، لا "هل لمست TP"
        if net_pnl_pct is None:
            outcome = "neutral"
        elif net_pnl_pct > BREAKEVEN_BAND_PCT:
            outcome = "win"
        elif net_pnl_pct < -BREAKEVEN_BAND_PCT:
            outcome = "loss"
        else:
            outcome = "neutral"
        if outcome == "win":
            wins += 1
        elif outcome == "loss":
            losses += 1
        else:
            neutral += 1

        # Average R: الربح/الخسارة الصافي كمضاعف من المخاطرة الأصلية للصفقة (لو معروفة)
        initial_risk = t.get("initial_risk")
        sl = t.get("sl")
        if not initial_risk and entry and sl:
            initial_risk = entry - sl  # توافقية مع صفقات قديمة بلا حقل initial_risk
        if initial_risk and entry and net_pnl_pct is not None:
            risk_pct = initial_risk / entry * 100
            if risk_pct > 0:
                r_list.append(net_pnl_pct / risk_pct)

        try:
            t0 = dt.datetime.strptime(t["opened_at"], "%Y-%m-%d %H:%M:%S")
            t1 = dt.datetime.strptime(t["closed_at"], "%Y-%m-%d %H:%M:%S")
            durations.append((t1 - t0).total_seconds() / 3600)
        except Exception:
            pass

        ttype = t.get("type", "official")
        b1 = by_type.setdefault(ttype, _new_bucket())
        _bump_bucket(b1, outcome, net_pnl_pct)

        score_key = _score_bucket(t.get("score"))
        b2 = by_score.setdefault(score_key, _new_bucket())
        _bump_bucket(b2, outcome, net_pnl_pct)

        # نسبة تحقق كل TP على حدة: المقام = عدد الصفقات التي كان لديها هذا الهدف أصلاً
        tps_defined = t.get("tps") or []
        hit_tps = set(t.get("hit_tps") or [])
        for idx in range(len(tps_defined)):
            key = f"tp{idx + 1}"
            entry_bucket = tp_hit.setdefault(key, {"hit": 0, "total": 0})
            entry_bucket["total"] += 1
            if idx in hit_tps:
                entry_bucket["hit"] += 1

        # تحليل حسب كل عامل تشخيصي بولياني (True/False فقط -- None يُستبعد من هذا العامل)
        for fkey in FACTOR_KEYS:
            fval = t.get(fkey)
            if fval is None:
                continue
            fbucket = by_factor.setdefault(fkey, {"true": _new_bucket(), "false": _new_bucket()})
            _bump_bucket(fbucket["true" if fval else "false"], outcome, net_pnl_pct)

        # تفاصيل جودة الانفجار/التجريبية (dict من مفاتيح بولية) -- تُعامل كعوامل إضافية
        for details_field, prefix in (("breakout_details", "breakout"), ("experimental_details", "experimental")):
            details = t.get(details_field) or {}
            for dkey, dval in details.items():
                fkey = f"{prefix}_{dkey}"
                fbucket = by_factor.setdefault(fkey, {"true": _new_bucket(), "false": _new_bucket()})
                _bump_bucket(fbucket["true" if dval else "false"], outcome, net_pnl_pct)

    # Profit Factor + Expectancy (مبنيان على نفس قائمة pnl الصافية)
    gains = sum(p for p in pnl_list if p > 0)
    losses_sum = sum(-p for p in pnl_list if p < 0)
    profit_factor = round(gains / losses_sum, 2) if losses_sum > 0 else (None if gains == 0 else float("inf"))
    expectancy_pct = round(sum(pnl_list) / len(pnl_list), 2) if pnl_list else None
    avg_r = round(sum(r_list) / len(r_list), 2) if r_list else None

    # Max Drawdown تقريبي: منحنى تراكمي مبسّط بافتراض حجم مركز متساوٍ لكل صفقة، مرتّب
    # زمنيًا حسب وقت الإغلاق -- تقريب توضيحي وليس محاكاة رأس مال حقيقية (trade_simulator.py
    # هو الأداة الأدق لذلك، فهو يحاكي رأس مال فعلي وشرائح متزامنة)
    equity_curve_points.sort(key=lambda x: x[0])
    running, peak, max_dd = 0.0, 0.0, 0.0
    for _, pnl in equity_curve_points:
        running += pnl
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)

    by_type = {k: _finalize_bucket(v) for k, v in by_type.items()}
    by_score = {k: _finalize_bucket(v) for k, v in by_score.items()}
    by_factor = {
        k: {"true": _finalize_bucket(v["true"]), "false": _finalize_bucket(v["false"])}
        for k, v in by_factor.items()
    }
    tp_hit_rates = {
        k: {**v, "hit_rate_pct": round(v["hit"] / v["total"] * 100, 1) if v["total"] else None}
        for k, v in tp_hit.items()
    }

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "neutral": neutral,
        "win_rate_pct": round(wins / total * 100, 1),
        "avg_pnl_pct": round(sum(pnl_list) / len(pnl_list), 2) if pnl_list else None,
        "avg_duration_hours": round(sum(durations) / len(durations), 1) if durations else None,
        # --- مقاييس جديدة (2026-09-07) ---
        "profit_factor": profit_factor,
        "expectancy_pct": expectancy_pct,
        "avg_r": avg_r,
        "max_drawdown_pct_approx": round(max_dd, 2),
        "tp_hit_rates": tp_hit_rates,
        "by_factor": by_factor,
        # --- كما كانت (بنفس أسماء المفاتيح، لكن outcome الآن مبني على net pnl فعلي) ---
        "by_type": by_type,
        "by_score": by_score,
        "by_reason": by_reason,
        "computed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_all_state(alerted_symbols, btc_dominance, positions, closed_delta, gist_files):
    """
    يحفظ في نفس الطلب: حالة التنبيهات + BTC Dominance + الصفقات المفتوحة،
    ويُلحق أي صفقات أُغلقت هذا التشغيل بسجل closed_trades (مع ترحيل الفائض للأرشيف
    بدل حذفه). كما يحسب إحصائيات أداء (stats.json) من السجل المحدَّث — تتبع فقط،
    بدون أي تأثير على منطق الفحص أو الدخول. يرجع الإحصائيات (أو None) للاستخدام
    الاختياري في إرسال تقرير دوري.

    gist_files: نفس القاموس المُجلب مرة واحدة في بداية main() — بلا أي إعادة جلب هنا،
    كي لا يتعرض الحفظ لفشل شبكي مستقل في آخر لحظة.
    """
    files = {
        GIST_FILENAME: json.dumps(
            {"alerted": sorted(alerted_symbols), "btc_dominance_prev": btc_dominance},
            ensure_ascii=False, separators=(',', ':')
        ),
    }

    # لا نحفظ positions إذا لم تتغير — يقلل حجم الطلب وعدد مرات الكتابة على Gist
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

        # إذا تجاوز السجل النشط الحد، تُرحَّل أقدم الصفقات لملفات الأرشيف بدل حذفها نهائيًا —
        # لا يُفقد أي شيء، والسجل النشط يبقى دائمًا صغيرًا وسريع القراءة
        if len(history) > ACTIVE_HISTORY_SIZE:
            overflow = history[:-ACTIVE_HISTORY_SIZE]
            history = history[-ACTIVE_HISTORY_SIZE:]
            archive_gist_id, archive_files, main_updates = archive_overflow(overflow, gist_files)
            files.update(main_updates)  # سلسلة الـGists + الفهرس، تُحفظ بالGist الرئيسي
            if archive_gist_id and archive_files:
                if archive_gist_id == GIST_ID:
                    files.update(archive_files)  # نفس الـGist الرئيسي، تُدمج بحفظة واحدة
                else:
                    archived_ok = _gist_patch_files(archive_files, gist_id=archive_gist_id)
                    if not archived_ok:
                        print(f"❌ فشل حفظ ملفات الأرشيف بـGist منفصل ({archive_gist_id})")

        files[CLOSED_GIST_FILE] = json.dumps(history, ensure_ascii=False, separators=(',', ':'))

        # ملاحظة: الإحصائيات الآنية تُحسب من السجل النشط فقط (آخر ACTIVE_HISTORY_SIZE صفقة)
        # للتقرير الفوري — التحليل الشامل الكامل يحتاج قراءة السجل النشط + كل ملفات الأرشيف
        stats = compute_stats(history)
        if stats:
            files[STATS_GIST_FILE] = json.dumps(stats, ensure_ascii=False, separators=(',', ':'))

    saved_ok = _gist_patch_files(files)
    if not saved_ok:
        print("❌ لم يُحفظ شيء في Gist — الصفقات المفتوحة والسجل المغلق غير محفوظين!")
        send_admin_alert(
            "فشل الحفظ في Gist بعد كل المحاولات — الصفقات المفتوحة والسجل المغلق "
            "لهذه التشغيلة لم يُحفظا. قد تتكرر تنبيهات لصفقات سبق إرسالها، أو تُفقد "
            "متابعة صفقات مفتوحة."
        )
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
            "monitor_interval": MONITOR_INTERVAL,
            "initial_risk": r["entry"] - r["sl"],  # المخاطرة الأصلية (وحدة سعر) -- تُستخدم لاحقًا بحساب Breakeven/R
            "type": "official",
            "concurrent_signals": r.get("concurrent_signals", []),
            # حقول تشخيصية: أي عوامل كانت حاضرة وقت الدخول -> تحليل لاحق لأثر كل عامل على النجاح/الفشل
            # (تُبقيها trade_stats.py قابلة للتصنيف حسب المؤشر بعد الإغلاق)
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
            "monitor_interval": MONITOR_INTERVAL,
            "initial_risk": r["early_entry"] - r["early_sl"],
            "type": "early",
            "concurrent_signals": r.get("concurrent_signals", []),
            "confidence": r.get("early_confidence"),
            # نفس الحقول التشخيصية للإشارات المبكرة، عشان نعرف أي مزيج (squeeze/accumulation/divergence)
            # فرّق فعليًا بين "احتمالية" ناجحة و"مؤكدة" فاشلة، بدل ما نكتفي بتصنيف الثقة العام
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
            "monitor_interval": MONITOR_INTERVAL,
            "initial_risk": r["breakout_entry"] - r["breakout_sl"],
            "type": "breakout",
            "concurrent_signals": r.get("concurrent_signals", []),
            "breakout_details": r.get("breakout_details"),
            "extended": r.get("extended"),
            "alert_message_id": r.get("_msg_id"),
            "alert_text": r.get("_alert_text"),
        })


def open_new_experimental_positions(positions, fresh_experimental_signals):
    """
    يفتح متابعة تلقائية (TP/SL) لإشارات التجريبية بنفس آلية الرسمية/المبكرة/الانفجار،
    بحقل type="experimental" يُستخدم لاحقًا لتمييز رسائل النتيجة.
    """
    for r in fresh_experimental_signals:
        if r.get("experimental_entry") is None:
            continue
        positions.append({
            "symbol": r["symbol"],
            "entry": r["experimental_entry"],
            "sl": r["experimental_sl"],
            "tps": r["experimental_tps"],
            "hit_tps": [],
            "tp_notify_ids": [None] * len(r["experimental_tps"]),
            "score": r["experimental_score"],
            "trend_up": r["trend_up"],
            "interval": INTERVAL,
            "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "monitor_interval": MONITOR_INTERVAL,
            "initial_risk": r["experimental_entry"] - r["experimental_sl"],
            "type": "experimental",
            "concurrent_signals": r.get("concurrent_signals", []),
            "experimental_details": r.get("experimental_details"),
            "near_resistance": r.get("near_resistance"),
            "extended": r.get("extended"),
            "alert_message_id": r.get("_msg_id"),
            "alert_text": r.get("_alert_text"),
        })


TIME_STOP_HOURS = float(os.environ.get("TIME_STOP_HOURS", "96"))  # سقف زمني أقصى (شبكة أمان) قبل اعتبار الصفقة منتهية الصلاحية — افتراضيًا 4 أيام
# شموع مراقبة أدق من شمعة الإشارة: نستخدم High/Low للشموع المغلقة لتفادي فقدان TP/SL
# بين تشغيلتين متباعدتين. عند تعارض TP وSL داخل الشمعة نفسها نعتمد SL أولًا بشكل محافظ
# لأن OHLC لا يخبرنا بترتيب الحركة داخل الشمعة.
MONITOR_INTERVAL = os.environ.get("MONITOR_INTERVAL", "5m")
MONITOR_KLINE_LIMIT = int(os.environ.get("MONITOR_KLINE_LIMIT", "1000"))
MONITOR_MAX_PAGES = int(os.environ.get("MONITOR_MAX_PAGES", "3"))


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
    type_labels = {"early": "❌ (إشارة مبكرة) ", "breakout": "❌ (إشارة انفجار) ", "experimental": "❌ (إشارة تجريبية) "}
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
    type_labels = {"early": "✅ (إشارة مبكرة) ", "breakout": "✅ (إشارة انفجار) ", "experimental": "✅ (إشارة تجريبية) "}
    header = type_labels.get(pos.get("type"), "✅ ")
    tp_label = f"TP{tp_index + 1}"
    return (
        f"{header}{pos['symbol'].replace('USDT', '/USDT')}\n"
        f"سعر الدخول: {entry:.6g}\n"
        f"{tp_label}: {tp:.6g}\n"
        f"نسبة الصعود: +{pct_gain:.2f}%\n"
        f"المدة الزمنية لتحقيق الهدف: {duration}"
    )


def _parse_opened_epoch_ms(opened_at_str):
    try:
        t = time.strptime(opened_at_str, "%Y-%m-%d %H:%M:%S")
        return int(time.mktime(t) * 1000)
    except Exception:
        return None


def fetch_monitor_candles(symbol, start_ms, end_ms=None):
    """يجلب شموع المراقبة المغلقة منذ فتح الصفقة، مع pagination عند الحاجة.

    الهدف هو عدم الاعتماد على السعر الحالي فقط: إذا وصل السعر إلى TP/SL ثم عاد
    قبل تشغيل السكربت التالي، نستطيع اكتشاف الحدث من High/Low للشموع السابقة.
    """
    if start_ms is None:
        return []
    if end_ms is None:
        end_ms = int(time.time() * 1000)

    all_klines = []
    cursor_end = end_ms
    for _ in range(max(1, MONITOR_MAX_PAGES)):
        r = _request_with_retry(
            f"{BASE_URL}/klines",
            params={
                "symbol": symbol,
                "interval": MONITOR_INTERVAL,
                "limit": MONITOR_KLINE_LIMIT,
                "endTime": cursor_end,
            },
        )
        batch = r.json() or []
        if not batch:
            break
        # Binance تعيد الأقدم -> الأحدث داخل الدفعة.
        all_klines = batch + all_klines
        oldest_open = int(batch[0][0])
        if oldest_open <= start_ms or len(batch) < MONITOR_KLINE_LIMIT:
            break
        cursor_end = oldest_open - 1

    # فقط الشموع التي بدأت بعد لحظة فتح الصفقة، والشموع المغلقة فعليًا.
    now_ms = int(time.time() * 1000)
    out = []
    seen = set()
    for k in sorted(all_klines, key=lambda x: int(x[0])):
        open_ms, close_ms = int(k[0]), int(k[6])
        if open_ms < start_ms or close_ms > now_ms:
            continue
        if open_ms in seen:
            continue
        seen.add(open_ms)
        out.append(k)
    return out


def _monitor_candle_map(positions):
    """يجلب شموع المراقبة مرة واحدة لكل رمز، لا مرة لكل صفقة."""
    grouped = {}
    for pos in positions:
        opened_ms = _parse_opened_epoch_ms(pos.get("opened_at"))
        if opened_ms is None:
            continue
        cursor_ms = pos.get("monitor_cursor_ms")
        try:
            start_ms = max(opened_ms, int(cursor_ms) + 1) if cursor_ms is not None else opened_ms
        except (TypeError, ValueError):
            start_ms = opened_ms
        sym = pos.get("symbol")
        if not sym:
            continue
        if sym not in grouped or start_ms < grouped[sym]:
            grouped[sym] = start_ms

    result = {}
    for sym, start_ms in grouped.items():
        try:
            result[sym] = fetch_monitor_candles(sym, start_ms)
        except Exception as e:
            print(f"⚠️ تعذر جلب شموع المراقبة لـ {sym}: {e}")
            result[sym] = []
    return result


def check_open_positions(positions, price_map, monitor_map=None):
    """يتابع TP/SL باستخدام High/Low للشموع المغلقة بدل السعر الحالي فقط.

    القاعدة المحافظة للشمعة الواحدة:
    - إذا لامس High الهدف ولامس Low وقف الخسارة في الشمعة نفسها قبل حسم الترتيب،
      نعتبر SL هو الحدث الأول؛ هذا يمنع منح نتائج متفائلة من بيانات OHLC التي لا
      تحتوي على ترتيب الحركة داخل الشمعة.
    - بعد تحقق TP1 ينتقل SL إلى Breakeven + buffer، ويُطبّق هذا الوقف من الشمعة التالية
      لأن ترتيب الحركة داخل شمعة TP1 غير معروف.
    """
    still_open, closed_now = [], []
    monitor_map = monitor_map or {}

    for pos in positions:
        symbol = pos["symbol"]
        price = price_map.get(symbol)
        candles = monitor_map.get(symbol) or []

        # آخر إغلاق متاح من شموع المراقبة أفضل كـ fallback إذا فشل ticker24h.
        if price is None and candles:
            try:
                price = float(candles[-1][4])
            except Exception:
                price = None
        if price is None:
            still_open.append(pos)
            continue

        silent = pos.get("alert_message_id") is None and pos.get("type") == "early"
        hit_set = set(pos.get("hit_tps") or [])
        pos.setdefault("hit_tps", [])
        pos.setdefault("tp_notify_ids", [None] * len(pos.get("tps") or []))

        # نراقب الشموع منذ آخر حالة محفوظة قدر الإمكان. الصفقات القديمة جدًا قد
        # تتجاوز عدد صفحات Binance المتاح؛ عندها نستخدم السعر الحالي فقط كشبكة أمان.
        events = []
        opened_ms = _parse_opened_epoch_ms(pos.get("opened_at")) or 0
        try:
            cursor_ms = int(pos.get("monitor_cursor_ms", opened_ms))
        except (TypeError, ValueError):
            cursor_ms = opened_ms
        for k in candles:
            try:
                candle_open_ms = int(k[0])
                if candle_open_ms <= cursor_ms:
                    continue
                events.append((candle_open_ms, float(k[2]), float(k[3]), float(k[4])))
            except Exception:
                continue

        closed = False
        for candle_open_ms, high, low, candle_close in events:
            # 1) الوقف الحالي أولًا. إذا اجتمع SL وTP في نفس الشمعة نأخذ SL أولًا.
            if low <= pos["sl"]:
                exit_price = pos["sl"]
                result_text = format_sl_hit(pos, exit_price)
                if not silent:
                    send_telegram(result_text)
                edit_telegram_strike(pos.get("alert_message_id"), build_progress_text(pos), result_text)
                pos["closed_reason"] = "SL"
                pos["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(candle_open_ms / 1000))
                pos["exit_price"] = exit_price
                pos["monitor_exit_interval"] = MONITOR_INTERVAL
                pos["monitor_exit_candle_open_ms"] = candle_open_ms
                closed_now.append(pos)
                closed = True
                break

            # 2) الأهداف التي لامسها High. إذا تحقق أكثر من هدف في نفس الشمعة
            # نسجلها كلها، لأننا نعلم أن High وصل إليها بالترتيب العددي.
            newly_hit = [i for i, tp in enumerate(pos["tps"]) if i not in hit_set and high >= tp]
            if newly_hit:
                for i in newly_hit:
                    tp_price = pos["tps"][i]
                    tp_text = format_tp_hit(pos, i, tp_price)
                    msg_id = None if silent else send_telegram(tp_text)
                    if i >= len(pos["tp_notify_ids"]):
                        pos["tp_notify_ids"].extend([None] * (i + 1 - len(pos["tp_notify_ids"])))
                    pos["tp_notify_ids"][i] = msg_id
                    if not silent:
                        time.sleep(1)

                    prev_index = i - 1
                    if prev_index >= 0 and pos["tp_notify_ids"][prev_index]:
                        delete_telegram_message(pos["tp_notify_ids"][prev_index])
                        pos["tp_notify_ids"][prev_index] = None

                    pos["hit_tps"].append(i)
                    hit_set.add(i)
                    hit_sorted = sorted(pos["hit_tps"])
                    lines = [format_tp_line(pos, j) for j in hit_sorted]
                    edit_telegram_append(pos.get("alert_message_id"), pos.get("alert_text", ""), lines)

                if 0 in hit_set and pos["sl"] < pos["entry"]:
                    buffer = pos.get("initial_risk", 0) * BREAKEVEN_BUFFER_RATIO
                    pos["sl"] = pos["entry"] + max(buffer, 0)
                    pos["breakeven_activated"] = True
                    pos["breakeven_activated_at"] = time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(candle_open_ms / 1000)
                    )

            if len(hit_set) >= len(pos["tps"]):
                all_tp_text = "🏁 تحققت جميع الأهداف"
                final_tp = pos["tps"][-1] if pos.get("tps") else candle_close
                edit_telegram_strike(pos.get("alert_message_id"), build_progress_text(pos), all_tp_text)
                pos["closed_reason"] = "ALL_TP"
                pos["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(candle_open_ms / 1000))
                pos["exit_price"] = final_tp
                pos["monitor_exit_interval"] = MONITOR_INTERVAL
                pos["monitor_exit_candle_open_ms"] = candle_open_ms
                closed_now.append(pos)
                closed = True
                break

            # لا نختبر SL الجديد في نفس شمعة TP1؛ ترتيب الحركة داخل الشمعة غير معروف.

        if closed:
            continue

        # نحفظ آخر شمعة تمت معالجتها حتى لا نعيد تفسير شموع تاريخية بعد تحريك SL
        # إلى Breakeven في تشغيل لاحق، وهو أمر مهم جدًا عند استخدام High/Low.
        if events:
            pos["monitor_cursor_ms"] = events[-1][0]
        pos["monitor_last_checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

        hours_open = _hours_since(pos["opened_at"])
        if hours_open >= TIME_STOP_HOURS:
            pct_change = (price - pos["entry"]) / pos["entry"] * 100
            status = "بربح" if pct_change > 0 else ("بخسارة" if pct_change < 0 else "بدون تغيير")
            expired_text = (
                f"⏱️ انتهت صلاحية المراقبة (سقف زمني) — متوقفة {status}\n{symbol.replace('USDT','/USDT')}\n"
                f"الدخول: {pos['entry']:.6g} | الحالي: {price:.6g} | مدة المراقبة: {hours_open:.0f}س\n"
                f"النسبة: {pct_change:+.2f}%"
            )
            if not silent:
                send_telegram(expired_text)
            edit_telegram_strike(pos.get("alert_message_id"), build_progress_text(pos), expired_text)
            pos["closed_reason"] = "EXPIRED"
            pos["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            pos["exit_price"] = price
            pos["monitor_exit_interval"] = MONITOR_INTERVAL
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

    # جلب كل ملفات الـ Gist مرة واحدة فقط هنا (بدل طلب منفصل لكل دالة لاحقًا) —
    # فشل فعلي بالجلب (شبكة/rate limit) يُوقف الرن احترازيًا بدل المتابعة بحالة فارغة/ناقصة
    # قد تُكتب لاحقًا فوق الحالة الحقيقية بالـGist (فقدان صامت للصفقات المفتوحة).
    try:
        gist_files = _gist_get_all_files()
    except GistFetchError as e:
        print(f"❌ فشل جلب ملفات Gist ({e}) — إيقاف هذه التشغيلة احترازيًا "
              f"بحالة فارغة/ناقصة. سيُعاد المحاولة تلقائيًا بالتشغيلة القادمة.")
        send_admin_alert(
            f"فشل جلب ملفات Gist — تم إيقاف هذه التشغيلة احترازيًا لتفادي الكتابة "
            f"فوق الصفقات المفتوحة بحالة فارغة.\nالتفاصيل: {e}"
        )
        return

    tickers = fetch_ticker24h()
    price_map = fetch_prices_map(tickers)

    # ── تشخيص: هل price_map يغطي كل الرموز المفتوحة؟ ──
    print(f"📊 price_map يحتوي على {len(price_map)} رمز")

    # قبل أي مسح جديد: تفقّد الصفقات المفتوحة سابقًا باستخدام شموع مراقبة 5m
    # (High/Low) حتى لا نفقد TP/SL الذي حدث بين تشغيلتين متباعدتين.
    try:
        open_positions = load_positions(gist_files)
    except PositionsCorruptedError as e:
        print(f"❌ ملف الصفقات المفتوحة تالف بالـGist ({e}) — إيقاف هذه التشغيلة احترازيًا "
              f"لتفادي الكتابة فوق الصفقات الحقيقية بحالة فارغة.")
        send_admin_alert(
            f"ملف الصفقات المفتوحة (open_positions) تالف بالـGist ولا يمكن تحليله — تم "
            f"إيقاف هذه التشغيلة احترازيًا كي لا يُكتب فوقه بحالة فارغة (فقدان نهائي "
            f"لتتبع الصفقات المفتوحة).\nالتفاصيل: {e}\nيلزم فحص محتوى ملف "
            f"{POSITIONS_GIST_FILE} يدويًا بالـGist."
        )
        return
    print(f"📋 الصفقات المفتوحة المحمّلة من Gist: {len(open_positions)}")
    if open_positions:
        missing = [p["symbol"] for p in open_positions if p["symbol"] not in price_map]
        if missing:
            print(f"⚠️ رموز مفقودة من price_map — سيُستخدم آخر إغلاق من شموع المراقبة إن توفر: {missing}")
            # نسبة كبيرة من الصفقات المفتوحة بدون سعر حالي تلمّح لعطل حقيقي بجلب الأسعار
            # (وليس مجرد رمز تم شطبه من المنصة) — تستحق تنبيهًا فوريًا بدل الاكتفاء باللوق
            if len(missing) / len(open_positions) >= 0.2:
                send_admin_alert(
                    f"{len(missing)} من أصل {len(open_positions)} صفقة مفتوحة بدون سعر "
                    f"حالي (price_map) مفقود — سيُستخدم مسار شموع المراقبة إن توفر، وإلا تتأجل المتابعة.\n"
                    f"أمثلة: {', '.join(missing[:10])}"
                )
    monitor_map = {}
    if open_positions:
        monitor_map = _monitor_candle_map(open_positions)
        covered = sum(1 for p in open_positions if monitor_map.get(p.get("symbol")))
        print(f"📈 شموع المراقبة ({MONITOR_INTERVAL}) متوفرة لـ {covered}/{len(open_positions)} صفقة")
    open_positions, closed_now = check_open_positions(open_positions, price_map, monitor_map)
    print(f"🔒 صفقات متبقية مفتوحة: {len(open_positions)} | أُغلقت الآن: {len(closed_now)}")

    results = run_scan(tickers)

    strong = [
        r for r in results
        if r["score"] >= 1.5 and r["vol_confirm"] and r["atr_pct"] >= 0.08 and r["persistent"]
        and not r["ranging"] and not r["near_resistance"]
        and meets_min_profit(r["entry"], r["tps"])
        and meets_min_rr(r["entry"], r["sl"], r["tps"])
    ]
    strong_symbols = {r["symbol"] for r in strong}

    # إشارات مبكرة (انضغاط تقلب / تراكم صامت) لعملات لم تصل بعد لإشارة شراء كاملة —
    # تُميَّز بمفتاح منفصل (":early") في ذاكرة التنبيهات كي لا تتعارض مع إشارات الشراء الرسمية.
    # تُصفّى هنا أيضًا بنفس شرط الحد الأدنى لنسبة الربح (MIN_PROFIT_PCT) قبل اعتبارها مؤهلة
    # أصلاً — وليس فقط عند الإرسال — كي لا تُسجَّل كـ"مُنبَّه عليها" في الذاكرة وتُحرَم من
    # الإرسال لاحقًا إن تحسّن ربحها المتوقع
    early_eligible = [
        r for r in results
        if r["score"] < 1.5 and (r["squeeze"] or r["accumulation"])
        and r.get("early_confidence") is not None
        and meets_min_profit(r["early_entry"], r["early_tps"])
        and meets_min_rr(r["early_entry"], r["early_sl"], r["early_tps"])
    ]
    early_keys = {f"{r['symbol']}:early" for r in early_eligible}

    # إشارات انفجار (breakout) — اختراق قمة سابقة مع تأكيد حجم، مستقلة عن الإشارة الرسمية،
    # تُستبعد العملات اللي أصلاً عندها إشارة رسمية جديدة تجنبًا للتكرار
    breakout_eligible = [
        r for r in results
        if r.get("breakout_entry") is not None
        and r["symbol"] not in strong_symbols
        and meets_min_profit(r["breakout_entry"], r["breakout_tps"])
        and meets_min_rr(r["breakout_entry"], r["breakout_sl"], r["breakout_tps"])
    ]
    breakout_keys = {f"{r['symbol']}:breakout" for r in breakout_eligible}

    # إشارات تجريبية (Ichimoku Tenkan/Kijun + حجم/OBV + MFI) — مستقلة، تُستبعد العملات
    # اللي أصلاً عندها إشارة رسمية جديدة تجنبًا للتكرار
    experimental_eligible = [
        r for r in results
        if r.get("experimental_entry") is not None
        and r["symbol"] not in strong_symbols
        and meets_min_profit(r["experimental_entry"], r["experimental_tps"])
        and meets_min_rr(r["experimental_entry"], r["experimental_sl"], r["experimental_tps"])
    ]
    experimental_keys = {f"{r['symbol']}:experimental" for r in experimental_eligible}

    # مجموعات الرموز حسب النوع (بصرف النظر عن سبق التنبيه) — تُستخدم فقط لتوثيق أي
    # أنواع أخرى ظهرت لنفس العملة بنفس دورة الفحص (concurrent_signals)، بلا أي استبعاد
    # فعلي بينها؛ الأنواع الأربعة تبقى مستقلة تمامًا كما هي، هذا توثيق تشخيصي بحت
    # لتحليل لاحق (trade_stats.py) يجاوب: "أي نوع يفوز فعليًا لما يتزامن مع غيره؟"
    early_symbols = {r["symbol"] for r in early_eligible}
    breakout_symbols = {r["symbol"] for r in breakout_eligible}
    experimental_symbols = {r["symbol"] for r in experimental_eligible}

    def _concurrent_signals_for(symbol, exclude_type):
        others = []
        if symbol in strong_symbols and exclude_type != "official":
            others.append("official")
        if symbol in early_symbols and exclude_type != "early":
            others.append("early")
        if symbol in breakout_symbols and exclude_type != "breakout":
            others.append("breakout")
        if symbol in experimental_symbols and exclude_type != "experimental":
            others.append("experimental")
        return others

    for r in strong:
        r["concurrent_signals"] = _concurrent_signals_for(r["symbol"], "official")
    for r in early_eligible:
        r["concurrent_signals"] = _concurrent_signals_for(r["symbol"], "early")
    for r in breakout_eligible:
        r["concurrent_signals"] = _concurrent_signals_for(r["symbol"], "breakout")
    for r in experimental_eligible:
        r["concurrent_signals"] = _concurrent_signals_for(r["symbol"], "experimental")

    prev_alerted, prev_dominance = load_state(gist_files)
    fresh = [r for r in strong if r["symbol"] not in prev_alerted]
    fresh_early = [r for r in early_eligible if f"{r['symbol']}:early" not in prev_alerted]
    fresh_breakout = [r for r in breakout_eligible if f"{r['symbol']}:breakout" not in prev_alerted]
    fresh_experimental = [r for r in experimental_eligible if f"{r['symbol']}:experimental" not in prev_alerted]

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
          f"إشارات مبكرة جديدة: {len(fresh_early)} | إشارات انفجار جديدة: {len(fresh_breakout)} | "
          f"إشارات تجريبية جديدة: {len(fresh_experimental)}")

    for r in fresh:
        caution = market_caution and not r["symbol"].startswith("BTC")
        alert_text = format_alert(r, caution)
        r["_msg_id"] = send_telegram(alert_text)
        r["_alert_text"] = alert_text
        time.sleep(1)  # تجنب تجاوز حد تيليجرام لعدد الرسائل بالثانية

    for r in fresh_early:
        alert_text = format_early_alert(r)
        r["_alert_text"] = alert_text
        # إشارة بشرط واحد فقط ("احتمالية") تُسجَّل وتُتابَع (TP/SL) لكن بدون إرسال
        # إشعار تيليجرام — الإرسال محصور بالإشارات ذات شرطين فأكثر ("مؤكدة"/"مؤكدة قوية")
        if r.get("early_confidence") == "احتمالية":
            r["_msg_id"] = None
        else:
            r["_msg_id"] = send_telegram(alert_text)
            time.sleep(1)

    for r in fresh_breakout:
        alert_text = format_breakout_alert(r)
        r["_msg_id"] = send_telegram(alert_text)
        r["_alert_text"] = alert_text
        time.sleep(1)

    for r in fresh_experimental:
        alert_text = format_experimental_alert(r)
        r["_msg_id"] = send_telegram(alert_text)
        r["_alert_text"] = alert_text
        time.sleep(1)

    # تسجيل الإشارات الجديدة كصفقات مفتوحة قيد المتابعة لاحقًا (رسمية + مبكرة + انفجار + تجريبية)
    open_new_positions(open_positions, fresh)
    open_new_early_positions(open_positions, fresh_early)
    open_new_breakout_positions(open_positions, fresh_breakout)
    open_new_experimental_positions(open_positions, fresh_experimental)

    # حفظ موحّد: ذاكرة الإشارات (رسمية + مبكرة + انفجار + تجريبية) + BTC Dominance + الصفقات المفتوحة + أرشيف الصفقات المغلقة حديثًا
    # + إحصائيات أداء محسوبة من السجل المحدَّث (خيار 3: تتبع فقط، بدون تعديل تلقائي على منطق البوت)
    save_all_state(
        strong_symbols | early_keys | breakout_keys | experimental_keys,
        btc_dominance, open_positions, closed_now, gist_files
    )

    print("انتهى المسح.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        tb = traceback.format_exc()
        print(tb)
        send_admin_alert(
            f"توقف السكربت بخطأ غير متوقع أثناء التشغيل:\n"
            f"{type(e).__name__}: {e}\n\n"
            f"آخر جزء من تتبع الخطأ:\n{tb[-600:]}"
        )
        sys.exit(1)  # يبقي حالة GitHub Action فاشلة (❌) بدل أن تظهر ناجحة رغم العطل
