"""
ماسح السوق — نسخة البايثون (تعمل بجدولة تلقائية عبر GitHub Actions)
نفس منطق أداة HTML: فلترة سيولة/حركة -> تحليل عميق -> تأكيد فريم أعلى -> استقرار -> تنبيه تيليجرام

يضيف أيضًا مسارًا مستقلاً لـ"إشارات مبكرة" (انضغاط تقلب / تراكم صامت) لعملات لم تصل بعد
لإشارة شراء كاملة، كتحذير رادار بدون خطة دخول مؤكدة — لتفادي مشكلة "شراء القمة" حيث
الإشارة الرسمية تصل بعد ما الحركة صارت واضحة للجميع.

الإشارة المبكرة (early_v2): أُعيد بناؤها كنظام Confluence بالنقاط (Accumulation / Compression / Structure /
Momentum / Volume / Market) مع مستويات WATCH → VERY STRONG PRE-BREAKOUT SETUP وتتبّع تطور الـScore عبر الزمن —
انظر قسم "الإشارة المبكرة v2" أدناه. إعداداتها كلها EARLY_* بمتغيرات البيئة.

يضيف كذلك فلتر "إرهاق/امتداد زائد" (Overextension) يعاقب درجة الإشارات الرسمية نفسها لو
السعر بعيد جدًا عن EMA50 بوحدات ATR — لمعالجة نفس مشكلة "شراء القمة" من جهة الإشارة
الرسمية مباشرة، وليس فقط عبر تحذير مبكر منفصل.

الإشارة الرسمية (confluence_v1): أُعيدت كتابتها بالكامل كنظام confluence صارم بدل مجموع درجات مرجّح:
    HTF trend (EMA20>EMA50 + ADX>25) -> trend على الفريم الحالي (EMA20/50 + ADX>25 + DI)
    -> محفّز MACD cross صاعد حديث -> RSI غير متشبع -> حجم >= 1.5x متوسط 20 شمعة
    -> OBV بلا توزيع خفي -> وقف خسارة بمضاعف ATR + حجم مركز من نسبة المخاطرة (1-2%).
كل الشروط إلزامية (gates)؛ الدرجة (score) تُستخدم فقط لترتيب الجودة وللسياسة الصارمة حسب market_regime.
"""

import os
import sys
import json
import time
import traceback
import datetime as dt
import concurrent.futures
import requests

# استيراد محمي: أي عطل باستيراد وحدة المحفظة الوهمية (ملف مفقود، خطأ Syntax لاحق فيه...)
# ما ينهارش السكانر الحقيقي، بس يعطّل ميزة المحفظة الوهمية لهذه التشغيلة فقط
try:
    import paper_trading
    _PAPER_TRADING_AVAILABLE = True
except Exception as _paper_import_err:
    _PAPER_TRADING_AVAILABLE = False
    print(f"⚠️ لم يتم تحميل وحدة المحفظة الوهمية (paper_trading.py) — ستُعطَّل هذه التشغيلة: {_paper_import_err}")

# ---------- الإعدادات (تُقرأ من متغيرات البيئة / GitHub Secrets) ----------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
INTERVAL = os.environ.get("SCAN_INTERVAL", "1h")          # 15m / 1h / 4h / 1d
DEPTH = int(os.environ.get("SCAN_DEPTH", "60"))            # عدد العملات للفحص العميق
SCAN_LIMIT = 400                                            # عدد الشموع التاريخية لكل عملة
LIQUIDITY_FLOOR = 1_000_000                                 # أدنى سيولة 24س بالدولار

EXCLUDE_SUFFIX = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
EXCLUDE_SYMS = {
    "USDCUSDT","FDUSDUSDT","TUSDUSDT","DAIUSDT","USDPUSDT",
    "EURUSDT","GBPUSDT","AEURUSDT","BFUSDUSDT",
    # --- قائمة سوداء (بقيت 8 عملات ضعيفة السيولة/غير مناسبة، أُلغي حظر 11 عملة) ---
    "ALLOUSDT",
    "API3USDT",
    "DCRUSDT",
    "GMTUSDT",
    "KORUBUSDT",
    "MSTRBUSDT",
    "QIUSDT",
    "ZKPUSDT",
}

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

# ---------- إعدادات الإشارة الرسمية (Confluence) ----------
# كل القيم قابلة للتعديل عبر متغيرات البيئة بدون تغيير الكود.
OFFICIAL_ENGINE_VERSION = "confluence_v1"
OFFICIAL_REQUIRE_HTF = os.environ.get("OFFICIAL_REQUIRE_HTF", "1") != "0"
# true = لا إشارة رسمية بدون تأكيد صريح من الفريم الأعلى (فشل جلبه = لا إشارة، fail-closed)
OFFICIAL_ADX_MIN = float(os.environ.get("OFFICIAL_ADX_MIN", "25"))               # ADX فوقه = اتجاه قائم (للفريم الحالي والأعلى)
OFFICIAL_MACD_CROSS_LOOKBACK = int(os.environ.get("OFFICIAL_MACD_CROSS_LOOKBACK", "3"))  # أقصى عمر (بالشموع) لتقاطع MACD الصاعد
OFFICIAL_RSI_MIN = float(os.environ.get("OFFICIAL_RSI_MIN", "40"))               # تحته زخم ضعيف لا يتوافق مع اتجاه صاعد
OFFICIAL_RSI_MAX = float(os.environ.get("OFFICIAL_RSI_MAX", "70"))               # فوقه تشبع شرائي -> ممنوع الدخول
OFFICIAL_VOL_MULT = float(os.environ.get("OFFICIAL_VOL_MULT", "1.5"))            # حجم >= هذا المضاعف x متوسط 20 شمعة (من شمعة التقاطع حتى الآن)
OFFICIAL_MIN_ATR_PCT = float(os.environ.get("OFFICIAL_MIN_ATR_PCT", "0.08"))     # أدنى تقلب (ATR%) لقبول الإشارة
OFFICIAL_SL_ATR_MULT = float(os.environ.get("OFFICIAL_SL_ATR_MULT", "1.5"))     # وقف الخسارة = entry - ATR x هذا المضاعف (لا يُوسَّع بعد الدخول)
OFFICIAL_TP_R_MULT = float(os.environ.get("OFFICIAL_TP_R_MULT", "1.0"))          # TP1 = entry + R x هذا المضاعف
OFFICIAL_RISK_PCT = float(os.environ.get("OFFICIAL_RISK_PCT", "1.0"))            # المخاطرة لكل صفقة % من الحساب (الشائع 1-2%)
OFFICIAL_MAX_POSITION_PCT = float(os.environ.get("OFFICIAL_MAX_POSITION_PCT", "100"))  # سقف حجم المركز % من الحساب (Spot بلا رافعة)
OFFICIAL_SLIPPAGE_PCT = float(os.environ.get("OFFICIAL_SLIPPAGE_PCT", "0.10"))   # slippage تقديري لكل جهة (0.05-0.30% على الأزواج الكبيرة)
OFFICIAL_BASE_SCORE = 2.0   # درجة أي إشارة اجتازت كل الشروط؛ تُضاف علاوات 0.5 (OBV صاعد / تقاطع طازج / ADX متصاعد)

# ---------- إعدادات الإشارات المبكرة (انضغاط تقلب / تراكم صامت) ----------
SQUEEZE_LOOKBACK = 20           # عدد الشموع لحساب متوسط عرض نطاق Bollinger
SQUEEZE_RATIO_THRESHOLD = 0.6   # عرض النطاق الحالي <= هذه النسبة من المتوسط -> يُعتبر انضغاطًا
ACCUM_WINDOW = 20               # عدد الشموع لقياس التراكم الصامت
ACCUM_PRICE_MAX_MOVE_PCT = 2.0  # (كان 4.0) أقصى تحرك سعري% خلال النافذة كي يُعتبر السعر "شبه ثابت"
ACCUM_FLOW_RATIO_MIN = 0.5      # (كان 0.3) أدنى نسبة صافي تدفق شراء (OBV/حجم) كي يُعتبر تراكمًا واضحًا

# ---------- إعدادات إشارة الانفجار (Breakout) ----------
BREAKOUT_LOOKBACK = 10          # عدد الشموع للبحث فيها عن أعلى قمة سابقة قبل الاختراق
BREAKOUT_VOL_MULT = 1.3         # الحجم الحالي يجب أن يتجاوز متوسط الحجم بهذا المضاعف
BREAKOUT_MIN_ATR_PCT = 0.08     # أدنى نسبة تقلب (ATR%) لقبول إشارة الانفجار
BREAKOUT_SL_ATR_MULT = float(os.environ.get("BREAKOUT_SL_ATR_MULT", "1.5"))  # (كان 1.8 ثابتة بالكود)

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
EARLY_SL_ATR_MULT = float(os.environ.get("EARLY_SL_ATR_MULT", "1.5"))  # (كان 2.0)
# عدد الأهداف والثقة يعتمدان مباشرة على عدد الشروط المتحققة (squeeze / accumulation / divergence / momentum):
# شرط واحد = احتمالية (هدف واحد)، شرطان = مؤكدة (هدفان)، 3 فأكثر = مؤكدة قوية (3-4 أهداف)

# وزن "الزخم المتفق عليه" (EMA trend + MACD) في نظام score — كانا يُحسبان كعاملين منفصلين
# رغم ارتباطهما الوثيق (كلاهما يقيس نفس ظاهرة الزخم تقريبًا)، فيُعطى الزخم وزنًا مضاعفًا
# بدون قصد. الآن لو اتفقا يُعطيان وزنًا واحدًا مجمّعًا (1.5 بدل 2)، ولو اختلفا فالنتيجة صفر كالسابق.
MOMENTUM_AGREE_WEIGHT = float(os.environ.get("MOMENTUM_AGREE_WEIGHT", "1.5"))

# نطاق حيادي حول الصفر (%) لتصنيف نتيجة الصفقة النهائية "بدون تغيير حقيقي" بدل ربح/خسارة
# صريحين — يُستخدم في compute_stats لتفادي تصنيف صفقة لمست TP1 ثم رجعت لنقطة قريبة من
# الصفر كـ"فوز" (المشكلة التي حُدِّدت بالمراجعة الخارجية للكود)
BREAKEVEN_BAND_PCT = float(os.environ.get("BREAKEVEN_BAND_PCT", "0.1"))

# ---------- إعدادات market_regime (نظام السوق العام، مرجعه BTCUSDT) ----------
# مؤشر تشخيصي يُحسب مرة واحدة فقط لكل دورة مسح (وليس لكل عملة) ويُسجَّل مع كل صفقة
# جديدة -- يفيد لاحقًا بتحليل: هل نوع إشارة معيّن يؤدي أفضل حسب حالة السوق العام؟
# يُستخدم الآن أيضًا كفلتر فعلي عبر REGIME_POLICY (انظر أدناه) إضافةً لتسجيله مع الصفقات.
MARKET_REGIME_EMA_PERIOD = 50
MARKET_REGIME_SLOPE_LOOKBACK = 10   # عدد الشموع للمقارنة (حالي مقابل قبل X شمعة) لتحديد ميل EMA50

# ---------- سياسة التداول حسب market_regime (فلتر فعلي) ----------
# allow  = السماح بالإشارة كما هي
# strict = السماح فقط لو توفرت شروط إضافية (HTF متوافق + حجم مؤكد + غير ممتد، وللرسمية score أعلى)
# block  = عدم قبول هذا النوع إطلاقًا في هذا النظام (لا تنبيه ولا صفقة ولا تسجيل في ذاكرة التنبيهات،
#          فلو تحسّن السوق لاحقًا تُعامَل كإشارة جديدة)
# regime غير معروف (None) = السماح دائمًا (fail-open) حتى لا يتعطل البوت بسبب فشل جلب BTC.
# للتعديل بدون تغيير الكود: REGIME_POLICY_JSON='{"ranging":{"breakout":"block"}}' (يُدمج فوق الافتراضي)
# ولإيقاف الفلتر كليًا: REGIME_FILTER_ENABLED=0
REGIME_FILTER_ENABLED = os.environ.get("REGIME_FILTER_ENABLED", "1") != "0"
REGIME_STRICT_MIN_SCORE_OFFICIAL = float(os.environ.get("REGIME_STRICT_MIN_SCORE_OFFICIAL", "2.5"))
REGIME_POLICY = {
    "trending_up":   {"official": "allow",  "early": "allow", "breakout": "allow",  "experimental": "allow"},
    "ranging":       {"official": "strict", "early": "allow", "breakout": "strict", "experimental": "block"},
    "trending_down": {"official": "strict", "early": "allow", "breakout": "block",  "experimental": "block"},
}
try:
    for _reg, _pol in json.loads(os.environ.get("REGIME_POLICY_JSON", "{}") or "{}").items():
        REGIME_POLICY.setdefault(_reg, {}).update(_pol)
except Exception as _e:
    print(f"⚠️ REGIME_POLICY_JSON غير صالح — استُخدمت السياسة الافتراضية: {_e}")


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
    opens = [float(k[1]) for k in klines]
    # حجم الشراء العدواني (taker buy base volume) من نفس الشموع — يُستخدم لحساب CVD الحقيقي
    taker_buy = [float(k[9]) if len(k) > 9 and k[9] not in (None, "") else None for k in klines]
    macd_line, signal = macd(closes)
    bb_upper, bb_lower = bollinger(closes)
    tenkan, kijun = ichimoku_tenkan_kijun(highs, lows)
    adx_vals, plus_di_vals, minus_di_vals = compute_adx_di(highs, lows, closes)
    return {
        "closes": closes, "highs": highs, "lows": lows, "vols": vols,
        "opens": opens, "taker_buy": taker_buy,
        "ema7": ema(closes, 7), "ema14": ema(closes, 14),
        "ema20": ema(closes, 20),
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
    # ملاحظة (confluence_v1): هذه الدالة لم تعد تحدد الإشارة الرسمية. تبقى مزوّدًا للحالات التشخيصية
    # (trend_up / macd_bull / rsi_state / bb_state / ranging / divergence / near_resistance ...) التي تعتمد
    # عليها باقي الإشارات (early / breakout / experimental) والنموذج التكيفي. قرار الرسمية: official_confluence().
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
        # Divergence مُستبعد كليًا من حساب درجة الإشارة الرسمية (2026-09-14): التحليل
        # أظهر أنه لا يضيف قيمة حقيقية للإشارة الرسمية رغم كونه عامل تشخيصي مفيد
        # للإشارات المبكرة؛ يبقى محسوبًا ومُرجعًا (r["divergence"]) لاستخدامه هناك فقط.
        if near_resistance:
            score -= 1
        if obv_confirm:
            score += trend_dir * 0.5
        if extended:
            score -= trend_dir * 1  # عقوبة على الامتداد المفرط -- احتمال دخول متأخر (شراء القمة)

        # طبقة إضافية على وزن RSI فوق rsi_state الأساسي (2026-09-14): rsi_state أعلاه
        # يعطي +1/-1 فقط عند تجاوز 35/65، بدون تمييز داخل النطاق المحايد ولا تعزيز إضافي
        # عند التطرف. هذه الطبقة تضيف: عقوبة إضافية عند تشبع شرائي (RSI>65)، مكافأة صغيرة
        # عند تشبع بيعي (RSI<35، احتمال ارتداد)، ومكافأة أكبر لمنطقة "النجاح الذهبية"
        # المحايدة (40-60) التي أظهرها تحليل العملات الناجحة سابقًا.
        if rv > 65:
            score -= 0.5
        if rv < 35:
            score += 0.3
        if 40 <= rv <= 60:
            score += 0.5

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


# ---------------- الإشارة الرسمية: نظام Confluence (confluence_v1) ----------------

def macd_recent_bull_cross(macd_line, signal, i, lookback=OFFICIAL_MACD_CROSS_LOOKBACK):
    """
    محفّز الدخول: تقاطع MACD صاعد (MACD يعبر فوق خط الإشارة) خلال آخر lookback شمعة، بشرط
    أن يبقى MACD فوق الإشارة عند الشمعة الحالية (التقاطع لم يُلغَ). يرجع فهرس شمعة التقاطع
    الأحدث أو None.
    """
    if i < 1 or macd_line[i] is None or signal[i] is None or not macd_line[i] > signal[i]:
        return None
    for j in range(i, max(0, i - lookback), -1):
        if j < 1:
            break
        if macd_line[j - 1] <= signal[j - 1] and macd_line[j] > signal[j]:
            return j
    return None


def official_plan(ind, i, resistance=None):
    """
    خطة الدخول: stop-loss بمضاعف ATR (لا يُوسَّع بعد الدخول)، TP1 بمضاعف R مع احترام أقرب
    مقاومة، وحجم المركز = المخاطرة ÷ مسافة الـ stop (كنسبة % من الحساب، بسقف OFFICIAL_MAX_POSITION_PCT).
    """
    entry = ind["closes"][i]
    atrv = atr_value_at(ind, i)
    if entry <= 0 or atrv <= 0:
        return {"entry": None, "sl": None, "tps": [], "size_pct": None, "risk_pct": None}
    sl = entry - atrv * OFFICIAL_SL_ATR_MULT
    risk = entry - sl
    tp1 = entry + risk * OFFICIAL_TP_R_MULT
    if resistance and tp1 >= resistance:
        tp1 = resistance
    stop_pct = risk / entry * 100
    size_pct = min(OFFICIAL_MAX_POSITION_PCT, OFFICIAL_RISK_PCT / stop_pct * 100) if stop_pct > 0 else None
    return {"entry": entry, "sl": sl, "tps": [tp1], "size_pct": size_pct, "risk_pct": OFFICIAL_RISK_PCT}


def official_confluence(ind, i, r, htf_bullish, atrp):
    """
    قرار الإشارة الرسمية (شراء فقط — Spot): كل الشروط التالية إلزامية، وفشل أي منها = لا إشارة.
    ترتيبها يتبع منطق الشرح: الاتجاه أولًا ثم المحفّز ثم التأكيد ثم إدارة المخاطرة.
      1) htf_trend           : الفريم الأعلى صاعد (EMA20>EMA50 + ميل EMA50 + السعر فوقه + ADX>25)
      2) trend               : الفريم الحالي: EMA20>EMA50 + ADX>25 + +DI>-DI (اتجاه صاعد قائم لا سوق عرضي)
      3) macd_cross          : تقاطع MACD صاعد حديث (المحفّز)
      4) rsi_band            : RSI بين OFFICIAL_RSI_MIN و OFFICIAL_RSI_MAX (لا تشبع شرائي)
      5) volume              : حجم >= OFFICIAL_VOL_MULT x متوسط 20 شمعة (من شمعة التقاطع حتى الحالية)
      6) no_obv_distribution : لا تباعد OBV سلبي (سعر يصعد بينما OBV يهبط = توزيع خفي)
      7) not_extended        : السعر ليس ممتدًا فوق EMA50 بأكثر من EXTENSION_ATR_THRESHOLD x ATR (لا شراء قمة)
      8) not_near_resistance : لا مقاومة قريبة فوق السعر
      9) no_divergence       : لا Bullish Divergence (مستبعد من الرسمية بقرار سابق مبني على التحليل)
     10) min_atr             : تقلب كافٍ (ATR%)
    الدرجة: عند النجاح OFFICIAL_BASE_SCORE + علاوات 0.5 (OBV صاعد / تقاطع طازج على الشمعة الحالية / ADX متصاعد).
    عند الفشل: min(1.4, 0.2 x عدد الشروط المتحققة) — أي دائمًا أقل من 1.5 فتبقى بوابة الإشارة المبكرة سليمة.
    الدالة نقية (pure) على (ind, i, r, htf_bullish, atrp) فتصلح للاختبار الرجعي/walk-forward لاحقًا.
    """
    gates = {}

    # 1) الفريم الأعلى (None = غير متاح: يُرفض إن كان OFFICIAL_REQUIRE_HTF مفعّلًا)
    gates["htf_trend"] = (not OFFICIAL_REQUIRE_HTF) if htf_bullish is None else bool(htf_bullish)

    # 2) اتجاه الفريم الحالي
    ema20, ema50 = ind["ema20"][i], ind["ema50"][i]
    adx_val, plus_di, minus_di = r.get("adx_val"), r.get("plus_di"), r.get("minus_di")
    gates["trend"] = bool(
        ema20 is not None and ema50 is not None and ema20 > ema50
        and adx_val is not None and adx_val > OFFICIAL_ADX_MIN
        and plus_di is not None and minus_di is not None and plus_di > minus_di
    )

    # 3) المحفّز: MACD cross صاعد حديث
    cross_idx = macd_recent_bull_cross(ind["macd"], ind["signal"], i)
    gates["macd_cross"] = cross_idx is not None

    # 4) RSI: يمنع الدخول عند التشبع المتطرف
    rv = r.get("rv")
    gates["rsi_band"] = rv is not None and OFFICIAL_RSI_MIN <= rv < OFFICIAL_RSI_MAX

    # 5) تأكيد الحجم (من شمعة التقاطع حتى الحالية: الحجم يؤكد الحركة لا يشترط أن يقع على آخر شمعة تحديدًا)
    vol_ratio = 0.0
    for j in range(cross_idx if cross_idx is not None else i, i + 1):
        va = ind["vol_avg"][j]
        if va:
            vol_ratio = max(vol_ratio, ind["vols"][j] / va)
    gates["volume"] = vol_ratio >= OFFICIAL_VOL_MULT

    # 6) OBV: تباعد سلبي = توزيع خفي ينذر بالانعكاس؛ OBV صاعد يمنح علاوة فقط
    w = OBV_TREND_WINDOW
    obv_rising, obv_distribution = False, False
    if i >= w:
        obv_rising = ind["obv"][i] > ind["obv"][i - w]
        obv_distribution = ind["closes"][i] > ind["closes"][i - w] and ind["obv"][i] < ind["obv"][i - w]
    gates["no_obv_distribution"] = not obv_distribution

    # 7-10) إدارة المخاطرة / جودة الدخول
    gates["not_extended"] = not overextended(ind, i, True)
    gates["not_near_resistance"] = not r.get("near_resistance")
    gates["no_divergence"] = not r.get("divergence")
    gates["min_atr"] = atrp >= OFFICIAL_MIN_ATR_PCT

    failed = [name for name, passed in gates.items() if not passed]
    ok = not failed

    plan = {"entry": None, "sl": None, "tps": [], "size_pct": None, "risk_pct": None}
    if ok:
        score = OFFICIAL_BASE_SCORE
        if obv_rising:
            score += 0.5
        if cross_idx == i:
            score += 0.5
        adx_prev = ind["adx"][i - 1] if i >= 1 else None
        if adx_prev is not None and adx_val > adx_prev:
            score += 0.5
        plan = official_plan(ind, i, r.get("resistance"))
        if plan["entry"] is None:   # بيانات غير صالحة لحساب الخطة -> لا إشارة
            ok, failed, score = False, ["plan_invalid"], 1.0
    else:
        score = min(1.4, 0.2 * (len(gates) - len(failed)))

    return {
        "ok": ok, "score": score, "failed": failed, "gates": gates,
        "cross_idx": cross_idx, "vol_ratio": vol_ratio, "obv_rising": obv_rising,
        **plan,
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

# تكلفة التنفيذ الكاملة للإشارة الرسمية (رسوم دخول+خروج + slippage للجهتين) — يجب أن يتجاوزها
# الربح عند TP1 بهامش MIN_PROFIT_PCT، وإلا فلا ميزة (edge) فعلية بعد التكاليف.
OFFICIAL_TOTAL_COST_PCT = TRADING_FEE_PCT + 2 * OFFICIAL_SLIPPAGE_PCT

# ---------- محرك اختيار الإشارات التكيفي ----------
# لا يلغي منطق اكتشاف الإشارات الحالي؛ يضيف طبقة ترتيب تعتمد على نتائج الصفقات
# المغلقة السابقة لكل نوع إشارة ولكل عامل تشخيصي، مع انكماش إحصائي لتجنب الثقة
# الزائدة في العينات الصغيرة.
ADAPTIVE_ENGINE_VERSION = os.environ.get("ADAPTIVE_ENGINE_VERSION", "adaptive_v2")
ADAPTIVE_MIN_TRADES = int(os.environ.get("ADAPTIVE_MIN_TRADES", "100"))
ADAPTIVE_PRIOR_TRADES = float(os.environ.get("ADAPTIVE_PRIOR_TRADES", "40"))
ADAPTIVE_MIN_RANK = float(os.environ.get("ADAPTIVE_MIN_RANK", "0.0"))

# عوامل كل نوع؛ تُستخدم كـ soft evidence وليست شروط استبعاد مطلقة.
ADAPTIVE_FACTORS = {
    "official": ["htf_aligned", "vol_confirm", "obv_confirm", "di_confirm", "momentum_agree", "extended", "ranging", "near_resistance", "rsi_state", "bb_state"],
    "early": ["squeeze", "accumulation", "divergence", "momentum", "extended", "vol_confirm", "htf_aligned", "ranging", "near_resistance", "obv_confirm"],
    "breakout": ["trend_support", "macd_bull", "rsi_ok", "extended", "vol_confirm", "htf_aligned", "ranging", "near_resistance"],
    "experimental": ["trend_support", "volume_confirm", "mfi_bullish", "htf_aligned", "vol_confirm", "ranging", "near_resistance"],
}

# عوامل النوعين breakout/experimental تُحفظ داخل قاموس details وبمفتاح موجود فقط عند تحققها
# (الغياب = False). نسطّحها إلى حقول bool صريحة حتى يراها النموذج التكيفي وtrade_stats.
ADAPTIVE_DETAIL_FACTORS = {
    "breakout": ("trend_support", "macd_bull", "rsi_ok"),
    "experimental": ("trend_support", "volume_confirm", "mfi_bullish"),
}


def flatten_signal_details(signal_type, details):
    """يحوّل قاموس details إلى {factor: bool} لكل عامل معروف للنوع (الغياب = False)."""
    keys = ADAPTIVE_DETAIL_FACTORS.get(signal_type)
    if not keys:
        return {}
    details = details or {}
    return {k: bool(details.get(k)) for k in keys}


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


# ======================================================================================
# الإشارة المبكرة v2 (Early Signal) — نظام Confluence بالنقاط
# ======================================================================================
# الهدف: اكتشاف "Pre-Breakout Probability Setup" = ظروف تراكم + انضغاط + هيكل + زخم + حجم
# تسبق الحركة القوية تاريخيًا — وليس تنبؤًا بالانفجار ولا ضمانًا للصعود. الـScore مقياس
# توافق عوامل فقط وليس نسبة نجاح. منفصلة تمامًا عن إشارتي Breakout/Explosive.
# كل الأوزان والعتبات قابلة للتعديل عبر متغيرات البيئة (EARLY_*) دون تغيير الكود،
# أو دفعة واحدة عبر EARLY_WEIGHTS_JSON='{"obv_bullish": 12}' (يُدمج فوق الافتراضي).
# بيانات غير متوفرة (Open Interest / Funding) لا تُخترع: تُعلَّم "n/a" وتظهر في التقرير.

EARLY_ENGINE_VERSION = "early_v2"


def _env_f(name, default):
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


# مستويات الإشارة (Score خام بنفس وحدة الأوزان)
EARLY_LEVEL_WATCH = _env_f("EARLY_LEVEL_WATCH", 50)
EARLY_LEVEL_EARLY = _env_f("EARLY_LEVEL_EARLY", 65)
EARLY_LEVEL_STRONG = _env_f("EARLY_LEVEL_STRONG", 75)
EARLY_LEVEL_VERY_STRONG = _env_f("EARLY_LEVEL_VERY_STRONG", 85)
EARLY_LEVEL_NAMES = {
    0: "IGNORE", 1: "WATCH", 2: "EARLY SETUP", 3: "STRONG EARLY SETUP",
    4: "VERY STRONG PRE-BREAKOUT SETUP",
}
EARLY_SEND_MIN_RANK = int(_env_f("EARLY_SEND_MIN_RANK", 2))   # أدنى مستوى يُرسل تنبيهًا ويُفتح له تتبع (2 = EARLY SETUP)

# أوزان العوامل (القيم المذكورة بالبرومت + إضافات صغيرة قابلة للمعايرة، اجعلها 0 لتعطيلها)
EARLY_WEIGHTS = {
    # Accumulation
    "obv_bullish": 10, "ad_bullish": 5, "cmf_positive": 8, "price_consolidation": 8, "cvd_improving": 4,
    # Compression
    "bb_width_low": 10, "bb_expansion_start": 5, "atr_compression": 5,
    # Structure
    "higher_lows": 10, "tightening_range": 7, "near_resistance": 8, "resistance_tests": 7,
    "failed_breakdown": 5, "reclaim_level": 3, "tight_candles": 3,
    # Momentum
    "rsi_improving": 7, "bullish_divergence": 8, "hidden_bullish_divergence": 6, "macd_hist_improving": 7,
    # Volume
    "rvol_improving": 7, "volume_confirmation": 5, "volume_profile_support": 2,
    # Market
    "market_ok": 5, "relative_strength": 5, "htf_agree": 4, "ltf_timing": 2,
}
try:
    for _k, _v in json.loads(os.environ.get("EARLY_WEIGHTS_JSON", "{}") or "{}").items():
        if _k in EARLY_WEIGHTS:
            EARLY_WEIGHTS[_k] = float(_v)
except Exception as _e:
    print(f"⚠️ EARLY_WEIGHTS_JSON غير صالح — استُخدمت الأوزان الافتراضية: {_e}")

EARLY_CATEGORIES = {
    "accumulation": ["obv_bullish", "ad_bullish", "cmf_positive", "price_consolidation", "cvd_improving"],
    "compression": ["bb_width_low", "bb_expansion_start", "atr_compression"],
    "structure": ["higher_lows", "tightening_range", "near_resistance", "resistance_tests",
                  "failed_breakdown", "reclaim_level", "tight_candles"],
    "momentum": ["rsi_improving", "bullish_divergence", "hidden_bullish_divergence", "macd_hist_improving"],
    "volume": ["rvol_improving", "volume_confirmation", "volume_profile_support"],
    "market": ["market_ok", "relative_strength", "htf_agree", "ltf_timing"],
}
EARLY_ACTIVE_FRACTION = _env_f("EARLY_ACTIVE_FRACTION", 0.35)    # فئة تُعتبر "فعّالة" لو نقاطها >= هذه النسبة من أقصاها
EARLY_MIN_ACTIVE_LAYERS = int(_env_f("EARLY_MIN_ACTIVE_LAYERS", 3))  # أقل عدد طبقات فعّالة (من 5 طبقات تحليلية، بدون Market) لإرسال إشارة

# فلاتر السيولة والجودة
EARLY_MIN_QUOTE_VOLUME = _env_f("EARLY_MIN_QUOTE_VOLUME", 2_000_000)   # أدنى سيولة 24س (أشد من LIQUIDITY_FLOOR)
EARLY_MAX_SPREAD_PCT = _env_f("EARLY_MAX_SPREAD_PCT", 0.15)             # أعلى سبريد % (من bid/ask بالـticker)
EARLY_MIN_CANDLES = int(_env_f("EARLY_MIN_CANDLES", 120))               # أقل عدد شموع لاعتبار البيانات كاملة
EARLY_MAX_ATR_PCT = _env_f("EARLY_MAX_ATR_PCT", 6.0)                    # فوقه = تقلب متطرف
EARLY_ANOMALY_ATR_MULT = _env_f("EARLY_ANOMALY_ATR_MULT", 5.0)          # شمعة مداها >= هذا x ATR = شاذة
EARLY_PUMP_PCT_24 = _env_f("EARLY_PUMP_PCT_24", 8.0)                    # صعود % خلال 24 شمعة = حدث Pump فعلًا
EARLY_PUMP_PCT_72 = _env_f("EARLY_PUMP_PCT_72", 15.0)                   # صعود % خلال 72 شمعة
EARLY_MAX_FROM_BASE_ATR = _env_f("EARLY_MAX_FROM_BASE_ATR", 9.0)        # بعد السعر عن قاع نطاق التجميع (ATR)
EARLY_SPIKE_RVOL = _env_f("EARLY_SPIKE_RVOL", 3.5)                      # RVOL الشمعة الحالية فوقه = انفجار حجم (ليس تراكمًا)
EARLY_RANGE_WINDOW = int(_env_f("EARLY_RANGE_WINDOW", 20))
EARLY_RANGE_MAX_ATR = _env_f("EARLY_RANGE_MAX_ATR", 8.0)                # أقصى مدى للنطاق (بوحدات ATR) كي يُعتبر تجميعًا
EARLY_RES_LOOKBACK = int(_env_f("EARLY_RES_LOOKBACK", 120))
EARLY_NEAR_RES_MAX_PCT = _env_f("EARLY_NEAR_RES_MAX_PCT", 3.5)          # منطقة Pre-Breakout: المقاومة خلال هذه النسبة
EARLY_MIN_UPSIDE_PCT = _env_f("EARLY_MIN_UPSIDE_PCT", 2.0)              # أدنى مجال صعود قبل المقاومة التالية %
EARLY_MIN_UPSIDE_ATR = _env_f("EARLY_MIN_UPSIDE_ATR", 2.5)              # ... أو هذا المضاعف x ATR% أيهما أكبر
EARLY_BTC_BEAR_PENALTY = _env_f("EARLY_BTC_BEAR_PENALTY", 10)           # خصم عند هبوط قوي للسوق (BTC + ETH)
EARLY_FETCH_MIN = _env_f("EARLY_FETCH_MIN", 42)                         # لا نجلب الفريمات الأعلى/الأدنى إلا لو الـScore الأولي >= هذا (توفير طلبات)
EARLY_BLOCK_HTF_CONFLICT = os.environ.get("EARLY_BLOCK_HTF_CONFLICT", "1") != "0"

# تطور الإشارة (Signal Evolution)
EARLY_UPDATE_DELTA = _env_f("EARLY_UPDATE_DELTA", 10)           # تغير Score يستدعي رسالة تحديث بنفس المستوى
EARLY_DOWNGRADE_DELTA = _env_f("EARLY_DOWNGRADE_DELTA", 8)      # هبوط Score مع نزول المستوى يستدعي رسالة خفض
EARLY_COOLDOWN_HOURS = _env_f("EARLY_COOLDOWN_HOURS", 6)        # بعد إلغاء إشارة لا نعيد إطلاقها قبل مرور هذه المدة
EARLY_MISSED_EXPIRE = int(_env_f("EARLY_MISSED_EXPIRE", 6))     # دورات متتالية غاب فيها الرمز عن التحليل -> انتهاء صامت
EARLY_STATE_TTL_HOURS = _env_f("EARLY_STATE_TTL_HOURS", 72)

LTF_MAP = {"15m": "5m", "1h": "15m", "4h": "1h", "1d": "4h"}

# كاش شموع مشترك (HTF/LTF/BTC/ETH) خلال التشغيلة الواحدة — يمنع تكرار نفس الطلب
_KLINES_CACHE = {}
EARLY_MARKET_CTX = None   # يُبنى مرة واحدة لكل دورة مسح في run_scan()


def fetch_klines_cached(symbol, interval, limit):
    key = (symbol, interval, limit)
    hit = _KLINES_CACHE.get(key)
    if hit is not None:
        return hit
    data = fetch_klines(symbol, interval, limit)
    _KLINES_CACHE[key] = data
    return data


def early_level_rank(score):
    if score >= EARLY_LEVEL_VERY_STRONG:
        return 4
    if score >= EARLY_LEVEL_STRONG:
        return 3
    if score >= EARLY_LEVEL_EARLY:
        return 2
    if score >= EARLY_LEVEL_WATCH:
        return 1
    return 0


def early_confidence_label(rank):
    """تصنيف الثقة القديم (يُستخدم بالتتبع/الإحصاءات/المحفظة الوهمية) مبنيًا على مستوى الإشارة الجديد."""
    if rank >= 3:
        return "مؤكدة قوية"
    if rank == 2:
        return "مؤكدة"
    return None


# ---------------- أدوات مساعدة للمحرك ----------------

def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _atr_series(highs, lows, closes, period=14):
    n = len(closes)
    tr = [0.0] * n
    for j in range(1, n):
        tr[j] = max(highs[j] - lows[j], abs(highs[j] - closes[j - 1]), abs(lows[j] - closes[j - 1]))
    out = [None] * n
    for j in range(period, n):
        out[j] = sum(tr[j - period + 1:j + 1]) / period
    return out


def _swing_lows(lows, end, lookback, span=3):
    start = max(span, end - lookback + 1)
    out = []
    for j in range(start, end - span + 1):
        if lows[j] == min(lows[j - span:j + span + 1]):
            out.append(j)
    return out


def _swing_highs(highs, end, lookback, span=3):
    start = max(span, end - lookback + 1)
    out = []
    for j in range(start, end - span + 1):
        if highs[j] == max(highs[j - span:j + span + 1]):
            out.append(j)
    return out


def _percentile_rank(values, current):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(1 for v in vals if v <= current) / len(vals)


def _cluster_levels(levels, tol_pct=0.4):
    """يدمج مستويات المقاومة المتقاربة (ضمن tol_pct%) في منطقة واحدة، ويمثّلها بأعلى مستوى فيها."""
    out = []
    for lv in sorted(levels):
        if out and (lv - out[-1]) / out[-1] * 100 <= tol_pct:
            out[-1] = lv
        else:
            out.append(lv)
    return out


def early_resistances(ind, i, max_levels=6):
    """مقاومات ديناميكية فوق السعر: Swing Highs + أعلى قمة سابقة، مدمجة، من الأقرب للأبعد."""
    highs, closes = ind["highs"], ind["closes"]
    price = closes[i]
    levels = resistance_levels(highs[:i + 1], closes[:i + 1], lookback=EARLY_RES_LOOKBACK,
                               pivot_span=3, max_levels=max_levels + 4)
    prev_high = max(highs[max(0, i - EARLY_RES_LOOKBACK):i + 1])
    if prev_high > price * 1.0005:
        levels = list(levels) + [prev_high]
    return [lv for lv in _cluster_levels(levels) if lv > price][:max_levels]


def early_volume_profile(ind, i, window=100, bins=40):
    """Volume Profile مبسّط من الشموع: POC + مناطق LVN القريبة فوق السعر (تسارع محتمل). None لو البيانات غير كافية."""
    start = max(0, i - window + 1)
    h, l, c, v = ind["highs"][start:i + 1], ind["lows"][start:i + 1], ind["closes"][start:i + 1], ind["vols"][start:i + 1]
    if len(c) < 40:
        return None
    lo, hi = min(l), max(h)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    prof = [0.0] * bins
    for hj, lj, cj, vj in zip(h, l, c, v):
        tp = (hj + lj + cj) / 3
        prof[min(bins - 1, int((tp - lo) / step))] += vj
    poc_idx = max(range(bins), key=lambda b: prof[b])
    avg = sum(prof) / bins
    price = ind["closes"][i]
    lvn_above = None
    for b in range(bins):
        center = lo + (b + 0.5) * step
        if center > price and (center - price) / price * 100 <= 3.0 and prof[b] <= 0.5 * avg:
            lvn_above = center
            break
    return {"poc": lo + (poc_idx + 0.5) * step, "lvn_above": lvn_above}


def early_market_context(fetch=True):
    """سياق السوق العام من BTC وETH (مرة واحدة لكل دورة). يرجع None لو تعذّر (يُعامل n/a بلا اختراع قيم)."""
    def _state(symbol):
        kl = drop_unclosed_candle(fetch_klines_cached(symbol, INTERVAL, SCAN_LIMIT))
        closes = [float(k[4]) for k in kl]
        if len(closes) < 80:
            return None
        e20, e50 = ema(closes, 20), ema(closes, 50)
        falling = e50[-1] < e50[-7]
        ret = (closes[-1] / closes[-25] - 1) * 100
        if closes[-1] < e50[-1] and e20[-1] < e50[-1] and falling:
            st = "bearish"
        elif closes[-1] > e50[-1] and e20[-1] > e50[-1]:
            st = "bullish"
        else:
            st = "neutral"
        return {"state": st, "ret": ret, "closes": closes}
    try:
        btc = _state("BTCUSDT")
    except Exception as e:
        print(f"⚠️ [early_v2] تعذّر سياق BTC: {e}")
        btc = None
    try:
        eth = _state("ETHUSDT")
    except Exception:
        eth = None
    if not btc:
        return None
    strong_bear = btc["state"] == "bearish" and (btc["ret"] <= -3.0 or (eth and eth["state"] == "bearish"))
    return {"btc_state": btc["state"], "btc_ret": btc["ret"],
            "eth_state": eth["state"] if eth else None, "eth_ret": eth["ret"] if eth else None,
            "strong_bear": bool(strong_bear)}


def early_htf_context(symbol, interval):
    htf = HTF_MAP.get(interval)
    if not htf:
        return None
    try:
        kl = drop_unclosed_candle(fetch_klines_cached(symbol, htf, 100))
        closes = [float(k[4]) for k in kl]
        if len(closes) < 56:
            return None
        e20, e50 = ema(closes, 20), ema(closes, 50)
        falling = e50[-1] < e50[-6]
        if closes[-1] < e50[-1] and e20[-1] < e50[-1] and falling:
            trend = "bearish"
        elif closes[-1] > e50[-1] and e20[-1] > e50[-1]:
            trend = "bullish"
        else:
            trend = "neutral"
        return {"tf": htf, "trend": trend}
    except Exception:
        return None


def early_ltf_context(symbol, interval):
    ltf = LTF_MAP.get(interval)
    if not ltf:
        return None
    try:
        kl = drop_unclosed_candle(fetch_klines_cached(symbol, ltf, 100))
        closes = [float(k[4]) for k in kl]
        if len(closes) < 50:
            return None
        e20 = ema(closes, 20)
        m, s = macd(closes)
        hist = [a - b for a, b in zip(m, s)]
        timing = closes[-1] > e20[-1] and hist[-1] > hist[-3]
        return {"tf": ltf, "timing_ok": bool(timing)}
    except Exception:
        return None


# ---------------- المحرك ----------------

def early_engine(ind, i, r, atrp, ticker=None, market_ctx=None, htf_ctx=None, ltf_ctx=None,
                 symbol=None, interval=None, fetch=False):
    """
    يحسب Score الإشارة المبكرة من عدة طبقات مستقلة (Accumulation / Compression / Structure /
    Momentum / Volume / Market) مع فلاتر إقصاء وفلاتر إرسال. دالة قابلة للاختبار الرجعي:
    fetch=False يعني لا طلبات شبكة (تُمرَّر htf_ctx/ltf_ctx/market_ctx يدويًا).
    """
    W = EARLY_WEIGHTS
    highs, lows, closes, vols = ind["highs"], ind["lows"], ind["closes"], ind["vols"]
    opens = ind.get("opens")
    price = closes[i]
    n = i + 1
    items, notes_pos, notes_missing, risks, na = {}, [], [], [], []
    hard, send_block = [], []

    atr = atr_value_at(ind, i)
    if atr <= 0 or price <= 0:
        return {"version": EARLY_ENGINE_VERSION, "score": 0.0, "raw_score": 0.0, "level_rank": 0,
                "level": "IGNORE", "sendable": False, "blocked": ["bad_atr"], "send_blocked": [],
                "cats": {}, "cat_max": {}, "items": {}, "metrics": {}, "plan": None,
                "reasons": [], "missing": [], "risks": [], "na": [], "active_layers": 0}

    def award(key, fraction, why=None):
        fraction = max(0.0, min(1.0, fraction))
        pts = W.get(key, 0) * fraction
        items[key] = pts
        if pts > 0 and why:
            notes_pos.append((pts, why))
        elif pts == 0 and why:
            notes_missing.append(why)
        return pts

    # ---------- فلاتر الإقصاء (Liquidity & Data) ----------
    quote_vol = bid = ask = None
    spread_pct = None
    if ticker:
        try:
            quote_vol = float(ticker.get("quoteVolume"))
        except (TypeError, ValueError):
            quote_vol = None
        try:
            bid, ask = float(ticker.get("bidPrice")), float(ticker.get("askPrice"))
            if bid > 0 and ask > 0:
                spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
        except (TypeError, ValueError):
            spread_pct = None
    if quote_vol is not None and quote_vol < EARLY_MIN_QUOTE_VOLUME:
        hard.append("low_liquidity")
    if spread_pct is None:
        na.append("spread")
    elif spread_pct > EARLY_MAX_SPREAD_PCT:
        hard.append("wide_spread")
    if n < EARLY_MIN_CANDLES or sum(1 for v in vols[-10:] if v <= 0) >= 3:
        hard.append("incomplete_data")
    if atrp > EARLY_MAX_ATR_PCT:
        hard.append("extreme_volatility")
    ranges20 = [highs[j] - lows[j] for j in range(max(1, n - 20), n)]
    if sum(1 for x in ranges20 if x >= EARLY_ANOMALY_ATR_MULT * atr) >= 2:
        hard.append("erratic_candles")

    # Pump / حركة انفجارية حدثت فعلًا
    chg24 = (price / closes[i - 24] - 1) * 100 if i >= 24 else 0.0
    chg72 = (price / closes[i - 72] - 1) * 100 if i >= 72 else 0.0
    if chg24 >= EARLY_PUMP_PCT_24 or chg72 >= EARLY_PUMP_PCT_72:
        hard.append("pump_already_happened")

    W_RNG = EARLY_RANGE_WINDOW
    if n <= W_RNG + 5:
        hard.append("incomplete_data")
        return _early_result(items, hard, send_block, [], [], [], na, {}, None, price, atr, 0.0)

    rvol_now = vols[i] / ind["vol_avg"][i] if ind["vol_avg"][i] else 0.0
    last_gain_atr = (closes[i] - closes[i - 1]) / atr
    if last_gain_atr >= 3.0 and rvol_now >= 2.5:
        hard.append("explosive_candle_started")

    hh = max(highs[i - W_RNG + 1:i + 1])
    ll = min(lows[i - W_RNG + 1:i + 1])
    from_base_atr = (price - ll) / atr
    if from_base_atr > EARLY_MAX_FROM_BASE_ATR:
        hard.append("far_from_base")

    # ---------- Accumulation ----------
    rng_atr = (hh - ll) / atr
    drift_atr = abs(closes[i] - closes[i - W_RNG]) / atr
    pos_in_range = (price - ll) / (hh - ll) if hh > ll else 0.5
    consolidation = rng_atr <= EARLY_RANGE_MAX_ATR and drift_atr <= 3.5 and pos_in_range >= 0.4
    award("price_consolidation", 1.0 if consolidation else 0.0, "Price consolidation (نطاق ضيق بدون انهيار)")

    obv_v = ind["obv"]
    half = W_RNG // 2
    obv_hl = (min(obv_v[i - half + 1:i + 1]) > min(obv_v[i - W_RNG + 1:i - half + 1])) and obv_v[i] > obv_v[i - half]
    obv_up_sideways = obv_v[i] > obv_v[i - W_RNG] and drift_atr <= 3.5
    if obv_hl and consolidation:
        obv_frac = 1.0
    elif obv_hl or obv_up_sideways:
        obv_frac = 0.8
    elif obv_v[i] > obv_v[i - 10]:
        obv_frac = 0.4
    else:
        obv_frac = 0.0
    award("obv_bullish", obv_frac, "OBV يصنع Higher Lows / يرتفع بينما السعر جانبي" if obv_frac >= 0.8 else
          ("OBV صاعد جزئيًا" if obv_frac > 0 else "OBV غير داعم"))

    mfm = [((closes[j] - lows[j]) - (highs[j] - closes[j])) / (highs[j] - lows[j]) if highs[j] > lows[j] else 0.0
           for j in range(n)]
    ad = []
    run = 0.0
    for j in range(n):
        run += mfm[j] * vols[j]
        ad.append(run)
    if ad[i] > ad[i - 10] and ad[i] >= (_mean(ad[i - 10:i]) or ad[i]):
        ad_frac = 1.0
    elif ad[i] > ad[i - 10]:
        ad_frac = 0.5
    else:
        ad_frac = 0.0
    award("ad_bullish", ad_frac, "A/D يتحسن" if ad_frac else "A/D غير داعم")

    def _cmf(end, period=20):
        vs = sum(vols[end - period + 1:end + 1])
        return sum(mfm[j] * vols[j] for j in range(end - period + 1, end + 1)) / vs if vs else 0.0
    cmf_now, cmf_prev = _cmf(i), _cmf(i - 5)
    if cmf_now > 0 and cmf_now > cmf_prev:
        cmf_frac = 1.0
    elif cmf_now > 0 or (cmf_now > cmf_prev and cmf_now > -0.1):
        cmf_frac = 0.6
    else:
        cmf_frac = 0.0
    award("cmf_positive", cmf_frac, "CMF موجب/يتحسن" if cmf_frac else "CMF غير داعم")

    tb = ind.get("taker_buy")
    cvd_trend = None
    if tb and all(x is not None for x in tb[-25:]):
        delta = [2 * tb[j] - vols[j] for j in range(n)]
        cvd = []
        run = 0.0
        for d in delta:
            run += d
            cvd.append(run)
        up10, up20 = cvd[i] - cvd[i - 10] > 0, cvd[i] - cvd[i - 20] > 0
        cvd_trend = "up" if (up10 and up20) else ("mixed" if up10 else "down")
        if up10 and up20 and (consolidation or drift_atr <= 2.0):
            cvd_frac = 1.0
        elif up10 and up20:
            cvd_frac = 0.6
        else:
            cvd_frac = 0.0
        award("cvd_improving", cvd_frac, "CVD يتحسن بينما السعر جانبي" if cvd_frac else None)
    else:
        na.append("CVD")

    # ---------- Compression ----------
    bw = [((ind["bb_upper"][j] - ind["bb_lower"][j]) / closes[j]) if ind["bb_upper"][j] is not None else None
          for j in range(n)]
    bw_hist = bw[max(0, i - 99):i + 1]
    bw_now = bw[i]
    bw_rank = _percentile_rank(bw_hist, bw_now) if bw_now is not None else None
    if bw_rank is None:
        na.append("BB Width")
        bb_frac = 0.0
    else:
        bb_frac = 1.0 if bw_rank <= 0.20 else (0.7 if bw_rank <= 0.30 else (0.3 if bw_rank <= 0.40 else 0.0))
    award("bb_width_low", bb_frac, "BB Width منخفض (Compression)" if bb_frac else "لا يوجد Compression واضح")

    recent_bw = [x for x in bw[i - 9:i + 1] if x is not None]
    expansion_start = False
    if recent_bw and bw_now is not None and bw[i - 1] is not None:
        rmin = min(recent_bw)
        rrank = _percentile_rank(bw_hist, rmin)
        if rrank is not None and rrank <= 0.30 and rmin * 1.04 <= bw_now <= rmin * 1.6 and bw_now >= bw[i - 1] * 0.98:
            expansion_start = True
    award("bb_expansion_start", 1.0 if expansion_start else 0.0,
          "بداية Expansion تدريجي بعد الانضغاط" if expansion_start else "Expansion لم يبدأ بعد")

    atr_ser = _atr_series(highs, lows, closes)
    base_atr = _mean(atr_ser[max(0, i - 50):i - 5])
    atr_ratio = (atr_ser[i] / base_atr) if (base_atr and atr_ser[i]) else None
    if atr_ratio is None:
        na.append("ATR compression")
        atrc_frac = 0.0
    else:
        atrc_frac = 1.0 if atr_ratio <= 0.75 else (0.6 if atr_ratio <= 0.9 else 0.0)
    award("atr_compression", atrc_frac, "ATR Compression" if atrc_frac else None)

    # ---------- Structure ----------
    sl_idx = _swing_lows(lows, i, 60, 3)
    hl_level = 0
    if len(sl_idx) >= 3 and lows[sl_idx[-1]] > lows[sl_idx[-2]] + 0.1 * atr and lows[sl_idx[-2]] > lows[sl_idx[-3]] + 0.1 * atr:
        hl_level = 2
    elif len(sl_idx) >= 2 and lows[sl_idx[-1]] > lows[sl_idx[-2]] + 0.1 * atr:
        hl_level = 1
    award("higher_lows", {2: 1.0, 1: 0.7, 0: 0.0}[hl_level], "Higher Lows" if hl_level else "لا Higher Lows واضحة")
    support = lows[sl_idx[-1]] if sl_idx else ll
    near_support = (price - support) / atr <= 1.5

    rng10 = max(highs[i - 9:i + 1]) - min(lows[i - 9:i + 1])
    rng10_prev = max(highs[i - 19:i - 9]) - min(lows[i - 19:i - 9])
    tight_ratio = rng10 / rng10_prev if rng10_prev > 0 else None
    tr_frac = 0.0 if tight_ratio is None else (1.0 if tight_ratio <= 0.75 else (0.55 if tight_ratio <= 0.9 else 0.0))
    award("tightening_range", tr_frac, "Tightening Range (النطاق يضيق)" if tr_frac else None)

    res = early_resistances(ind, i)
    r1 = res[0] if res else None
    r2 = res[1] if len(res) > 1 else None
    dist1 = (r1 - price) / price * 100 if r1 else None
    if dist1 is None:
        nr_frac = 0.0
        na.append("Resistance (لا مقاومة فوق السعر ضمن النافذة)")
    else:
        nr_frac = 1.0 if dist1 <= 1.0 else (0.75 if dist1 <= 2.0 else (0.4 if dist1 <= EARLY_NEAR_RES_MAX_PCT else 0.0))
    award("near_resistance", nr_frac, f"قريب من المقاومة ({dist1:.2f}%)" if nr_frac and dist1 is not None else "بعيد عن المقاومة")

    touches = 0
    if r1:
        tol = max(0.003, 0.35 * atr / price)
        last_touch = -10
        for j in range(max(1, i - 30), i + 1):
            if highs[j] >= r1 * (1 - tol) and closes[j] < r1 and j - last_touch >= 3:
                touches += 1
                last_touch = j
    award("resistance_tests", 1.0 if touches >= 3 else (0.7 if touches == 2 else 0.0),
          f"اختبارات متكررة للمقاومة ({touches})" if touches >= 2 else "لا اختبارات متكررة للمقاومة")

    # Failed Breakdown / Spring
    spring = False
    prior_sw = _swing_lows(lows, i - 6, 40, 3)
    if prior_sw:
        sup = lows[prior_sw[-1]]
        for j in range(max(0, i - 6), i + 1):
            if lows[j] < sup - 0.05 * atr and closes[j] >= sup and price > sup:
                spring = True
                break
    award("failed_breakdown", 1.0 if spring else 0.0, "Failed Breakdown / Spring" if spring else None)

    ema50 = ind["ema50"]
    ema20 = ind["ema20"]
    reclaim = False
    for j in range(max(1, i - 7), i + 1):
        if closes[j - 1] < ema50[j - 1] and closes[j] >= ema50[j] and price >= ema50[i]:
            reclaim = True
            break
    award("reclaim_level", 1.0 if reclaim else 0.0, "Reclaim لـEMA50" if reclaim else None)

    avg_rng5 = _mean([highs[j] - lows[j] for j in range(i - 4, i + 1)])
    avg_rng20 = _mean([highs[j] - lows[j] for j in range(i - 24, i - 4)])
    tight_candles = bool(avg_rng20 and avg_rng5 <= 0.7 * avg_rng20 and dist1 is not None
                         and dist1 <= EARLY_NEAR_RES_MAX_PCT)
    award("tight_candles", 1.0 if tight_candles else 0.0, "Tight candles قرب المقاومة" if tight_candles else None)

    engulf = False
    if opens and near_support and i >= 1:
        engulf = (closes[i] > opens[i] and closes[i - 1] < opens[i - 1]
                  and closes[i] >= opens[i - 1] and opens[i] <= closes[i - 1])

    # ---------- Momentum ----------
    rv = ind["rsi"]
    rsi_now = rv[i]
    rsi_improving = False
    reclaim50 = False
    if rsi_now is not None and rv[i - 5] is not None:
        rmin = min(x for x in rv[i - 10:i + 1] if x is not None)
        rsi_improving = rsi_now - rv[i - 5] >= 2 and rsi_now - rmin >= 3 and rsi_now < 68
        for j in range(max(1, i - 7), i + 1):
            if rv[j - 1] is not None and rv[j] is not None and rv[j - 1] < 50 <= rv[j] and rsi_now >= 50:
                reclaim50 = True
    if rsi_now is None:
        na.append("RSI")
        rsi_frac = 0.0
    elif rsi_now >= 70:
        rsi_frac = 0.0
        risks.append("RSI مرتفع (متأخر)")
    else:
        rsi_frac = (1.0 if (rsi_improving and (reclaim50 or 40 <= rsi_now <= 62)) else (0.6 if rsi_improving else 0.0))
    award("rsi_improving", rsi_frac, "RSI يتحسن تدريجيًا" + (" + Reclaim 50" if reclaim50 else "") if rsi_frac
          else "RSI لا يؤكد تحسن الزخم")

    acc_pts = sum(items.get(k, 0) for k in EARLY_CATEGORIES["accumulation"])
    acc_support = acc_pts >= 0.3 * sum(W[k] for k in EARLY_CATEGORIES["accumulation"]) or near_support
    div = bool(r.get("divergence"))
    hidden = False
    sw = _swing_lows(closes, i, 40, 3)
    if len(sw) >= 2 and rv[sw[-1]] is not None and rv[sw[-2]] is not None:
        hidden = closes[sw[-1]] > closes[sw[-2]] and rv[sw[-1]] < rv[sw[-2]]
    if div and not acc_support:
        risks.append("Divergence بدون دعم/تراكم (لم تُحتسب)")
        div = False
    if hidden and not acc_support:
        hidden = False
    award("bullish_divergence", 1.0 if div else 0.0, "Bullish Divergence (RSI)" if div else None)
    award("hidden_bullish_divergence", 1.0 if hidden else 0.0, "Hidden Bullish Divergence" if hidden else None)

    hist = [(a - b) if (a is not None and b is not None) else None for a, b in zip(ind["macd"], ind["signal"])]
    h4 = hist[i - 3:i + 1]
    macd_frac = 0.0
    if all(x is not None for x in h4):
        rising3 = h4[1] >= h4[0] and h4[2] >= h4[1] and h4[3] >= h4[2] and h4[3] > h4[0]
        max_abs = max(abs(x) for x in hist[i - 19:i + 1] if x is not None) or 1e-12
        if rising3 and h4[3] <= 0.5 * max_abs:
            macd_frac = 1.0
        elif h4[3] > h4[1]:
            macd_frac = 0.55
    else:
        na.append("MACD")
    award("macd_hist_improving", macd_frac, "MACD Histogram يتحسن قبل التقاطع" if macd_frac else "MACD Histogram لا يتحسن")

    # ---------- Volume ----------
    base_vol = _mean(vols[i - 24:i - 4])
    rvol5 = (_mean(vols[i - 4:i + 1]) / base_vol) if base_vol else 0.0
    rvol5_prev = (_mean(vols[i - 9:i - 4]) / base_vol) if base_vol else 0.0
    if rvol5 >= 1.15 and rvol5 > rvol5_prev * 1.05 and rvol5 <= 3.0:
        rv_frac = 1.0
    elif rvol5 >= 1.05 and rvol5 > rvol5_prev and rvol5 <= 3.0:
        rv_frac = 0.5
    else:
        rv_frac = 0.0
    award("rvol_improving", rv_frac, f"Volume buildup تدريجي (RVOL5={rvol5:.2f})" if rv_frac else "لا Volume buildup")

    up_vol = sum(vols[j] for j in range(i - 9, i + 1) if closes[j] > closes[j - 1])
    dn_vol = sum(vols[j] for j in range(i - 9, i + 1) if closes[j] < closes[j - 1])
    ud_ratio = up_vol / dn_vol if dn_vol > 0 else (2.0 if up_vol > 0 else 1.0)
    vc_frac = 1.0 if (ud_ratio >= 1.25 and rvol_now >= 1.0) else (0.5 if ud_ratio >= 1.1 else 0.0)
    award("volume_confirmation", vc_frac, f"حجم الصعود يتفوق على حجم الهبوط ({ud_ratio:.2f}x)" if vc_frac
          else "تأكيد الحجم غير متحقق")

    vp = early_volume_profile(ind, i)
    vp_frac = 0.0
    if vp:
        if price >= vp["poc"] and (price - vp["poc"]) / atr <= 3.0:
            vp_frac = 1.0
        award("volume_profile_support", vp_frac, "السعر فوق POC (دعم حجمي)" if vp_frac else None)
    else:
        na.append("Volume Profile")

    # ---------- Market (مع سياق BTC/ETH والفريمات) ----------
    mctx = market_ctx
    if mctx is None and fetch:
        mctx = EARLY_MARKET_CTX
    btc_pen = 0.0
    if mctx:
        if mctx["btc_state"] in ("bullish", "neutral"):
            award("market_ok", 1.0)
        else:
            award("market_ok", 0.0, "السوق العام هابط")
            btc_pen = EARLY_BTC_BEAR_PENALTY if mctx["strong_bear"] else (
                EARLY_BTC_BEAR_PENALTY * 0.3 if mctx["btc_state"] == "bearish" else 0.0)
            if btc_pen:
                risks.append("السوق العام هابط" + (" بقوة" if mctx["strong_bear"] else ""))
        coin_ret = (price / closes[i - 24] - 1) * 100 if i >= 24 else None
        if symbol != "BTCUSDT" and coin_ret is not None:
            diff = coin_ret - mctx["btc_ret"]
            if diff >= 1.0 or (mctx["btc_ret"] <= -1.0 and coin_ret >= -0.5):
                rs_frac = 1.0
            elif diff > 0:
                rs_frac = 0.4
            else:
                rs_frac = 0.0
            award("relative_strength", rs_frac, f"قوة نسبية مقابل BTC ({diff:+.2f}%)" if rs_frac else
                  f"أضعف من BTC ({diff:+.2f}%)")
            rs_val = diff
        else:
            rs_val = None
            na.append("Relative Strength")
    else:
        na.append("Market context (BTC/ETH)")
        rs_val = None

    def _pre_total():
        return sum(items.values()) - btc_pen
    if fetch and not hard and _pre_total() >= EARLY_FETCH_MIN:
        if htf_ctx is None:
            htf_ctx = early_htf_context(symbol, interval)
        if ltf_ctx is None and _pre_total() >= EARLY_LEVEL_WATCH - 5:
            ltf_ctx = early_ltf_context(symbol, interval)
    htf_trend = htf_ctx["trend"] if htf_ctx else None
    htf_pen = 0.0
    if htf_ctx is None:
        na.append("HTF")
    elif htf_trend == "bullish":
        award("htf_agree", 1.0, f"اتجاه {htf_ctx['tf']} صاعد (توافق الفريم الأعلى)")
    elif htf_trend == "neutral":
        award("htf_agree", 0.4, None)
    else:
        award("htf_agree", 0.0, None)
        htf_pen = 8.0
        risks.append(f"تعارض مع الفريم الأعلى {htf_ctx['tf']} (هابط)")
        if EARLY_BLOCK_HTF_CONFLICT:
            send_block.append("htf_conflict")
    if ltf_ctx is None:
        na.append("LTF")
    else:
        award("ltf_timing", 1.0 if ltf_ctx["timing_ok"] else 0.0,
              f"توقيت {ltf_ctx['tf']} داعم" if ltf_ctx["timing_ok"] else None)

    # ---------- التجميع والفلاتر ----------
    cats = {c: sum(items.get(k, 0) for k in keys) for c, keys in EARLY_CATEGORIES.items()}
    cat_max = {c: sum(W[k] for k in keys) for c, keys in EARLY_CATEGORIES.items()}
    cats["market"] -= (btc_pen + htf_pen)
    active = {c: (cats[c] >= EARLY_ACTIVE_FRACTION * cat_max[c]) for c in cats if cat_max[c] > 0}
    # طبقة Market لا تُحتسب بين الطبقات المؤكِّدة (يسهل تحققها في سوق صاعد وتمنح نقاطًا شبه مجانية)
    active_layers = sum(1 for c, v in active.items() if v and c != "market")
    raw = max(0.0, sum(cats.values()))

    # لا إشارة بدون تأكيد سعري: هيكل أو تراكم (لا يكفي RSI/MACD/Volume وحدها)
    if not (active.get("structure") or active.get("accumulation")):
        raw = min(raw, EARLY_LEVEL_WATCH - 1)
        risks.append("لا تأكيد سعري (هيكل/تراكم) — الزخم وحده لا يكفي")
    # الحجم الانفجاري بدون هيكل = ليس Buildup
    if rvol_now >= EARLY_SPIKE_RVOL:
        send_block.append("volume_spike")
        if not active.get("structure"):
            hard.append("volume_spike_without_structure")
    # امتداد مفرط / اختراق نطاق فعلي (ليست مبكرة)
    if overextended(ind, i, True):
        send_block.append("overextended")
    if price > max(highs[i - W_RNG:i]):
        send_block.append("already_breaking_out")
    # طبقات التوافق
    # شروط التوافق: >= EARLY_MIN_ACTIVE_LAYERS طبقات تحليلية فعّالة + تراكم فعلي + هيكل داعم
    # (انضغاط/زخم/حجم وحدها لا تكفي، ولا Bollinger Squeeze وحده)
    if active_layers < EARLY_MIN_ACTIVE_LAYERS or not active.get("accumulation") or not active.get("structure"):
        send_block.append("insufficient_confluence")

    # Reward/Room
    upside_target, upside_pct = None, None
    if r1 is not None:
        if dist1 <= EARLY_NEAR_RES_MAX_PCT:
            upside_target = r2   # المقاومة الأولى هي التي نتوقع كسرها: المجال = للمقاومة التي تليها
        else:
            upside_target = r1
        if upside_target is not None:
            upside_pct = (upside_target - price) / price * 100
    need_up = max(EARLY_MIN_UPSIDE_PCT, EARLY_MIN_UPSIDE_ATR * atrp)
    room_ok = True if upside_pct is None else (upside_pct >= need_up)
    if not room_ok:
        send_block.append("insufficient_room")
        risks.append(f"مجال الصعود قبل المقاومة التالية {upside_pct:.2f}% < المطلوب {need_up:.2f}%")

    if dist1 is not None and dist1 < 0.3 and r2 is not None and (r2 - r1) / r1 * 100 < 1.0:
        risks.append("مقاومة كثيفة فوق السعر مباشرة")
    if rvol5 < 1.0:
        risks.append("الحجم لم يرتفع بعد")
    if drift_atr > 2.5:
        risks.append("السعر ابتعد نسبيًا عن منطقة التجميع")

    # المخاطر الدائمة: بيانات المشتقات غير متاحة عبر Binance Spot API
    na.append("Open Interest/Funding")

    score = 0.0 if hard else raw
    rank = early_level_rank(score)
    eff_send = rank >= 2 and not send_block and not hard

    # لو الإشارة غير قابلة للإرسال بسبب فلتر إرسال نخفّض مستواها الفعلي إلى WATCH كحد أقصى
    # (تبقى مسجّلة للمراقبة فقط)، مع الاحتفاظ بالـScore الحقيقي للعرض والتتبع.
    metrics = {
        "rsi": rsi_now, "macd_hist": hist[i], "bb_width_pct": (bw_now * 100 if bw_now is not None else None),
        "bb_width_rank": bw_rank, "atr": atr, "atr_pct": atrp, "rvol": rvol_now, "rvol5": rvol5,
        "obv_trend": "up" if obv_v[i] > obv_v[i - 10] else "down",
        "obv_higher_lows": bool(obv_hl), "cmf": cmf_now,
        "ad_trend": "up" if ad[i] > ad[i - 10] else "down", "cvd_trend": cvd_trend,
        "relative_strength_vs_btc": rs_val, "htf_trend": htf_trend,
        "htf_tf": (htf_ctx or {}).get("tf"), "ltf_tf": (ltf_ctx or {}).get("tf"),
        "resistance": r1, "resistance_2": r2, "distance_to_resistance_pct": dist1,
        "upside_pct": upside_pct, "need_upside_pct": need_up, "support": support,
        "chg24_pct": chg24, "spread_pct": spread_pct, "poc": (vp or {}).get("poc"),
        "lvn_above": (vp or {}).get("lvn_above"), "wyckoff_spring": spring, "bullish_engulfing_at_support": engulf,
        "market_state": (mctx or {}).get("btc_state"),
    }

    plan = None
    if eff_send:
        entry = price
        sl = entry - atr * EARLY_SL_ATR_MULT
        risk = entry - sl
        tp1 = entry + risk
        if upside_target is not None and tp1 >= upside_target:
            tp1 = upside_target
        plan = {"entry": entry, "sl": sl, "tps": [tp1]}

    notes_pos.sort(key=lambda x: x[0], reverse=True)
    reasons = [w for _, w in notes_pos[:5]]
    missing = [m for m in notes_missing if m][:5]
    if r1:
        nxt = f"إغلاق شمعة {interval or INTERVAL} فوق {r1:.6g} مع RVOL ≥ {BREAKOUT_VOL_MULT} (ويفضّل Retest ناجح)"
    else:
        nxt = f"إغلاق شمعة {interval or INTERVAL} فوق أعلى قمة {W_RNG} شمعة ({hh:.6g}) مع RVOL ≥ {BREAKOUT_VOL_MULT}"
    inval = f"إغلاق شمعة {interval or INTERVAL} تحت الدعم {support:.6g} (- 0.25×ATR) أو هبوط الـScore تحت {EARLY_LEVEL_WATCH:g}"

    out = _early_result(items, hard, send_block, reasons, missing, risks, na, metrics, plan, price, atr, score,
                        cats=cats, cat_max=cat_max, active_layers=active_layers, raw=raw, rank=rank, eff_send=eff_send)
    out["next_trigger"] = nxt
    out["invalidation"] = inval
    return out


def _early_result(items, hard, send_block, reasons, missing, risks, na, metrics, plan, price, atr, score,
                  cats=None, cat_max=None, active_layers=0, raw=None, rank=None, eff_send=False):
    rank = early_level_rank(score) if rank is None else rank
    return {
        "version": EARLY_ENGINE_VERSION, "score": round(score, 2), "raw_score": round(raw if raw is not None else score, 2),
        "level_rank": rank, "level": EARLY_LEVEL_NAMES[rank], "sendable": bool(eff_send),
        "blocked": list(hard), "send_blocked": list(send_block),
        "cats": {k: round(v, 2) for k, v in (cats or {}).items()}, "cat_max": cat_max or {},
        "items": {k: round(v, 2) for k, v in items.items()}, "metrics": metrics, "plan": plan,
        "reasons": reasons, "missing": missing, "risks": risks, "na": na, "active_layers": active_layers,
        "next_trigger": "", "invalidation": "",
    }


# ---------------- تطور الإشارة (Signal Evolution) ----------------

def early_effective_rank(e):
    """مستوى الإشارة الفعلي للتتبع: فلتر إرسال فعّال يخفّض المستوى إلى WATCH كحد أقصى."""
    if not e:
        return 0
    rank = e["level_rank"]
    if rank >= 2 and not e.get("sendable"):
        return 1 if not e.get("blocked") else 0
    return rank


def early_evolution_update(prev_state, results, eligible_symbols, now=None):
    """
    يحدّث حالة كل إشارة مبكرة عبر الزمن ويرجع (new_state, events). لا يُرسل شيئًا بنفسه.
    الأحداث: NEW (أول إرسال) | UPGRADE | UPDATE | DOWNGRADE | INVALIDATED.
    لا حدث (لا رسالة) عند عدم وجود تغير مهم — فلا تتكرر نفس الإشارة كل دورة.
    """
    now = time.time() if now is None else now
    prev_state = prev_state or {}
    new_state, events = {}, []
    by_sym = {r["symbol"]: r for r in results if r.get("early_v2")}

    for sym, st in prev_state.items():
        if sym in by_sym:
            continue
        # الرمز لم يُحلَّل هذه الدورة (خارج القائمة المختارة / خطأ): نحتفظ بالحالة بعدّاد غياب
        st = dict(st)
        st["missed"] = st.get("missed", 0) + 1
        too_old = now - st.get("last_ts", now) > EARLY_STATE_TTL_HOURS * 3600
        in_cooldown = st.get("status") == "invalidated" and now < st.get("cooldown_until", 0)
        if too_old or (st["missed"] >= EARLY_MISSED_EXPIRE and not in_cooldown):
            continue
        new_state[sym] = st

    for sym, r in by_sym.items():
        e = r["early_v2"]
        score = e["score"]
        eff = early_effective_rank(e)
        st = prev_state.get(sym)
        st = dict(st) if st else None

        if st and st.get("status") == "invalidated":
            if now < st.get("cooldown_until", 0):
                st["missed"] = 0
                st["last_ts"] = now
                new_state[sym] = st
                continue
            st = None   # انتهى التبريد: إشارة جديدة كليًا

        if st is None:
            if eff < 1:
                continue
            st = {"signal_id": f"{sym}-{time.strftime('%Y%m%d%H%M%S', time.gmtime(now))}", "symbol": sym,
                  "first_ts": now, "last_ts": now, "score": score, "prev_score": None, "history": [score],
                  "sent_rank": 0, "sent_score": None, "msg_id": None, "status": "watch", "low_cycles": 0, "missed": 0}
            if eff >= EARLY_SEND_MIN_RANK and sym in eligible_symbols:
                st.update({"sent_rank": eff, "sent_score": score, "status": "active", "sent_ts": now})
                events.append({"kind": "NEW", "symbol": sym, "r": r, "state": st})
            new_state[sym] = st
            continue

        # إشارة قائمة: تحديث الـScore والتاريخ
        st["prev_score"] = st.get("score")
        st["score"] = score
        st["last_ts"] = now
        st["missed"] = 0
        st["history"] = (st.get("history", []) + [score])[-8:]
        sent_rank = st.get("sent_rank", 0)

        if sent_rank == 0:
            if eff >= EARLY_SEND_MIN_RANK and sym in eligible_symbols:
                st.update({"sent_rank": eff, "sent_score": score, "status": "active", "sent_ts": now})
                events.append({"kind": "NEW", "symbol": sym, "r": r, "state": st})
            if eff < 1:
                st["low_cycles"] = st.get("low_cycles", 0) + 1
                if st["low_cycles"] >= 3:
                    continue
            else:
                st["low_cycles"] = 0
            new_state[sym] = st
            continue

        sent_score = st.get("sent_score") or score
        if eff == 0:
            reason = (e["blocked"] or ["score_below_watch"])[0]
            events.append({"kind": "INVALIDATED", "symbol": sym, "r": r, "state": st, "reason": reason})
            st.update({"status": "invalidated", "cooldown_until": now + EARLY_COOLDOWN_HOURS * 3600,
                       "sent_rank": 0, "sent_score": None})
        elif eff > sent_rank:
            events.append({"kind": "UPGRADE", "symbol": sym, "r": r, "state": st, "from_rank": sent_rank})
            st.update({"sent_rank": eff, "sent_score": score})
        elif eff < sent_rank and (sent_score - score >= EARLY_DOWNGRADE_DELTA or eff <= 1):
            events.append({"kind": "DOWNGRADE", "symbol": sym, "r": r, "state": st, "from_rank": sent_rank})
            st.update({"sent_rank": eff, "sent_score": score})
        elif eff == sent_rank and abs(score - sent_score) >= EARLY_UPDATE_DELTA:
            events.append({"kind": "UPDATE", "symbol": sym, "r": r, "state": st})
            st.update({"sent_score": score})
        new_state[sym] = st

    return new_state, events



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

        htf_checked, htf_aligned = False, None
        htf_official_bullish = None   # تأكيد الفريم الأعلى الصارم للإشارة الرسمية (None = غير متاح/فشل الجلب)
        # النطاق القديم لحساب htf_aligned (يخدم باقي الإشارات + النموذج التكيفي) يبقى كما هو بالضبط؛
        # لكن الفريم الأعلى يُجلب الآن أيضًا لأي عملة اجتازت كل شروط الرسمية محليًا (لأنه شرط إلزامي لها).
        legacy_htf_scope = abs(r["score"]) >= 0.5
        official_local_ok = official_confluence(ind, last, r, True, atrp)["ok"]
        if legacy_htf_scope or official_local_ok:
            htf = HTF_MAP.get(interval)
            if htf:
                try:
                    # تأكيد أقوى من مجرد EMA7>EMA14: نتحقق من هيكل EMA20/EMA50 + ميل EMA50
                    # + موقع السعر منها + ADX>عتبة (اتجاه فعلي قوي وليس تقاطعًا سطحيًا)
                    htf_klines = fetch_klines_cached(symbol, htf, 100)
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
                        # الفريم الأعلى للإشارة الرسمية: نفس البنية لكن بعتبة ADX الرسمية (25) وصاعد فقط
                        htf_official_bullish = bool(
                            htf_ema20[-1] > htf_ema50[-1]
                            and htf_ema50_rising
                            and htf_closes[-1] > htf_ema50[-1]
                            and htf_adx_last is not None and htf_adx_last > OFFICIAL_ADX_MIN
                        )
                        if legacy_htf_scope:
                            htf_aligned = htf_bullish_strict if r["trend_up"] else htf_bearish_strict
                            htf_checked = True
                except Exception:
                    pass

        # إشارات مبكرة (انضغاط تقلب / تراكم صامت) — مستقلة عن الدرجة الرسمية، تُحسب دائمًا
        # للعرض، وتُستخدم لاحقًا فقط لعملات لم تصل بعد لإشارة شراء كاملة
        squeeze = volatility_squeeze(ind["bb_upper"], ind["bb_lower"], ind["closes"])
        # فلتر اتجاه إضافي على Squeeze (2026-09-14): انضغاط التقلب وحده لا يحدد جهة الحركة
        # المرتقبة، فلا يُعتبر إشارة إلا لو الاتجاه السريع صاعد (EMA7>EMA14) وMACD صاعد أيضًا
        # — غير هيك نرفضه حتى لو كان الانضغاط نفسه موجودًا تقنيًا.
        if squeeze and not (r["trend_up"] and r["macd_bull"]):
            squeeze = False
        accumulation = silent_accumulation(ind["closes"], ind["vols"], ind["obv"])

        # قرار الإشارة الرسمية (confluence_v1): كل الشروط إلزامية — انظر official_confluence().
        # بونص Squeeze أُزيل من الرسمية (Squeeze ما زال يخدم الإشارات المبكرة فقط). final_score = درجة
        # الـconfluence، وهي أقل من 1.5 دائمًا عند الفشل فتبقى بوابة الإشارة المبكرة (early_gate_score < 1.5) سليمة.
        official = official_confluence(ind, last, r, htf_official_bullish, atrp)
        final_score = official["score"]
        score_before_squeeze_bonus = final_score

        # الإشارة المبكرة v2: نظام Confluence بالنقاط (Accumulation/Compression/Structure/Momentum/
        # Volume/Market) مع فلاتر إقصاء وإرسال — انظر early_engine(). منفصلة عن Breakout/Explosive.
        early_entry = early_sl = None
        early_tps = []
        early_confidence = None
        momentum = momentum_strength(ind["macd"], ind["signal"], ind["rsi"], last)
        early_v2 = early_engine(ind, last, r, atrp, ticker=t, symbol=symbol, interval=interval, fetch=True)
        _cats, _cmax = early_v2.get("cats") or {}, early_v2.get("cat_max") or {}
        if _cmax:
            # الحقول التشخيصية القديمة تعكس الآن الطبقات الجديدة (لتبقى التقارير/النموذج التكيفي متسقة)
            squeeze = _cats.get("compression", 0) >= EARLY_ACTIVE_FRACTION * _cmax["compression"]
            accumulation = _cats.get("accumulation", 0) >= EARLY_ACTIVE_FRACTION * _cmax["accumulation"]
            momentum = _cats.get("momentum", 0) >= EARLY_ACTIVE_FRACTION * _cmax["momentum"]
        if early_v2.get("sendable") and early_v2.get("plan"):
            early_entry = early_v2["plan"]["entry"]
            early_sl = early_v2["plan"]["sl"]
            early_tps = early_v2["plan"]["tps"]
            early_confidence = early_confidence_label(early_v2["level_rank"])

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
                breakout_sl = breakout_entry - atrv * BREAKOUT_SL_ATR_MULT
                risk = breakout_entry - breakout_sl
                # هدف واحد فقط: TP1 = 1R، مع احترام أقرب مقاومة إن كانت قبل TP1.
                breakout_tps = [breakout_entry + risk]
                resistance = r.get("resistance")
                if resistance and breakout_tps[0] >= resistance:
                    breakout_tps[0] = resistance

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
                # هدف واحد فقط: TP1 = 1R، مع احترام أقرب مقاومة إن كانت قبل TP1.
                experimental_tps = [experimental_entry + risk]
                resistance = r.get("resistance")
                if resistance and experimental_tps[0] >= resistance:
                    experimental_tps[0] = resistance

        # خطة دخول (شراء فقط — السوق الفوري لا يدعم فتح صفقة بيع مكشوفة): من official_confluence فقط.
        # وقف الخسارة بمضاعف ATR + هدف TP1 + حجم المركز من نسبة المخاطرة؛ بلا خطة لو فشل أي شرط.
        entry, sl, tps = official["entry"], official["sl"], official["tps"]

        # توحيد الحقول التشخيصية مع تعريف الرسمية الجديد: عند نجاح الإشارة تعكس هذه الحقول ما تحقق فعليًا
        # (مثلاً htf_aligned القديمة تعتمد على EMA7/14 وقد تخالف الحكم الرسمي) كي تبقى سياسة regime
        # strict والنموذج التكيفي متسقَين مع القرار الفعلي. عند الفشل تبقى القيم القديمة كما هي لباقي الإشارات.
        diag = {
            "htf_aligned": htf_aligned, "vol_confirm": r["vol_confirm"], "ranging": r["ranging"],
            "di_confirm": r["di_confirm"], "momentum_agree": r["momentum_agree"],
            "obv_confirm": r["obv_confirm"], "extended": r["extended"],
        }
        if official["ok"]:
            diag.update({
                "htf_aligned": True, "vol_confirm": True, "ranging": False, "di_confirm": True,
                "momentum_agree": True, "obv_confirm": bool(official["obv_rising"]), "extended": False,
            })

        return {
            "symbol": symbol,
            "price": ind["closes"][last],
            "change_pct": float(t["priceChangePercent"]),
            "score": final_score,
            "early_gate_score": score_before_squeeze_bonus,
            "trend_up": r["trend_up"],
            "vol_confirm": diag["vol_confirm"],
            "atr_pct": atrp,
            "persistent": persistent,
            "htf_checked": htf_checked,
            "htf_aligned": diag["htf_aligned"],
            "ranging": diag["ranging"],
            "di_confirm": diag["di_confirm"],
            "momentum_agree": diag["momentum_agree"],
            "divergence": r["divergence"],
            "near_resistance": r["near_resistance"],
            "resistance_levels": resistance_levels(ind["highs"][:last + 1], ind["closes"][:last + 1]),
            "obv_confirm": diag["obv_confirm"],
            "extended": diag["extended"],
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
            "official_ok": official["ok"],
            "official_failed": official["failed"],
            "official_engine": OFFICIAL_ENGINE_VERSION,
            "size_pct": official["size_pct"], "risk_pct": official["risk_pct"],
            "adx_val": r["adx_val"], "vol_ratio": official["vol_ratio"],
            "early_entry": early_entry, "early_sl": early_sl, "early_tps": early_tps,
            "early_confidence": early_confidence,
            "early_v2": early_v2,
            "early_score": early_v2.get("score"),
            "early_level": early_v2.get("level"),
            "breakout_entry": breakout_entry, "breakout_sl": breakout_sl, "breakout_tps": breakout_tps,
        }
    except Exception as e:
        print(f"[تخطي] {symbol}: {e}")
        if error_list is not None:
            error_list.append(symbol)
        return None


# ---------------- المسح الكامل (مرحلتين) ----------------

def run_scan(tickers=None):
    global EARLY_MARKET_CTX
    if tickers is None:
        tickers = fetch_ticker24h()
    try:
        EARLY_MARKET_CTX = early_market_context()
        if EARLY_MARKET_CTX:
            print(f"🌍 [early_v2] سياق السوق: BTC={EARLY_MARKET_CTX['btc_state']} ({EARLY_MARKET_CTX['btc_ret']:+.2f}%/24ش) "
                  f"ETH={EARLY_MARKET_CTX['eth_state']} | هبوط قوي={EARLY_MARKET_CTX['strong_bear']}")
    except Exception as _e:
        EARLY_MARKET_CTX = None
        print(f"⚠️ [early_v2] تعذّر بناء سياق السوق: {_e}")

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

def send_telegram(text, retries=2, reply_to=None):
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
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}
            if reply_to:
                payload["reply_to_message_id"] = reply_to
                payload["allow_sending_without_reply"] = True
            resp = requests.post(url, data=payload, timeout=15)
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


def format_tp_line(pos):
    """سطر مختصر لهدف TP1 المتحقق."""
    entry = pos["entry"]
    tp = pos["tps"][0]
    pct_gain = (tp - entry) / entry * 100
    return f"✅ تحقق TP1: {tp:.6g} (+{pct_gain:.2f}%)"


def build_progress_text(pos):
    """النص الأساسي قبل الإغلاق؛ يحتفظ بسطر تحقق TP1 إن وُجد."""
    base = pos.get("alert_text") or ""
    return base + ("\n\n" + format_tp_line(pos) if pos.get("tp_hit") else "")


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
        if r.get("tps"):
            lines.append(f"TP1: {r['tps'][0]:.6g}")
        lines.append(f"وقف الخسارة: {r['sl']:.6g}")
        if r.get("size_pct"):
            lines.append(f"الحجم المقترح: {r['size_pct']:.0f}% من الحساب (مخاطرة {r['risk_pct']:g}%)")
    else:
        # السوق الفوري لا يدعم فتح صفقة بيع مكشوفة — فلا توجد خطة دخول لإشارات "تجنب شراء"
        lines.append("لا توجد خطة دخول (تحذير فقط)")

    return "\n".join(lines)


def _fv(v, nd=2, suffix=""):
    """تنسيق رقم اختياري: None -> n/a (لا قيم مخترعة)."""
    if v is None:
        return "n/a"
    try:
        return f"{v:.{nd}f}{suffix}"
    except (TypeError, ValueError):
        return str(v)


_EARLY_DOTS = {1: "👁", 2: "🔵", 3: "🟣", 4: "🟢"}
_EARLY_REASON_AR = {
    "score_below_watch": "هبط الـScore تحت حد WATCH",
    "low_liquidity": "السيولة أصبحت غير كافية",
    "wide_spread": "السبريد اتسع",
    "pump_already_happened": "حدث Pump فعلي (الحركة بدأت)",
    "explosive_candle_started": "شمعة انفجارية بدأت الحركة",
    "far_from_base": "السعر ابتعد كثيرًا عن منطقة التجميع",
    "extreme_volatility": "تقلب متطرف",
    "erratic_candles": "شموع شاذة/حركة عشوائية",
    "volume_spike_without_structure": "Volume spike بدون تأكيد هيكلي",
    "incomplete_data": "بيانات ناقصة",
}


def format_early_alert(r, st=None):
    """
    تقرير الإشارة المبكرة v2 (Pre-Breakout Probability Setup): ليست تنبؤًا بالانفجار.
    Score = مقياس توافق عوامل وليس نسبة نجاح.
    """
    e = r.get("early_v2") or {}
    m = e.get("metrics") or {}
    cats, cmax = e.get("cats") or {}, e.get("cat_max") or {}
    rank = e.get("level_rank", 2)
    sym = r["symbol"].replace("USDT", "/USDT")
    hist = (st or {}).get("history") or [e.get("score")]
    trend = " → ".join(f"{x:.0f}" for x in hist[-6:] if x is not None)

    def layer(name, label):
        return f"{label} {cats.get(name, 0):.0f}/{cmax.get(name, 0):.0f}"

    lines = [
        f"{_EARLY_DOTS.get(rank, '🔵')} {e.get('level', 'EARLY SETUP')}",
        "Pre-Breakout Probability Setup (Early Momentum / Accumulation)",
        f"{sym} | Price {r['price']:.6g} | TF {INTERVAL}"
        + (f" (HTF {m['htf_tf']}" if m.get("htf_tf") else " (HTF n/a")
        + (f" / LTF {m['ltf_tf']})" if m.get("ltf_tf") else " / LTF n/a)"),
        f"Score: {e.get('score', 0):.0f} (Score trend: {trend})",
        "Score = توافق عوامل وليس نسبة نجاح",
        "",
        " | ".join([layer("accumulation", "Accumulation"), layer("compression", "Compression"),
                    layer("structure", "Structure")]),
        " | ".join([layer("momentum", "Momentum"), layer("volume", "Volume"), layer("market", "Market")]),
        "",
        f"Nearest Resistance: {_fv(m.get('resistance'), 6)} (Distance {_fv(m.get('distance_to_resistance_pct'), 2, '%')})"
        if m.get("resistance") else "Nearest Resistance: n/a (لا مقاومة فوق السعر ضمن النافذة)",
        f"Support: {_fv(m.get('support'), 6)} | Upside to next resistance: {_fv(m.get('upside_pct'), 2, '%')}",
        f"RVOL: {_fv(m.get('rvol'))} (buildup5 {_fv(m.get('rvol5'))}) | RSI: {_fv(m.get('rsi'), 1)} | "
        f"MACD Hist: {_fv(m.get('macd_hist'), 6)}",
        f"BB Width: {_fv(m.get('bb_width_pct'), 2, '%')} (rank {_fv(m.get('bb_width_rank'), 2)}) | "
        f"ATR: {_fv(m.get('atr'), 6)} ({_fv(m.get('atr_pct'), 2, '%')})",
        f"OBV: {m.get('obv_trend', 'n/a')}{' (Higher Lows)' if m.get('obv_higher_lows') else ''} | "
        f"CMF: {_fv(m.get('cmf'), 3)} | A/D: {m.get('ad_trend', 'n/a')} | CVD: {m.get('cvd_trend') or 'n/a'}",
        f"Relative Strength vs BTC: {_fv(m.get('relative_strength_vs_btc'), 2, '%')} | "
        f"Higher Timeframe Trend: {m.get('htf_trend') or 'n/a'}",
    ]
    if r.get("early_entry") is not None:
        lines += ["", f"الدخول : {r['early_entry']:.6g}"]
        if r.get("early_tps"):
            lines.append(f"TP1: {r['early_tps'][0]:.6g}")
        lines.append(f"SL : {r['early_sl']:.6g}")

    def block(title, items):
        if items:
            lines.append("")
            lines.append(title)
            lines.extend(f"- {x}" for x in items[:5])

    block("Why Early Signal Triggered:", e.get("reasons"))
    block("Missing Confirmations:", e.get("missing"))
    risks = list(e.get("risks") or [])
    if any("Open Interest" in x for x in (e.get("na") or [])):
        risks.append("Open Interest/Funding غير متاحين (لا تُحتسب)")
    block("Risk Factors:", risks)
    lines += ["", "Next Trigger:", f"- {e.get('next_trigger', '')}", "", "Invalidation:", f"- {e.get('invalidation', '')}"]
    return "\n".join(lines)


def format_early_update(ev):
    """رسالة تحديث تطور الإشارة (تُرسل كرد على الرسالة الأصلية عند وجودها)."""
    r, st, kind = ev["r"], ev["state"], ev["kind"]
    e = r.get("early_v2") or {}
    sym = r["symbol"].replace("USDT", "/USDT")
    hist = st.get("history") or []
    trend = " → ".join(f"{x:.0f}" for x in hist[-6:])
    icon = {"UPGRADE": "🔼", "UPDATE": "➖", "DOWNGRADE": "🔽", "INVALIDATED": "❌"}.get(kind, "ℹ️")
    lines = [f"{icon} Early Signal {kind}: {sym}"]
    if kind == "INVALIDATED":
        lines.append(f"السبب: {_EARLY_REASON_AR.get(ev.get('reason'), ev.get('reason'))}")
        lines.append(f"Score: {e.get('score', 0):.0f} ({trend})")
        lines.append("الإشارة فقدت قوتها. الصفقة المفتوحة (إن وُجدت) تُتابَع بـ TP/SL كما هي.")
    else:
        frm = EARLY_LEVEL_NAMES.get(ev.get("from_rank"), None)
        lines.append(f"{(frm + ' → ') if frm else ''}{e.get('level', '')}")
        lines.append(f"Score: {e.get('score', 0):.0f} ({trend})")
        cats, cmax = e.get("cats") or {}, e.get("cat_max") or {}
        lines.append(" | ".join(f"{c.title()} {cats.get(c, 0):.0f}/{cmax.get(c, 0):.0f}" for c in cats))
        if e.get("risks"):
            lines.append("Risk: " + e["risks"][0])
        if kind in ("UPGRADE", "UPDATE") and e.get("next_trigger"):
            lines.append("Next Trigger: " + e["next_trigger"])
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
        if r.get("breakout_tps"):
            lines.append(f"TP1: {r['breakout_tps'][0]:.6g}")
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
        if r.get("experimental_tps"):
            lines.append(f"TP1: {r['experimental_tps'][0]:.6g}")
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


class GistContentError(Exception):
    """يُرفع لما يتعذّر قراءة محتوى ملف JSON من Gist قراءة كاملة وسليمة.
    مقصود: بدل اعتبار الملف فارغًا بصمت (وكتابة فوقه = فقدان صفقات)، نوقف العملية."""
    pass


def _gist_read_json_list(entry, name=""):
    """يقرأ ملف JSON (قائمة) من بيانات ملف Gist بشكل آمن.
    GitHub قد يرجع محتوى الملف مقتطعًا أو فارغًا بالـAPI، فلو ظهر ذلك (أو فشل التحليل)
    نعيد القراءة من raw_url (المحتوى الكامل). لو فشلت القراءة السليمة نرفع
    GistContentError ولا نرجع قائمة فارغة أبدًا."""
    def _parse(text):
        data = json.loads(text or "[]")
        if not isinstance(data, list):
            raise ValueError("المحتوى ليس قائمة")
        return data

    content = entry.get("content")
    size = entry.get("size") or 0
    if not entry.get("truncated") and (content or size <= 2):
        try:
            return _parse(content)
        except Exception:
            pass  # نجرّب raw_url

    raw_url = entry.get("raw_url")
    if not raw_url:
        raise GistContentError(f"{name}: المحتوى مقتطع/غير صالح ولا يوجد raw_url")
    try:
        r = requests.get(raw_url, headers=_gist_headers(), timeout=30)
        r.raise_for_status()
        r.encoding = "utf-8"
        return _parse(r.text)
    except Exception as e:
        raise GistContentError(f"{name}: فشلت القراءة الكاملة من raw_url: {e}") from e


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
        # مهم: لو فشلت القراءة السليمة يُرفع GistContentError (لا نعتبر الملف فارغًا ولا نكتب فوقه)
        last_content = _gist_read_json_list(archive_files[last_name], last_name)
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
        "last_updated": dt.datetime.now(dt.timezone.utc).isoformat(),
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


def load_early_state(gist_files):
    """حالة تطور الإشارات المبكرة (Signal ID / Score السابق / الحالة) من نفس ملف الحالة. فارغة عند أي مشكلة."""
    try:
        content = _gist_get_file(GIST_FILENAME, gist_files)
        if not content:
            return {}
        data = json.loads(content).get("early_signals", {})
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"⚠️ تعذّر تحميل حالة الإشارات المبكرة (ستبدأ فارغة): {e}")
        return {}


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
    """السجل النشط للصفقات المغلقة (قراءة آمنة تتعامل مع المحتوى المقتطع عبر raw_url)."""
    entry = gist_files.get(CLOSED_GIST_FILE)
    if not entry:
        return []
    try:
        return _gist_read_json_list(entry, CLOSED_GIST_FILE)
    except Exception as e:
        print(f"⚠️ تعذّرت قراءة {CLOSED_GIST_FILE} للنموذج التكيفي: {e}")
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


def _adaptive_outcome_pnl(trade):
    entry, exit_price = trade.get("entry"), trade.get("exit_price")
    if not entry or not exit_price:
        return None
    return ((exit_price - entry) / entry * 100) - TRADING_FEE_PCT


def _adaptive_type(trade):
    return trade.get("type", "official")


def _adaptive_factor_value(trade, factor, ttype):
    """قيمة العامل من الصفقة المغلقة: الحقل المباشر أولًا، وإلا من قاموس details للصفقات
    القديمة (breakout/experimental) حيث الغياب = False. يعمل بأثر رجعي على التاريخ الحالي."""
    value = trade.get(factor)
    if value is not None:
        return value
    keys = ADAPTIVE_DETAIL_FACTORS.get(ttype)
    if keys and factor in keys:
        details = trade.get(f"{ttype}_details")
        if isinstance(details, dict) and details:
            return bool(details.get(factor))
    return None


def build_adaptive_model(history):
    """
    يبني نموذج evidence بسيطًا وقابلًا للتفسير من الصفقات المغلقة.
    لكل نوع إشارة نحسب baseline، ثم نقارن كل عامل بمتوسط النوع نفسه.
    contribution = فرق متوسط PnL بعد shrinkage نحو الصفر، مع سقف صغير حتى لا
    يسيطر عامل واحد على القرار. لا يتم تحويل أي عامل إلى hard filter تلقائيًا.
    """
    model = {"version": ADAPTIVE_ENGINE_VERSION, "types": {}}
    # استبعاد اختياري لصفقات نسخ محرك معيّنة (مثلًا adaptive_v1 التي كانت تعيد إرسال/فتح
    # إشارات early/breakout/experimental كل دورة بسبب خطأ مفاتيح الذاكرة، فتضخّم العيّنة بتكرار
    # شبه متطابق). الافتراضي: لا استبعاد. مثال: ADAPTIVE_EXCLUDE_ENGINES="adaptive_v1"
    excluded = {x.strip() for x in os.environ.get("ADAPTIVE_EXCLUDE_ENGINES", "").split(",") if x.strip()}
    if excluded and history:
        history = [t for t in history if t.get("adaptive_engine") not in excluded]
    if not history:
        return model

    for ttype in ADAPTIVE_FACTORS:
        trades = [t for t in history if _adaptive_type(t) == ttype]
        pnls = [_adaptive_outcome_pnl(t) for t in trades]
        pnls = [p for p in pnls if p is not None]
        n = len(pnls)
        if n == 0:
            model["types"][ttype] = {"n": 0, "baseline": 0.0, "factors": {}}
            continue
        baseline = sum(pnls) / n
        factor_model = {}
        for factor in ADAPTIVE_FACTORS[ttype]:
            groups = {True: [], False: []}
            for t in trades:
                p = _adaptive_outcome_pnl(t)
                if p is None:
                    continue
                value = _adaptive_factor_value(t, factor, ttype)
                # بعض العوامل ليست boolean (مثل rsi_state/bb_state). نتعامل معها
                # كفئات نصية منفصلة أدناه بدل تحويلها خطأ إلى True/False.
                if factor in ("rsi_state", "bb_state"):
                    key = str(value) if value is not None else None
                    if key is None:
                        continue
                    groups.setdefault(key, []).append(p)
                else:
                    if value is None:
                        continue
                    groups[bool(value)].append(p)
            entries = {}
            for key, vals in groups.items():
                if not vals:
                    continue
                count = len(vals)
                raw_edge = (sum(vals) / count) - baseline
                shrink = count / (count + ADAPTIVE_PRIOR_TRADES)
                edge = raw_edge * shrink
                entries[str(key)] = {
                    "n": count,
                    "avg_pnl": round(sum(vals) / count, 4),
                    "edge": round(edge, 4),
                }
            factor_model[factor] = entries
        model["types"][ttype] = {"n": n, "baseline": round(baseline, 4), "factors": factor_model}
    return model


def adaptive_signal_rank(r, signal_type, model, market_regime=None):
    """
    يرجع evidence score إضافي مستقل عن score الأصلي.
    الهدف: ترتيب المرشحين حسب ما أثبتته البيانات، لا إعادة اختراع signal detector.
    القيم الإيجابية تعني أن الحالة الحالية كانت تاريخيًا أفضل من baseline لنفس النوع.
    """
    type_model = (model.get("types") or {}).get(signal_type) or {}
    n = type_model.get("n", 0)
    if n < ADAPTIVE_MIN_TRADES:
        return 0.0, {"status": "insufficient_history", "n": n}

    score = 0.0
    evidence = []
    factors = ADAPTIVE_FACTORS.get(signal_type, [])
    for factor in factors:
        value = r.get(factor)
        if factor == "rsi_state" or factor == "bb_state":
            key = str(value) if value is not None else None
        else:
            if value is None:
                continue
            key = str(bool(value))
        item = ((type_model.get("factors") or {}).get(factor) or {}).get(key)
        if not item:
            continue
        edge = float(item.get("edge", 0.0))
        score += edge
        evidence.append({"factor": factor, "state": key, "edge": edge, "n": item.get("n", 0)})

    # ملاحظة: market_regime لا يدخل في الدرجة هنا؛ تأثيره عبر regime_filter (REGIME_POLICY) في main().
    return round(score, 4), {"status": "active", "n": n, "evidence": evidence}


def apply_adaptive_ranking(results, model, market_regime=None):
    """يضيف adaptive score لكل مرشح. الترتيب الفعلي يتم لاحقًا في adaptive_sort()."""
    ranked = []
    for r in results:
        candidates = []
        if r.get("entry") is not None:
            candidates.append("official")
        if r.get("early_entry") is not None:
            candidates.append("early")
        if r.get("breakout_entry") is not None:
            candidates.append("breakout")
        if r.get("experimental_entry") is not None:
            candidates.append("experimental")
        r["adaptive"] = {}
        for ttype in candidates:
            extra, detail = adaptive_signal_rank(r, ttype, model, market_regime)
            # breakout/experimental details ليست موجودة في r كحقول مباشرة، لذلك نضيفها
            # من القواميس الخاصة بهما إلى نسخة التقييم.
            if ttype == "breakout":
                local = dict(r)
                local.update(flatten_signal_details("breakout", r.get("breakout_details")))
                extra, detail = adaptive_signal_rank(local, ttype, model, market_regime)
            elif ttype == "experimental":
                local = dict(r)
                local.update(flatten_signal_details("experimental", r.get("experimental_details")))
                extra, detail = adaptive_signal_rank(local, ttype, model, market_regime)
            r["adaptive"][ttype] = {"score": extra, **detail}
        ranked.append(r)
    return ranked


def adaptive_pass(r, signal_type):
    """في أول فترة بدون تاريخ كافٍ نمرر الإشارة كما هي؛ بعد توفر تاريخ كافٍ
    لا نرفض إلا المرشحين ذوي evidence سلبي بوضوح، مع إبقاء قرار العدد/المحفظة منفصلًا."""
    a = (r.get("adaptive") or {}).get(signal_type) or {}
    if a.get("status") == "insufficient_history":
        return True
    return float(a.get("score", 0.0)) >= ADAPTIVE_MIN_RANK


def adaptive_sort(candidates, signal_type):
    """يرتّب المرشحين تنازليًا حسب adaptive score (الأعلى أولًا).
    الترتيب مستقر (stable): عند التعادل (مثلًا فترة insufficient_history حيث كل الدرجات 0)
    يبقى الترتيب الأصلي كما هو، فلا نفرض ترتيبًا غير مثبت إحصائيًا.
    القائمة تُرتَّب قبل بناء fresh* حتى تصل الشرائح للأفضل أولًا."""
    def _key(r):
        a = (r.get("adaptive") or {}).get(signal_type) or {}
        try:
            return float(a.get("score", 0.0))
        except (TypeError, ValueError):
            return 0.0
    return sorted(candidates, key=_key, reverse=True)


def log_adaptive_top(candidates, signal_type, top_n=5):
    """طباعة أعلى المرشحين بعد الترتيب لسهولة المراجعة في لوق GitHub Actions."""
    if not candidates:
        return
    parts = []
    for r in candidates[:top_n]:
        a = (r.get("adaptive") or {}).get(signal_type) or {}
        parts.append(f"{r['symbol']}={a.get('score', 0.0):+.3f}")
    print(f"🏅 ترتيب {signal_type} (adaptive): " + " | ".join(parts))


def regime_allows(r, signal_type, market_regime):
    """يطبّق REGIME_POLICY على إشارة واحدة. يرجع True لو مسموحة."""
    if not REGIME_FILTER_ENABLED or market_regime is None:
        return True
    mode = (REGIME_POLICY.get(market_regime) or {}).get(signal_type, "allow")
    if mode == "allow":
        return True
    if mode == "block":
        return False
    # strict: شروط إضافية
    if r.get("htf_aligned") is not True:
        return False
    if signal_type == "experimental":
        flat = flatten_signal_details("experimental", r.get("experimental_details"))
        vol_ok = bool(r.get("vol_confirm")) or flat.get("volume_confirm")
    else:
        vol_ok = bool(r.get("vol_confirm"))
    if not vol_ok or r.get("extended"):
        return False
    if signal_type == "official" and abs(r.get("score") or 0) < REGIME_STRICT_MIN_SCORE_OFFICIAL:
        return False
    return True


def regime_filter(candidates, signal_type, market_regime):
    """يصفّي قائمة مرشحين حسب نظام السوق ويطبع عدد المستبعد (شفافية في لوق GitHub Actions)."""
    kept = [r for r in candidates if regime_allows(r, signal_type, market_regime)]
    dropped = len(candidates) - len(kept)
    if dropped:
        mode = (REGIME_POLICY.get(market_regime) or {}).get(signal_type, "allow")
        print(f"🧭 regime={market_regime} | {signal_type} ({mode}): استُبعد {dropped} من {len(candidates)}")
    return kept


def load_closed_full(gist_files):
    """يحمّل الأرشيف الكامل للصفقات المغلقة (السجل النشط + كل ملفات الأرشيف عبر سلسلة
    الـGists) لبناء النموذج التكيفي. للقراءة فقط ولا يكتب شيئًا. عند أي فشل نرجع للسجل
    النشط فقط حتى لا يتعطل البوت. تُزال التكرارات بمفتاح (type, symbol, opened_at)."""
    active = load_closed(gist_files)
    try:
        chain_raw = _gist_get_file(ARCHIVE_CHAIN_FILE, gist_files)
        chain = json.loads(chain_raw) if chain_raw else []
        all_trades = []
        for gid in chain:
            files = gist_files if gid == GIST_ID else _gist_get_all_files_for(gid)
            for fn in sorted(f for f in files if f.startswith(ARCHIVE_PREFIX)):
                all_trades.extend(_gist_read_json_list(files[fn], fn))
        all_trades.extend(active)
        seen, merged = set(), []
        for t in all_trades:
            k = (t.get("type", "official"), t.get("symbol"), t.get("opened_at"))
            if k in seen:
                continue
            seen.add(k)
            merged.append(t)
        print(f"📚 تاريخ النموذج التكيفي: {len(merged)} صفقة (نشط {len(active)} + أرشيف {len(merged) - len(active)})")
        return merged
    except Exception as e:
        print(f"⚠️ تعذّر تحميل الأرشيف الكامل للنموذج التكيفي — سيُستخدم السجل النشط فقط: {e}")
        return active


def _engine_key(symbol, kind="official"):
    """مفتاح ذاكرة التنبيهات: نسخة المحرك + الرمز (+ نوع الإشارة لغير الرسمية).
    يُمرَّر له الرمز الخام دائمًا (وليس مفتاحًا جاهزًا) كي لا يتكرر اللاحق."""
    suffix = "" if kind == "official" else f":{kind}"
    return f"{ADAPTIVE_ENGINE_VERSION}:{symbol}{suffix}"


def build_alerted_keys(strong, early_eligible, breakout_eligible, experimental_eligible):
    """كل مفاتيح الإشارات المؤهلة هذه الدورة — تُحفظ كذاكرة تنبيهات. تُبنى من الرموز
    الخام بنفس دالة _engine_key المستخدمة في فلترة fresh*، فتتطابق المفاتيح تمامًا."""
    return (
        {_engine_key(r["symbol"], "official") for r in strong}
        | {_engine_key(r["symbol"], "early") for r in early_eligible}
        | {_engine_key(r["symbol"], "breakout") for r in breakout_eligible}
        | {_engine_key(r["symbol"], "experimental") for r in experimental_eligible}
    )


def _already_open(positions, symbol, signal_type):
    """هل توجد صفقة مفتوحة بنفس الرمز والنوع؟ (لمنع صفقتين مكررتين لنفس الإشارة)"""
    return any(p.get("symbol") == symbol and p.get("type", "official") == signal_type for p in positions)


def log_adaptive_rejections(results):
    """يطبع عدد المرشحين الذين رفضهم المحرك التكيفي لكل نوع (شفافية: ما يُرفض لا يُتتبَّع)."""
    counts = {}
    for r in results:
        for ttype, a in (r.get("adaptive") or {}).items():
            if not adaptive_pass(r, ttype):
                counts[ttype] = counts.get(ttype, 0) + 1
    if counts:
        print("🚫 مرفوض بالمحرك التكيفي: " + " | ".join(f"{k}={v}" for k, v in sorted(counts.items())))


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
    tp_hit = {"tp1": {"hit": 0, "total": 0}}
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

        # نسبة تحقق TP1 — كل صفقة لديها هدف واحد فقط.
        if t.get("tps"):
            tp_hit["tp1"]["total"] += 1
            if t.get("tp_hit") or t.get("closed_reason") == "TP1":
                tp_hit["tp1"]["hit"] += 1

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
    profit_factor = round(gains / losses_sum, 2) if losses_sum > 0 else None  # None = لا خسائر (يتفادى Infinity غير الصالح بـJSON)
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


def save_all_state(alerted_symbols, btc_dominance, positions, closed_delta, gist_files, early_state=None):
    """
    يحفظ في نفس الطلب: حالة التنبيهات + BTC Dominance + الصفقات المفتوحة،
    ويُلحق أي صفقات أُغلقت هذا التشغيل بسجل closed_trades (مع ترحيل الفائض للأرشيف
    بدل حذفه). كما يحسب إحصائيات أداء (stats.json) من السجل المحدَّث — تتبع فقط،
    بدون أي تأثير على منطق الفحص أو الدخول. يرجع الإحصائيات (أو None) للاستخدام
    الاختياري في إرسال تقرير دوري.

    gist_files: نفس القاموس المُجلب مرة واحدة في بداية main() — بلا أي إعادة جلب هنا،
    كي لا يتعرض الحفظ لفشل شبكي مستقل في آخر لحظة.
    """
    state_obj = {"alerted": sorted(alerted_symbols), "btc_dominance_prev": btc_dominance}
    if early_state is not None:
        state_obj["early_signals"] = early_state
    files = {
        GIST_FILENAME: json.dumps(state_obj, ensure_ascii=False, separators=(',', ':')),
    }

    history = None
    if closed_delta:
        if CLOSED_GIST_FILE in gist_files:
            try:
                history = _gist_read_json_list(gist_files[CLOSED_GIST_FILE], CLOSED_GIST_FILE)
            except GistContentError as e:
                print(f"❌ تعذّرت قراءة السجل النشط، لن يُكتب فوقه: {e}")
                send_admin_alert(f"تعذّرت قراءة closed_trades.json — لم تُسجَّل الصفقات المغلقة بعد، "
                                 f"أُبقيت بقائمة المفتوحة وتُعاد معالجتها بالتشغيلة القادمة: {e}")
        else:
            history = []

    # لو تعذّرت قراءة السجل المغلق لا نخسر الصفقات التي أُغلقت الآن: نُبقيها ضمن ملف الصفقات
    # المفتوحة، فتُعاد معالجتها (إغلاقها حتميًا من نفس الشموع) بالتشغيلة القادمة بدل الضياع.
    positions_to_save = positions
    if closed_delta and history is None:
        positions_to_save = list(positions) + list(closed_delta)

    # لا نحفظ positions إذا لم تتغير — يقلل حجم الطلب وعدد مرات الكتابة على Gist
    try:
        old_positions = json.loads(_gist_get_file(POSITIONS_GIST_FILE, gist_files) or "[]")
    except Exception:
        old_positions = []
    if old_positions != positions_to_save:
        files[POSITIONS_GIST_FILE] = json.dumps(positions_to_save, ensure_ascii=False, separators=(',', ':'))

    stats = None
    if closed_delta and history is not None:
        history.extend(closed_delta)

        # إذا تجاوز السجل النشط الحد، تُرحَّل أقدم الصفقات لملفات الأرشيف بدل حذفها نهائيًا —
        # لا يُفقد أي شيء، والسجل النشط يبقى دائمًا صغيرًا وسريع القراءة
        if len(history) > ACTIVE_HISTORY_SIZE:
            overflow = history[:-ACTIVE_HISTORY_SIZE]
            full_history = history
            history = history[-ACTIVE_HISTORY_SIZE:]
            try:
                archive_gist_id, archive_files, main_updates = archive_overflow(overflow, gist_files)
            except (GistContentError, GistFetchError) as e:
                # فشل قراءة الأرشيف: لا نكتب فوقه ولا نقتطع السجل النشط، فلا تضيع أي صفقة؛
                # يُعاد ترحيل الفائض تلقائيًا بالتشغيلة القادمة.
                print(f"❌ تعذّر ترحيل الأرشيف هذه الدورة (لم يُفقد شيء): {e}")
                send_admin_alert(f"تعذّر ترحيل الصفقات للأرشيف، أُبقيت بالسجل النشط مؤقتًا: {e}")
                history = full_history
                archive_gist_id, archive_files, main_updates = None, {}, {}
            files.update(main_updates)  # سلسلة الـGists + الفهرس، تُحفظ بالGist الرئيسي
            if archive_gist_id and archive_files:
                if archive_gist_id == GIST_ID:
                    files.update(archive_files)  # نفس الـGist الرئيسي، تُدمج بحفظة واحدة
                else:
                    archived_ok = _gist_patch_files(archive_files, gist_id=archive_gist_id)
                    if not archived_ok:
                        # لا نقتطع السجل النشط لو لم يُحفظ الأرشيف — وإلا تضيع الصفقات الفائضة نهائيًا
                        print(f"❌ فشل حفظ ملفات الأرشيف بـGist منفصل ({archive_gist_id}) — "
                              f"أُبقي الفائض بالسجل النشط وتُعاد المحاولة لاحقًا")
                        history = full_history

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

def open_new_positions(positions, fresh_signals, market_regime=None):
    """يضيف كل إشارة شراء جديدة أُرسلت كصفقة مفتوحة قيد المتابعة. يُعدّل القائمة في المكان (in place)."""
    for r in fresh_signals:
        if r.get("entry") is None:
            continue  # لا خطة دخول (تجنب شراء) -> لا داعي لتتبعها
        positions.append({
            "market_regime": market_regime,
            "symbol": r["symbol"],
            "entry": r["entry"],
            "sl": r["sl"],
            "tps": r["tps"],
            "tp_hit": False,
            "score": r["score"],
            "adaptive_score": (r.get("adaptive", {}).get("official", {}) or {}).get("score"),
            "adaptive_engine": ADAPTIVE_ENGINE_VERSION,
            "trend_up": r["trend_up"],   # اتجاه EMA9/21 وقت فتح الصفقة، يُستخدم لاحقًا لكشف انعكاس الإشارة
            "interval": INTERVAL,
            "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "monitor_interval": MONITOR_INTERVAL,
            "initial_risk": r["entry"] - r["sl"],  # المخاطرة الأصلية (وحدة سعر) -- تُستخدم لاحقًا بحساب Breakeven/R
            "type": "official",
            "official_engine": OFFICIAL_ENGINE_VERSION,
            "size_pct": r.get("size_pct"),
            "risk_pct": r.get("risk_pct"),
            "adx_val": r.get("adx_val"),
            "vol_ratio": r.get("vol_ratio"),
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
            "di_confirm": r.get("di_confirm"),
            "momentum_agree": r.get("momentum_agree"),
            # message_id ونص رسالة الإشارة الأصلية -> تُستخدم لاحقًا لتعديل نفس الرسالة (شطب + نتيجة) عند الإغلاق
            "alert_message_id": r.get("_msg_id"),
            "alert_text": r.get("_alert_text"),
        })


def open_new_early_positions(positions, fresh_early_signals, market_regime=None):
    """
    يفتح متابعة تلقائية (TP/SL) لإشارات مبكرة توفّرت لها أهداف تقديرية، بنفس آلية
    الصفقات الرسمية لكن بحقل type="early" يُستخدم لاحقًا لتمييز رسائل النتيجة.
    """
    for r in fresh_early_signals:
        if r.get("early_entry") is None:
            continue
        positions.append({
            "market_regime": market_regime,
            "symbol": r["symbol"],
            "entry": r["early_entry"],
            "sl": r["early_sl"],
            "tps": r["early_tps"],
            "tp_hit": False,
            "score": r["score"],
            "adaptive_score": (r.get("adaptive", {}).get("early", {}) or {}).get("score"),
            "adaptive_engine": ADAPTIVE_ENGINE_VERSION,
            "trend_up": r["trend_up"],
            "interval": INTERVAL,
            "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "monitor_interval": MONITOR_INTERVAL,
            "initial_risk": r["early_entry"] - r["early_sl"],
            "type": "early",
            "concurrent_signals": r.get("concurrent_signals", []),
            "confidence": r.get("early_confidence"),
            "early_engine": EARLY_ENGINE_VERSION,
            "early_score": r.get("early_score"),
            "early_level": r.get("early_level"),
            "early_cats": (r.get("early_v2") or {}).get("cats"),
            "signal_id": r.get("_signal_id"),
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
            "momentum": r.get("momentum"),
            "alert_message_id": r.get("_msg_id"),
            "alert_text": r.get("_alert_text"),
        })


def open_new_breakout_positions(positions, fresh_breakout_signals, market_regime=None):
    """
    يفتح متابعة تلقائية (TP/SL) لإشارات الانفجار (breakout) بنفس آلية الصفقات
    الرسمية/المبكرة، بحقل type="breakout" يُستخدم لاحقًا لتمييز رسائل النتيجة.
    """
    for r in fresh_breakout_signals:
        if r.get("breakout_entry") is None:
            continue
        positions.append({
            "market_regime": market_regime,
            "symbol": r["symbol"],
            "entry": r["breakout_entry"],
            "sl": r["breakout_sl"],
            "tps": r["breakout_tps"],
            "tp_hit": False,
            "score": r["breakout_score"],
            "adaptive_score": (r.get("adaptive", {}).get("breakout", {}) or {}).get("score"),
            "adaptive_engine": ADAPTIVE_ENGINE_VERSION,
            "trend_up": r["trend_up"],
            "interval": INTERVAL,
            "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "monitor_interval": MONITOR_INTERVAL,
            "initial_risk": r["breakout_entry"] - r["breakout_sl"],
            "type": "breakout",
            "concurrent_signals": r.get("concurrent_signals", []),
            "breakout_details": r.get("breakout_details"),
            **flatten_signal_details("breakout", r.get("breakout_details")),
            "extended": r.get("extended"),
            "vol_confirm": r.get("vol_confirm"),
            "htf_aligned": r.get("htf_aligned"),
            "ranging": r.get("ranging"),
            "near_resistance": r.get("near_resistance"),
            "alert_message_id": r.get("_msg_id"),
            "alert_text": r.get("_alert_text"),
        })


def open_new_experimental_positions(positions, fresh_experimental_signals, market_regime=None):
    """
    يفتح متابعة تلقائية (TP/SL) لإشارات التجريبية بنفس آلية الرسمية/المبكرة/الانفجار،
    بحقل type="experimental" يُستخدم لاحقًا لتمييز رسائل النتيجة.
    """
    for r in fresh_experimental_signals:
        if r.get("experimental_entry") is None:
            continue
        positions.append({
            "market_regime": market_regime,
            "symbol": r["symbol"],
            "entry": r["experimental_entry"],
            "sl": r["experimental_sl"],
            "tps": r["experimental_tps"],
            "tp_hit": False,
            "score": r["experimental_score"],
            "adaptive_score": (r.get("adaptive", {}).get("experimental", {}) or {}).get("score"),
            "adaptive_engine": ADAPTIVE_ENGINE_VERSION,
            "trend_up": r["trend_up"],
            "interval": INTERVAL,
            "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "monitor_interval": MONITOR_INTERVAL,
            "initial_risk": r["experimental_entry"] - r["experimental_sl"],
            "type": "experimental",
            "concurrent_signals": r.get("concurrent_signals", []),
            "experimental_details": r.get("experimental_details"),
            **flatten_signal_details("experimental", r.get("experimental_details")),
            "near_resistance": r.get("near_resistance"),
            "extended": r.get("extended"),
            "vol_confirm": r.get("vol_confirm"),
            "htf_aligned": r.get("htf_aligned"),
            "ranging": r.get("ranging"),
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


def format_tp_hit(pos, price):
    entry = pos["entry"]
    tp = pos["tps"][0]
    pct_gain = (tp - entry) / entry * 100
    duration = format_duration(_hours_since(pos["opened_at"]))
    type_labels = {"early": "✅ (إشارة مبكرة) ", "breakout": "✅ (إشارة انفجار) ", "experimental": "✅ (إشارة تجريبية) "}
    header = type_labels.get(pos.get("type"), "✅ ")
    return (
        f"{header}{pos['symbol'].replace('USDT', '/USDT')}\n"
        f"سعر الدخول: {entry:.6g}\n"
        f"TP1: {tp:.6g}\n"
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
    """يتابع TP1 وSL فقط ويغلق الصفقة نهائيًا عند تحقق أي منهما."""
    still_open, closed_now = [], []
    monitor_map = monitor_map or {}

    for pos in positions:
        symbol = pos["symbol"]
        price = price_map.get(symbol)
        candles = monitor_map.get(symbol) or []

        if price is None and candles:
            try:
                price = float(candles[-1][4])
            except Exception:
                price = None
        if price is None:
            still_open.append(pos)
            continue

        silent = pos.get("alert_message_id") is None and pos.get("type") == "early"
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
        tp = pos["tps"][0] if pos.get("tps") else None

        for candle_open_ms, high, low, candle_close in events:
            # SL أولًا عند تعارض TP1 وSL داخل الشمعة.
            if low <= pos["sl"]:
                exit_price = pos["sl"]
                result_text = format_sl_hit(pos, exit_price)
                if not silent:
                    send_telegram(result_text)
                edit_telegram_strike(pos.get("alert_message_id"), pos.get("alert_text", ""), result_text)
                pos["closed_reason"] = "SL"
                pos["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(candle_open_ms / 1000))
                pos["exit_price"] = exit_price
                pos["monitor_exit_interval"] = MONITOR_INTERVAL
                pos["monitor_exit_candle_open_ms"] = candle_open_ms
                closed_now.append(pos)
                closed = True
                break

            # TP1: عند الوصول إليه تُغلق الصفقة نهائيًا.
            if tp is not None and high >= tp:
                pos["tp_hit"] = True
                result_text = format_tp_hit(pos, tp)
                if not silent:
                    send_telegram(result_text)
                edit_telegram_strike(pos.get("alert_message_id"), pos.get("alert_text", ""), result_text)
                pos["closed_reason"] = "TP1"
                pos["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(candle_open_ms / 1000))
                pos["exit_price"] = tp
                pos["monitor_exit_interval"] = MONITOR_INTERVAL
                pos["monitor_exit_candle_open_ms"] = candle_open_ms
                closed_now.append(pos)
                closed = True
                break

        if closed:
            continue

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
            edit_telegram_strike(pos.get("alert_message_id"), pos.get("alert_text", ""), expired_text)
            pos["closed_reason"] = "EXPIRED"
            pos["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            pos["exit_price"] = price
            pos["monitor_exit_interval"] = MONITOR_INTERVAL
            closed_now.append(pos)
            continue

        still_open.append(pos)

    if closed_now:
        print(f"صفقات أُغلقت هذا المسح: {len(closed_now)}")
    return still_open, closed_now


# ---------------- BTC Dominance (تحذير جودة إشارات العملات البديلة) ----------------


def fetch_btc_dominance():
    r = _request_with_retry("https://api.coingecko.com/api/v3/global")
    return r.json()["data"]["market_cap_percentage"]["btc"]


def compute_market_regime(closes, highs, lows):
    """
    يحدد نظام السوق العام استنادًا لسلسلة شموع (BTCUSDT عادة) عبر:
    1) ADX(14) لقياس قوة الاتجاه (نفس ADX_THRESHOLD المستخدم لفلتر "سوق عرضي" الحالي)
    2) عند وجود اتجاه: ميل EMA50 (القيمة الحالية مقابل قيمتها قبل
       MARKET_REGIME_SLOPE_LOOKBACK شمعة) لتحديد جهة الاتجاه
    يُرجع: "trending_up" / "trending_down" / "ranging" / None (بيانات غير كافية)
    """
    min_len = MARKET_REGIME_EMA_PERIOD + MARKET_REGIME_SLOPE_LOOKBACK + 1
    if len(closes) < min_len:
        return None

    adx_vals = adx(highs, lows, closes)
    adx_last = adx_vals[-1] if adx_vals else None
    if adx_last is None:
        return None

    if adx_last < ADX_THRESHOLD:
        return "ranging"

    ema50 = ema(closes, MARKET_REGIME_EMA_PERIOD)
    return "trending_up" if ema50[-1] > ema50[-1 - MARKET_REGIME_SLOPE_LOOKBACK] else "trending_down"


def fetch_market_regime():
    """
    يجلب شموع BTCUSDT بنفس إطار السكانر (INTERVAL) ويحسب market_regime الحالي.
    يُستدعى مرة واحدة فقط لكل دورة مسح (مقياس عام للسوق، وليس خاص بعملة معينة).
    """
    klines = fetch_klines("BTCUSDT", INTERVAL, SCAN_LIMIT)
    klines = drop_unclosed_candle(klines)
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    return compute_market_regime(closes, highs, lows)


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
    # نجلب سياق المحفظة الوهمية (Gist منفصل) مبكرًا هنا فقط — مرة واحدة لكل دورة —
    # عشان نضم رموز صفقاتها المفتوحة لخريطة شموع المراقبة قبل بنائها، بدل ما تعتمد
    # لاحقًا على آخر سعر ticker فقط وتفوّت TP/SL حصل وارتد بين تشغيلتين متباعدتين
    # (نفس المشكلة التي صُممت خريطة المراقبة أصلاً لتفاديها للصفقات الحقيقية).
    # paper_context يُمرَّر لاحقًا لـ paper_trading.run_cycle بدل إعادة جلب نفس الـGist مرتين.
    paper_context = None
    paper_positions_for_monitor = []
    if _PAPER_TRADING_AVAILABLE:
        try:
            paper_context = paper_trading.load_context()
            paper_positions_for_monitor = paper_trading.get_open_symbols(paper_context)
        except Exception as e:
            print(f"⚠️ [محفظة وهمية] تعذّر تحميل سياقها المبكر — ستُبنى خريطة المراقبة "
                  f"للصفقات الحقيقية فقط هذه الدورة: {e}")

    monitor_map = {}
    positions_for_monitor = open_positions + paper_positions_for_monitor
    if positions_for_monitor:
        monitor_map = _monitor_candle_map(positions_for_monitor)
        covered = sum(1 for p in open_positions if monitor_map.get(p.get("symbol")))
        print(f"📈 شموع المراقبة ({MONITOR_INTERVAL}) متوفرة لـ {covered}/{len(open_positions)} صفقة حقيقية "
              f"(+ {len(paper_positions_for_monitor)} صفقة وهمية ضُمّت لنفس الخريطة)")
    open_positions, closed_now = check_open_positions(open_positions, price_map, monitor_map)
    print(f"🔒 صفقات متبقية مفتوحة: {len(open_positions)} | أُغلقت الآن: {len(closed_now)}")

    # نموذج الاختيار التكيفي يُبنى من الصفقات المغلقة الموجودة حاليًا، بدون حذفها أو إعادة ضبطها.
    closed_history = load_closed_full(gist_files)
    adaptive_model = build_adaptive_model(closed_history)

    # regime يُحسب قبل اختيار الإشارات حتى يصبح جزءًا من قرار الترتيب والتسجيل.
    market_regime = None
    try:
        market_regime = fetch_market_regime()
        print(f"Market Regime (BTCUSDT {INTERVAL}): {market_regime}")
    except Exception as e:
        print("تعذّر حساب market_regime:", e)

    results = run_scan(tickers)
    results = apply_adaptive_ranking(results, adaptive_model, market_regime)
    log_adaptive_rejections(results)

    # الإشارة الرسمية (confluence_v1): كل شروط الـgates أُجريت داخل analyze_symbol (official_ok)؛ هنا فقط
    # فلتر الجدوى بعد التكاليف (رسوم + slippage للجهتين) ثم النموذج التكيفي. لا persistent ولا درجة عتبة
    # منفصلة: التقاطع الحديث لـMACD (<= OFFICIAL_MACD_CROSS_LOOKBACK شموع) يحل محل فكرة الاستقرار.
    strong = [
        r for r in results
        if r.get("official_ok") and r.get("entry") is not None
        and meets_min_profit(r["entry"], r["tps"], fee_pct=OFFICIAL_TOTAL_COST_PCT)
        and adaptive_pass(r, "official")
    ]
    _gate_fail = {}
    for _r in results:
        for _g in _r.get("official_failed", []):
            _gate_fail[_g] = _gate_fail.get(_g, 0) + 1
    if _gate_fail:
        _top = sorted(_gate_fail.items(), key=lambda kv: kv[1], reverse=True)[:5]
        print(f"🧪 الرسمية ({OFFICIAL_ENGINE_VERSION}): اجتازت {sum(1 for _r in results if _r.get('official_ok'))} من {len(results)} | "
              "أكثر الشروط رفضًا: " + " | ".join(f"{k}={v}" for k, v in _top))
    strong = regime_filter(strong, "official", market_regime)
    strong_symbols = {r["symbol"] for r in strong}

    # إشارات مبكرة (انضغاط تقلب / تراكم صامت) لعملات لم تصل بعد لإشارة شراء كاملة —
    # تُميَّز بمفتاح منفصل (":early") في ذاكرة التنبيهات كي لا تتعارض مع إشارات الشراء الرسمية.
    # تُصفّى هنا أيضًا بنفس شرط الحد الأدنى لنسبة الربح (MIN_PROFIT_PCT) قبل اعتبارها مؤهلة
    # أصلاً — وليس فقط عند الإرسال — كي لا تُسجَّل كـ"مُنبَّه عليها" في الذاكرة وتُحرَم من
    # الإرسال لاحقًا إن تحسّن ربحها المتوقع
    early_eligible = [
        r for r in results
        if r.get("early_v2") and r["early_v2"].get("sendable") and not r.get("official_ok")
        and r.get("early_confidence") is not None and r.get("early_entry") is not None
        and meets_min_profit(r["early_entry"], r["early_tps"])
        and adaptive_pass(r, "early")
    ]
    early_eligible = regime_filter(early_eligible, "early", market_regime)
    early_keys = {f"{r['symbol']}:early" for r in early_eligible}

    # إشارات انفجار (breakout) — اختراق قمة سابقة مع تأكيد حجم، مستقلة عن الإشارة الرسمية،
    # تُستبعد العملات اللي أصلاً عندها إشارة رسمية جديدة تجنبًا للتكرار
    breakout_eligible = [
        r for r in results
        if r.get("breakout_entry") is not None
        and r["symbol"] not in strong_symbols
        and meets_min_profit(r["breakout_entry"], r["breakout_tps"])
        and adaptive_pass(r, "breakout")
    ]
    breakout_eligible = regime_filter(breakout_eligible, "breakout", market_regime)
    breakout_keys = {f"{r['symbol']}:breakout" for r in breakout_eligible}

    # إشارات تجريبية (Ichimoku Tenkan/Kijun + حجم/OBV + MFI) — مستقلة، تُستبعد العملات
    # اللي أصلاً عندها إشارة رسمية جديدة تجنبًا للتكرار
    experimental_eligible = [
        r for r in results
        if r.get("experimental_entry") is not None
        and r["symbol"] not in strong_symbols
        and meets_min_profit(r["experimental_entry"], r["experimental_tps"])
        and adaptive_pass(r, "experimental")
    ]
    experimental_eligible = regime_filter(experimental_eligible, "experimental", market_regime)
    experimental_keys = {f"{r['symbol']}:experimental" for r in experimental_eligible}

    # ترتيب المرشحين: الأعلى adaptive score أولًا (يحدد من يأخذ الشرائح/الرصيد أولًا)
    strong = adaptive_sort(strong, "official")
    early_eligible = adaptive_sort(early_eligible, "early")
    breakout_eligible = adaptive_sort(breakout_eligible, "breakout")
    experimental_eligible = adaptive_sort(experimental_eligible, "experimental")
    log_adaptive_top(strong, "official")
    log_adaptive_top(early_eligible, "early")
    log_adaptive_top(breakout_eligible, "breakout")
    log_adaptive_top(experimental_eligible, "experimental")

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
    # ذاكرة منفصلة للإصدار التكيفي: لا نمسح الذاكرة القديمة، لكن لا نسمح لها بمنع
    # اختبار المحرك الجديد على الرموز التي سبق إرسالها بالمحرك القديم.
    # _engine_key معرّفة على مستوى الموديول (بجانب build_alerted_keys).

    fresh = [r for r in strong if _engine_key(r["symbol"], "official") not in prev_alerted]
    # الإشارة المبكرة v2: القرار بتطور الإشارة عبر الزمن (Signal ID + Score السابق/الحالي + الحالة)
    # بدل مفتاح "أُرسلت/لم تُرسل". لا رسالة إلا عند: إشارة جديدة، أو تغير مستوى، أو تغير Score مهم، أو إلغاء.
    early_state_prev = load_early_state(gist_files)
    _open_early = {p["symbol"] for p in open_positions if p.get("type", "official") == "early"}
    _early_elig_syms = {r["symbol"] for r in early_eligible if r["symbol"] not in _open_early}
    try:
        early_state_new, early_events = early_evolution_update(early_state_prev, results, _early_elig_syms)
    except Exception as _ee:
        print(f"⚠️ [early_v2] تعذّر تحديث تطور الإشارات (تُحفظ الحالة السابقة كما هي): {_ee}")
        traceback.print_exc()
        early_state_new, early_events = early_state_prev, []
    _lvl_counts = {}
    for _r in results:
        _e = _r.get("early_v2")
        if _e:
            _k = EARLY_LEVEL_NAMES[early_effective_rank(_e)]
            _lvl_counts[_k] = _lvl_counts.get(_k, 0) + 1
    print("🔭 [early_v2] المستويات: " + " | ".join(f"{k}={v}" for k, v in sorted(_lvl_counts.items())) +
          f" | أحداث: " + ", ".join(f"{ev['kind']}:{ev['symbol']}" for ev in early_events))
    fresh_early = [ev["r"] for ev in early_events if ev["kind"] == "NEW"]
    early_updates = [ev for ev in early_events if ev["kind"] != "NEW"]
    _early_state_of = {ev["symbol"]: ev["state"] for ev in early_events if ev["kind"] == "NEW"}
    for _r in fresh_early:
        _r["_signal_id"] = _early_state_of[_r["symbol"]].get("signal_id")
    fresh_breakout = [r for r in breakout_eligible if _engine_key(r["symbol"], "breakout") not in prev_alerted]
    fresh_experimental = [r for r in experimental_eligible if _engine_key(r["symbol"], "experimental") not in prev_alerted]

    # لا تنبيه ولا صفقة جديدة لإشارة لها صفقة مفتوحة أصلًا بنفس الرمز والنوع
    fresh = [r for r in fresh if not _already_open(open_positions, r["symbol"], "official")]
    fresh_breakout = [r for r in fresh_breakout if not _already_open(open_positions, r["symbol"], "breakout")]
    fresh_experimental = [r for r in fresh_experimental if not _already_open(open_positions, r["symbol"], "experimental")]

    # محاط بـ try/except عمدًا: أي استثناء غير متوقع هنا (وليس بس فشل جلب/حفظ الـGist،
    # ده متحكّم فيه جوه paper_trading.py نفسه) لازم يتسجل باللوق بس ولا يوقف main()
    # قبل ما يوصل لـ save_all_state() بتاع البوت الحقيقي تحت — عزل كامل زي ما هو مطلوب.
    if _PAPER_TRADING_AVAILABLE:
        try:
            paper_trading.run_cycle(paper_context, price_map, monitor_map,
                                     fresh, fresh_early, fresh_breakout, fresh_experimental)
        except Exception as e:
            print(f"⚠️ [محفظة وهمية] خطأ غير متوقع أثناء دورتها — تم تجاهله ولن يؤثر على "
                  f"البوت الحقيقي: {e}")

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
          f"إشارات مبكرة جديدة: {len(fresh_early)} (تحديثات: {len(early_updates)}) | إشارات انفجار جديدة: {len(fresh_breakout)} | "
          f"إشارات تجريبية جديدة: {len(fresh_experimental)}")

    for r in fresh:
        caution = market_caution and not r["symbol"].startswith("BTC")
        alert_text = format_alert(r, caution)
        r["_msg_id"] = send_telegram(alert_text)
        r["_alert_text"] = alert_text
        time.sleep(1)  # تجنب تجاوز حد تيليجرام لعدد الرسائل بالثانية

    for r in fresh_early:
        _st = _early_state_of.get(r["symbol"])
        alert_text = format_early_alert(r, _st)
        r["_alert_text"] = alert_text
        r["_msg_id"] = send_telegram(alert_text)
        if _st is not None:
            _st["msg_id"] = r["_msg_id"]
        time.sleep(1)

    # تحديثات تطور الإشارة (ترقية/تحديث/خفض/إلغاء): ردّ على رسالة الإشارة الأصلية
    for ev in early_updates:
        try:
            send_telegram(format_early_update(ev), reply_to=ev["state"].get("msg_id"))
        except Exception as _ue:
            print(f"⚠️ [early_v2] فشل إرسال تحديث {ev.get('symbol')}: {_ue}")
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
    open_new_positions(open_positions, fresh, market_regime)
    open_new_early_positions(open_positions, fresh_early, market_regime)
    open_new_breakout_positions(open_positions, fresh_breakout, market_regime)
    open_new_experimental_positions(open_positions, fresh_experimental, market_regime)

    # حفظ موحّد: ذاكرة الإشارات (رسمية + مبكرة + انفجار + تجريبية) + BTC Dominance + الصفقات المفتوحة + أرشيف الصفقات المغلقة حديثًا
    # + إحصائيات أداء محسوبة من السجل المحدَّث (خيار 3: تتبع فقط، بدون تعديل تلقائي على منطق البوت)
    save_all_state(
        build_alerted_keys(strong, early_eligible, breakout_eligible, experimental_eligible),
        btc_dominance, open_positions, closed_now, gist_files, early_state=early_state_new
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
