"""
ماسح السوق — نسخة البايثون (تعمل بجدولة تلقائية عبر GitHub Actions)
نفس منطق أداة HTML: فلترة سيولة/حركة -> تحليل عميق -> تأكيد فريم أعلى -> استقرار -> تنبيه تيليجرام

يضيف أيضًا مسارًا مستقلاً لـ"إشارات مبكرة" (انضغاط تقلب / تراكم صامت) لعملات لم تصل بعد
لإشارة شراء كاملة، كتحذير رادار بدون خطة دخول مؤكدة — لتفادي مشكلة "شراء القمة" حيث
الإشارة الرسمية تصل بعد ما الحركة صارت واضحة للجميع.

الإشارة المبكرة: نفس منطق النسخة القديمة بالضبط (squeeze / accumulation / divergence / momentum،
عدد الأهداف = عدد الشروط المتحققة، SL = 2×ATR، تُقلَّم الأهداف عند أقرب مقاومة، وقالب الرسالة نفسه).
تم حذف نظام early_v2 (Confluence بالنقاط) بالكامل.

يضيف كذلك فلتر "إرهاق/امتداد زائد" (Overextension) يعاقب درجة الإشارات الرسمية نفسها لو
السعر بعيد جدًا عن EMA50 بوحدات ATR — لمعالجة نفس مشكلة "شراء القمة" من جهة الإشارة
الرسمية مباشرة، وليس فقط عبر تحذير مبكر منفصل.

الإشارة الرسمية: منطق النسخة القديمة بالضبط (نظام نقاط score_at + تأكيد الفريم الأعلى + بونص Squeeze،
وتُقبل عند score >= 1.5 مع vol_confirm وATR% وpersistent وغير عرضية وبعيدة عن المقاومة).

الإشارة التجريبية (confluence_v1): نظام موحّد من 5 مؤشرات احترافية بأدوار مختلفة:
Anchored VWAP (السياق) + Volume Profile POC/VAH/VAL (المستويات) + CVD (التأكيد) +
TTM Squeeze (التوقيت) + Open Interest/Funding Rate (فلتر ازدحام Long)، ووقف الخسارة بـ ATR.
"""

import os
import sys
import json
import time
import traceback
import datetime as dt
import concurrent.futures
import threading
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

# ---------- الإشارة الرسمية: خيارات تشغيل ----------
# الإشارة الرسمية بمنطقها القديم بالضبط. الفلاتر الجديدة (النموذج التكيفي وسياسة market_regime) كانت تُطبَّق
# على الرسمية في النسخة الحديثة فقط؛ هي معطّلة هنا افتراضيًا لتطابق الرسمية القديمة، ويمكن تفعيلها بـ=1.
OFFICIAL_USE_ADAPTIVE = os.environ.get("OFFICIAL_USE_ADAPTIVE", "0") == "1"
OFFICIAL_USE_REGIME = os.environ.get("OFFICIAL_USE_REGIME", "1") == "1"

# ---------- إعدادات الإشارات المبكرة (انضغاط تقلب / تراكم صامت) ----------
SQUEEZE_LOOKBACK = 20           # عدد الشموع لحساب متوسط عرض نطاق Bollinger
SQUEEZE_RATIO_THRESHOLD = 0.6   # عرض النطاق الحالي <= هذه النسبة من المتوسط -> يُعتبر انضغاطًا
ACCUM_WINDOW = 20               # عدد الشموع لقياس التراكم الصامت
ACCUM_PRICE_MAX_MOVE_PCT = 4.0  # أقصى تحرك سعري% خلال النافذة كي يُعتبر السعر "شبه ثابت" (قيمة النسخة القديمة)
ACCUM_FLOW_RATIO_MIN = 0.3      # أدنى نسبة صافي تدفق شراء (OBV/حجم) كي يُعتبر تراكمًا واضحًا (قيمة النسخة القديمة)

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

# ---------- الإشارة التجريبية (النسخة الجديدة): نظام Confluence من 5 مؤشرات احترافية ----------
# الأدوار: Anchored VWAP = السياق | Volume Profile = المستويات | CVD = التأكيد |
#          TTM Squeeze = التوقيت | OI + Funding = فلتر الازدحام (Crowding) | ATR = وقف الخسارة
# (Ichimoku/MFI القديمين لم يعودا يُستخدمان بالتجريبية؛ دوالهما ثابتة بالملف للتوافق مع أي أداة أخرى)
EXPERIMENTAL_ENGINE_VERSION = "confluence_v1"

# 1) Anchored VWAP — يُثبَّت على أدنى قاع محوري (Swing Low) مؤكَّد خلال النافذة
AVWAP_ANCHOR_LOOKBACK = int(os.environ.get("AVWAP_ANCHOR_LOOKBACK", "120"))   # عدد الشموع للبحث عن القاع المرجعي
AVWAP_PIVOT_SPAN = 3                                                           # شموع على كل جانب لتأكيد القاع المحوري
AVWAP_MIN_ANCHOR_BARS = 12                                                     # أقل عمر للمرساة (وإلا VWAP≈السعر بلا معنى)
AVWAP_MAX_DIST_ATR = float(os.environ.get("AVWAP_MAX_DIST_ATR", "6.0"))       # أقصى بعد للسعر فوق AVWAP بوحدات ATR (منع مطاردة الصعود)

# 2) Volume Profile — POC / VAH / VAL
VP_LOOKBACK = int(os.environ.get("VP_LOOKBACK", "100"))                        # شموع بناء البروفايل
VP_BINS = int(os.environ.get("VP_BINS", "40"))                                 # عدد المستويات السعرية
VP_VALUE_AREA_PCT = 0.70                                                       # نسبة الحجم داخل Value Area
VP_RECLAIM_LOOKBACK = 5                                                        # شموع للبحث عن خروج سابق من منطقة القيمة قبل العودة
VP_MAX_ABOVE_VAH_ATR = float(os.environ.get("VP_MAX_ABOVE_VAH_ATR", "2.0"))   # أبعد من هذا فوق VAH = ممتد (لا مطاردة)

# 3) CVD (Cumulative Volume Delta) من taker_buy_base لكل شمعة (بدون أي طلب إضافي)
CVD_WINDOW = int(os.environ.get("CVD_WINDOW", "20"))
CVD_RECENT = 3                                                                 # آخر شموع يجب أن يكون صافي دلتاها موجبًا
CVD_DIV_TOL = float(os.environ.get("CVD_DIV_TOL", "0.05"))                     # فجوة CVD عن قمته (كنسبة من حجم النافذة) تُعتبر Bearish Divergence لو السعر عند قمة

# 4) TTM Squeeze (Bollinger داخل Keltner) + Momentum (Linear Regression)
TTM_LENGTH = 20
TTM_BB_MULT = 2.0
TTM_KC_MULT = 1.5
TTM_FIRE_LOOKBACK = int(os.environ.get("TTM_FIRE_LOOKBACK", "3"))             # آخر X شموع يُقبل فيها انفجار الانضغاط
TTM_MIN_SQUEEZE_BARS = int(os.environ.get("TTM_MIN_SQUEEZE_BARS", "4"))       # أقل مدة انضغاط قبل الانفجار
TTM_WINDOW_BARS = 160                                                          # نافذة الحساب (كفاية للإحماء + الفحص)

# 5) Open Interest + Funding Rate (عقود دائمة) — فلتر ازدحام اختياري، يفشل بأمان (محايد) لو البيانات غير متاحة
EXPERIMENTAL_USE_DERIVATIVES = os.environ.get("EXPERIMENTAL_USE_DERIVATIVES", "1") != "0"
FUNDING_HIGH = float(os.environ.get("FUNDING_HIGH", "0.0003"))                 # 0.03% لكل 8 ساعات = تمويل مرتفع
FUNDING_EXTREME = float(os.environ.get("FUNDING_EXTREME", "0.0010"))           # 0.10% = ازدحام Long شديد (رفض مباشر)
OI_RISE_PCT = float(os.environ.get("OI_RISE_PCT", "3.0"))                      # ارتفاع OI% خلال DERIV_OI_BARS شموع
DERIV_OI_BARS = 6
DERIV_TIMEOUT = 6                                                              # ثوانٍ، بدون إعادة محاولة (حتى لا يبطئ المسح لو الموقع محجوب)
DERIV_MAX_PROVIDER_FAILS = 3                                                   # بعد هذا العدد من الفشل المتتالي يُتخطى المزوّد لباقي التشغيلة
BINANCE_FAPI_BASE = "https://fapi.binance.com"
OKX_BASE = "https://www.okx.com"

# ---------------- إعدادات الحد الأدنى لنسبة الربح المستهدفة ----------------
# لا تُرسل أي إشارة (رسمية أو مبكرة) إلا لو كانت نسبة الربح المتوقعة عند أول هدف (TP1)
# مقارنة بسعر الدخول >= هذه النسبة% — لتفادي إشارات ذات هدف قريب جدًا لا يستحق الدخول
MIN_PROFIT_PCT = float(os.environ.get("MIN_PROFIT_PCT", "1.0"))

# ---------------- إعدادات أهداف الإشارات المبكرة (تقديرية، أقل ثقة من الإشارة الرسمية) ----------------
# وقف خسارة أوسع من الإشارة الرسمية (1.5×ATR) لأن نقطة الدخول أقل دقة والتقلب حولها أعلى
EARLY_SL_ATR_MULT = 2.0
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
# --- تحسينات تصنيف النظام (Hysteresis + حد أدنى للميل + تأكيد الاتجاه) ---
# Hysteresis: الدخول في "ranging" عندما ينزل ADX تحت ENTER، والخروج منه فقط عندما يصعد فوق EXIT.
# بينهما تبقى الحالة السابقة (تُعاد محاكاتها على تاريخ الشموع، فلا حاجة لتخزين حالة بين التشغيلات).
# مستقلة عن ADX_THRESHOLD حتى لا يتأثر فلتر "السوق العرضي" الخاص بكل عملة.
REGIME_ADX_ENTER_RANGING = float(os.environ.get("REGIME_ADX_ENTER_RANGING", "18"))
REGIME_ADX_EXIT_RANGING = float(os.environ.get("REGIME_ADX_EXIT_RANGING", "22"))
# حد أدنى لميل EMA50 (نسبة مئوية خلال MARKET_REGIME_SLOPE_LOOKBACK شمعة)؛ أقل منه = ranging
REGIME_MIN_SLOPE_PCT = float(os.environ.get("REGIME_MIN_SLOPE_PCT", "0.10"))
# عدد التأكيدات المطلوبة من اثنين: (السعر على نفس جهة EMA50) و(+DI/-DI بنفس الاتجاه). 0 = تعطيل التأكيد
REGIME_MIN_CONFIRMS = int(os.environ.get("REGIME_MIN_CONFIRMS", "1"))

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
    "trending_up":   {"official": "allow",  "early": "allow",  "breakout": "allow",  "experimental": "allow"},
    "ranging":       {"official": "strict", "early": "strict", "breakout": "strict", "experimental": "strict"},
    "trending_down": {"official": "block",  "early": "block",  "breakout": "block",  "experimental": "block"},
}
try:
    for _reg, _pol in json.loads(os.environ.get("REGIME_POLICY_JSON", "{}") or "{}").items():
        REGIME_POLICY.setdefault(_reg, {}).update(_pol)
except Exception as _e:
    print(f"⚠️ REGIME_POLICY_JSON غير صالح — استُخدمت السياسة الافتراضية: {_e}")

# ---------- نظام السوق الاحترافي (Composite Regime) ----------
# REGIME_MODEL="legacy" يعيد السلوك القديم حرفيًا (ADX + ميل EMA50 على BTC فقط).
# "pro" (الافتراضي) يجمع عدة أصوات في درجة واحدة [-1..+1] ثم يحوّلها لنفس التسميات الثلاث
# (trending_up / ranging / trending_down) فتبقى REGIME_POLICY والتسجيل والإحصائيات تعمل كما هي:
#   primary   = تصنيف BTC على إطار السكانر (المنطق القديم نفسه)
#   htf/htf2  = نفس التصنيف على الإطارين الأعلى (1h -> 4h -> 1d) بوزن أكبر
#   structure = هيكل السوق (HH/HL مقابل LH/LL + كسر آخر قمة/قاع) -- يسبق EMA في كشف الانعكاس
#   breadth   = اتساع السوق: نسبة الصاعدين 24س + نسبة العملات فوق EMA50 (للعملات البديلة)
# التقلب (Volatility Regime) لا يصوّت على الاتجاه: يضبط risk_mult ويشدّد السياسة عند "extreme".
REGIME_MODEL = os.environ.get("REGIME_MODEL", "pro").strip().lower()
REGIME_FETCH_LIMIT = int(os.environ.get("REGIME_FETCH_LIMIT", "1000"))   # شموع BTC على إطار السكانر (لنسبة التقلب المئوية)
REGIME_W_PRIMARY = float(os.environ.get("REGIME_W_PRIMARY", "1.0"))
REGIME_W_HTF = float(os.environ.get("REGIME_W_HTF", "1.5"))
REGIME_W_HTF2 = float(os.environ.get("REGIME_W_HTF2", "1.0"))
REGIME_W_STRUCTURE = float(os.environ.get("REGIME_W_STRUCTURE", "1.0"))
REGIME_W_BREADTH = float(os.environ.get("REGIME_W_BREADTH", "1.0"))
REGIME_UP_SCORE = float(os.environ.get("REGIME_UP_SCORE", "0.35"))       # الدرجة >= هذا = trending_up
REGIME_DOWN_SCORE = float(os.environ.get("REGIME_DOWN_SCORE", "-0.35"))  # الدرجة <= هذا = trending_down
# Veto الفريم الأعلى: لا نعلن trending_up ضد فريم أعلى هابط (ولا trending_down ضد فريم أعلى صاعد) -> ranging
REGIME_HTF_VETO = os.environ.get("REGIME_HTF_VETO", "1") != "0"
# أسرع في التحوّط من العودة للمخاطرة: لو primary هابط وهيكل السوق غير صاعد والفريم الأعلى غير صاعد -> trending_down
# حتى لو الدرجة الكلية لم تصل العتبة (Asymmetry: التحوّط السريع، والعودة البطيئة المؤكدة)
REGIME_FAST_DERISK = os.environ.get("REGIME_FAST_DERISK", "1") != "0"
REGIME_PIVOT_SPAN = int(os.environ.get("REGIME_PIVOT_SPAN", "3"))
REGIME_STRUCTURE_LOOKBACK = int(os.environ.get("REGIME_STRUCTURE_LOOKBACK", "150"))
REGIME_BREADTH_BAND = float(os.environ.get("REGIME_BREADTH_BAND", "0.20"))  # انحراف عن 50% يعطي صوتًا كاملًا
REGIME_BREADTH_MIN_TICKERS = int(os.environ.get("REGIME_BREADTH_MIN_TICKERS", "30"))
REGIME_BREADTH_MIN_RESULTS = int(os.environ.get("REGIME_BREADTH_MIN_RESULTS", "15"))
# Volatility regime: النسبة المئوية لـ ATR% الحالي داخل تاريخ آخر REGIME_FETCH_LIMIT شمعة
REGIME_VOL_LOW_PCT = float(os.environ.get("REGIME_VOL_LOW_PCT", "20"))
REGIME_VOL_HIGH_PCT = float(os.environ.get("REGIME_VOL_HIGH_PCT", "80"))
REGIME_VOL_EXTREME_PCT = float(os.environ.get("REGIME_VOL_EXTREME_PCT", "95"))
# عند extreme: allow -> strict و strict -> block ("لا تتداول في سوق فوضوي"). 0 = تعطيل.
REGIME_VOL_ESCALATE = os.environ.get("REGIME_VOL_ESCALATE", "1") != "0"
# مضاعف مخاطرة مقترح حسب التقلب -- يُسجَّل مع كل صفقة (regime_risk_mult)؛ هذا الملف لا يحدد حجم المراكز
REGIME_RISK_MULT = {"low": 1.0, "normal": 1.0, "high": 0.75, "extreme": 0.5}


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


# ======================================================================================
# الإشارة التجريبية — نظام Confluence (confluence_v1)
# كل دالة تعمل على القوائم الخام (highs/lows/closes/vols/taker_buy) وبدون أي طلب شبكة،
# عدا fetch_derivatives (OI + Funding) التي تُستدعى آخر خطوة وفقط للمرشحين الناجين.
# ======================================================================================

_EXP_FUNNEL_STAGES = ("analyzed", "ttm", "filters", "avwap", "cvd", "vp", "deriv", "signal")
_EXP_FUNNEL = {k: 0 for k in _EXP_FUNNEL_STAGES}
_EXP_LOCK = threading.Lock()


def _funnel(stage):
    """عدّاد قمع (Funnel) لمعرفة عند أي مؤشر تتوقف العملات — يُطبع آخر كل مسح لتسهيل المعايرة."""
    with _EXP_LOCK:
        _EXP_FUNNEL[stage] = _EXP_FUNNEL.get(stage, 0) + 1


def funnel_summary():
    return " → ".join(f"{k}={_EXP_FUNNEL.get(k, 0)}" for k in _EXP_FUNNEL_STAGES)


# ---------------- 1) Anchored VWAP (السياق) ----------------

def anchored_vwap_from(highs, lows, closes, vols, anchor):
    """VWAP تراكمي يبدأ من شمعة المرساة anchor: Σ(typical×vol) / Σ(vol). قبل المرساة = None."""
    n = len(closes)
    out = [None] * n
    num = den = 0.0
    for j in range(anchor, n):
        tp = (highs[j] + lows[j] + closes[j]) / 3
        num += tp * vols[j]
        den += vols[j]
        out[j] = (num / den) if den > 0 else None
    return out


def avwap_anchor_swing_low(lows, lookback=AVWAP_ANCHOR_LOOKBACK, span=AVWAP_PIVOT_SPAN,
                           min_bars=AVWAP_MIN_ANCHOR_BARS):
    """أدنى قاع محوري مؤكَّد (أدنى من span شموع على كل جانب) داخل النافذة، بعمر >= min_bars."""
    n = len(lows)
    start = max(span, n - lookback)
    end = n - max(min_bars, span + 1)      # آخر فهرس مسموح كمرساة
    best = None
    for j in range(start, end + 1):
        if j - span < 0 or j + span >= n:
            continue
        if lows[j] <= min(lows[j - span:j + span + 1]):
            if best is None or lows[j] < lows[best]:
                best = j
    return best


def avwap_context(highs, lows, closes, vols, atrv):
    """ok = السعر فوق AVWAP (المشترون مسيطرون منذ القاع) وغير بعيد عنه أكثر من AVWAP_MAX_DIST_ATR×ATR."""
    if atrv <= 0:
        return None
    anchor = avwap_anchor_swing_low(lows)
    if anchor is None:
        return None
    av = anchored_vwap_from(highs, lows, closes, vols, anchor)
    v = av[-1]
    if v is None:
        return None
    price = closes[-1]
    dist = (price - v) / atrv
    return {
        "ok": price > v and dist <= AVWAP_MAX_DIST_ATR,
        "avwap": v,
        "dist_atr": dist,
        "anchor_bars_ago": len(closes) - 1 - anchor,
    }


# ---------------- 2) Volume Profile (المستويات) ----------------

def volume_profile(highs, lows, closes, vols, lookback=VP_LOOKBACK, bins=VP_BINS, va_pct=VP_VALUE_AREA_PCT):
    """
    يوزّع حجم كل شمعة بالتساوي على المستويات السعرية التي يلمسها مدى الشمعة (low→high)،
    ثم يستخرج POC (أعلى مستوى حجمًا) وحدّي Value Area (VAL/VAH) الذين يحتويان va_pct من الحجم
    بالتوسّع من POC نحو الجار الأعلى حجمًا.
    """
    n = len(closes)
    start = max(0, n - lookback)
    lo = min(lows[start:])
    hi = max(highs[start:])
    if not (hi > lo):
        return None
    step = (hi - lo) / bins
    hist = [0.0] * bins
    for j in range(start, n):
        v = vols[j]
        if v <= 0:
            continue
        b0 = min(bins - 1, max(0, int((lows[j] - lo) / step)))
        b1 = min(bins - 1, max(0, int((highs[j] - lo) / step)))
        share = v / (b1 - b0 + 1)
        for b in range(b0, b1 + 1):
            hist[b] += share
    total = sum(hist)
    if total <= 0:
        return None
    poc_b = max(range(bins), key=lambda b: hist[b])
    lo_b = hi_b = poc_b
    acc = hist[poc_b]
    while acc < total * va_pct and (lo_b > 0 or hi_b < bins - 1):
        up = hist[hi_b + 1] if hi_b < bins - 1 else -1.0
        dn = hist[lo_b - 1] if lo_b > 0 else -1.0
        if up >= dn:
            hi_b += 1
            acc += hist[hi_b]
        else:
            lo_b -= 1
            acc += hist[lo_b]
    return {
        "poc": lo + (poc_b + 0.5) * step,
        "vah": lo + (hi_b + 1) * step,
        "val": lo + lo_b * step,
    }


def volume_profile_context(highs, lows, closes, vols, atrv):
    """
    حالات مقبولة للشراء (ok): reclaim_val (عودة لمنطقة القيمة بعد خروج تحتها) | above_poc / reclaim_poc
    (فوق POC) | break_above_vah / accept_above_vah (اختراق VAH). strong = قبول فوق VAH بإغلاقين أو استعادة POC.
    مرفوضة: below_value (تحت VAL) | inside_lower_value (بين VAL وPOC بدون عودة) | extended_above_value (مطاردة).
    """
    if atrv <= 0 or len(closes) < VP_RECLAIM_LOOKBACK + 3:
        return None
    vp = volume_profile(highs, lows, closes, vols)
    if not vp:
        return None
    c = closes[-1]
    poc, vah, val = vp["poc"], vp["vah"], vp["val"]
    prev = closes[-1 - VP_RECLAIM_LOOKBACK:-1]
    was_below_val = any(x < val for x in prev)
    was_below_poc = any(x < poc for x in prev)
    ok = strong = False
    if c < val:
        state = "below_value"
    elif c > vah + VP_MAX_ABOVE_VAH_ATR * atrv:
        state = "extended_above_value"
    elif c > vah:
        accepted = closes[-2] > vah
        state = "accept_above_vah" if accepted else "break_above_vah"
        ok, strong = True, accepted
    elif c >= poc:
        state = "reclaim_poc" if was_below_poc else "above_poc"
        ok, strong = True, was_below_poc
    else:
        state = "reclaim_val" if was_below_val else "inside_lower_value"
        ok = was_below_val
    return {"ok": ok, "strong": strong, "state": state, **vp}


# ---------------- 3) CVD (التأكيد) ----------------

def cvd_context(vols, taker_buy, closes, window=CVD_WINDOW):
    """
    delta = شراء عدواني − بيع عدواني = 2×taker_buy − volume، و CVD = المجموع التراكمي.
    ok: صافي الدلتا خلال النافذة موجب + آخر CVD_RECENT شموع موجبة + لا Bearish Divergence
    (السعر عند قمة النافذة بينما CVD تحت قمته بأكثر من CVD_DIV_TOL من حجم النافذة).
    strong: CVD عند قمة جديدة داخل النافذة.
    """
    n = len(closes)
    if n < window + 2 or len(taker_buy) != n:
        return None
    tail = taker_buy[-(window + 1):]
    if any(x is None for x in tail):
        return None
    delta = []
    for v, tb in zip(vols, taker_buy):
        delta.append((2 * tb - v) if tb is not None else 0.0)
    cvd, run = [], 0.0
    for d in delta:
        run += d
        cvd.append(run)
    vol_sum = sum(vols[-window:])
    if vol_sum <= 0:
        return None
    net = cvd[-1] - cvd[-1 - window]
    recent = sum(delta[-CVD_RECENT:])
    price_at_high = closes[-1] >= max(closes[-window:]) * 0.998
    cvd_hi = max(cvd[-window:])
    gap = (cvd_hi - cvd[-1]) / vol_sum
    bearish_div = price_at_high and gap > CVD_DIV_TOL
    ok = net > 0 and recent > 0 and not bearish_div
    return {
        "ok": ok,
        "strong": ok and cvd[-1] >= cvd_hi,
        "bearish_div": bearish_div,
        "flow_ratio": net / vol_sum,
    }


# ---------------- 4) TTM Squeeze (التوقيت) ----------------

def _linreg_endpoint(ys):
    """قيمة خط الانحدار الخطي عند آخر نقطة (كما في Momentum الخاص بـ TTM Squeeze)."""
    L = len(ys)
    xm = (L - 1) / 2
    ym = sum(ys) / L
    num = sum((x - xm) * (y - ym) for x, y in enumerate(ys))
    den = sum((x - xm) ** 2 for x in range(L))
    slope = num / den if den else 0.0
    return ym + slope * (L - 1 - xm)


def ttm_squeeze_series(highs, lows, closes, length=TTM_LENGTH, bb_mult=TTM_BB_MULT, kc_mult=TTM_KC_MULT):
    """
    squeeze_on[i] = True لو نطاق Bollinger (±bb_mult×σ) داخل نطاق Keltner (±kc_mult×ATR) حول نفس المتوسط.
    momentum[i] = انحدار خطي لـ (close − متوسط[(أعلى قمة+أدنى قاع)/2 , SMA]) خلال length شمعة.
    """
    n = len(closes)
    sq = [None] * n
    mom = [None] * n
    if n < 2 * length + 1:
        return sq, mom
    tr = [0.0] * n
    for j in range(1, n):
        tr[j] = max(highs[j] - lows[j], abs(highs[j] - closes[j - 1]), abs(lows[j] - closes[j - 1]))
    sma = [None] * n
    val = [None] * n
    for i in range(length, n):
        win = closes[i - length + 1:i + 1]
        basis = sum(win) / length
        sma[i] = basis
        sd = (sum((x - basis) ** 2 for x in win) / length) ** 0.5
        atr_i = sum(tr[i - length + 1:i + 1]) / length
        sq[i] = (bb_mult * sd) < (kc_mult * atr_i)
        hh = max(highs[i - length + 1:i + 1])
        ll = min(lows[i - length + 1:i + 1])
        val[i] = closes[i] - (((hh + ll) / 2) + basis) / 2
    for i in range(2 * length - 1, n):
        mom[i] = _linreg_endpoint(val[i - length + 1:i + 1])
    return sq, mom


def ttm_squeeze_signal(highs, lows, closes):
    """
    توقيت الدخول: انفجار انضغاط (Squeeze Fire) حدث خلال آخر TTM_FIRE_LOOKBACK شموع بعد انضغاط
    دام >= TTM_MIN_SQUEEZE_BARS، مع Momentum موجب ومتزايد (اتجاه الانفجار صاعد).
    """
    h, l, c = highs[-TTM_WINDOW_BARS:], lows[-TTM_WINDOW_BARS:], closes[-TTM_WINDOW_BARS:]
    sq, mom = ttm_squeeze_series(h, l, c)
    i = len(sq) - 1
    out = {"ok": False, "fired": False, "momentum_ok": False, "squeeze_on_now": None,
           "squeeze_bars": 0, "bars_since_fire": None, "momentum": None}
    if i < 2 or mom[i] is None or mom[i - 1] is None:
        return out
    out["squeeze_on_now"] = sq[i]
    out["momentum"] = mom[i]
    out["momentum_ok"] = mom[i] > 0 and mom[i] > mom[i - 1]
    for j in range(i, i - TTM_FIRE_LOOKBACK, -1):
        if j < 1:
            break
        if sq[j] is False and sq[j - 1] is True:
            k, dur = j - 1, 0
            while k >= 0 and sq[k] is True:
                dur += 1
                k -= 1
            if dur >= TTM_MIN_SQUEEZE_BARS:
                out["fired"] = True
                out["squeeze_bars"] = dur
                out["bars_since_fire"] = i - j
                break
    out["ok"] = out["fired"] and out["momentum_ok"]
    return out


# ---------------- 5) Open Interest + Funding Rate (فلتر الازدحام) ----------------

_DERIV_LOCK = threading.Lock()
_DERIV_FAILS = {"binance": 0, "okx": 0}
_DERIV_CACHE = {}
_DERIV_STATS = {"ok": 0, "partial": 0, "unavailable": 0, "crowded": 0}


def _deriv_get(url, params, provider):
    """طلب واحد بلا إعادة محاولة. يرجع (json|None, حالة) — الحالة: ok | not_listed | fail | skipped."""
    with _DERIV_LOCK:
        if _DERIV_FAILS[provider] >= DERIV_MAX_PROVIDER_FAILS:
            return None, "skipped"
    try:
        r = requests.get(url, params=params, timeout=DERIV_TIMEOUT)
        if r.status_code == 400 and provider == "binance":
            return None, "not_listed"          # الرمز غير موجود بالعقود الدائمة (ليس عطلًا بالمزوّد)
        if r.status_code != 200:
            raise RuntimeError(f"status {r.status_code}")
        data = r.json()
        with _DERIV_LOCK:
            _DERIV_FAILS[provider] = 0
        return data, "ok"
    except Exception:
        with _DERIV_LOCK:
            _DERIV_FAILS[provider] += 1
        return None, "fail"


def _fetch_binance_deriv(symbol, interval):
    pi, st = _deriv_get(f"{BINANCE_FAPI_BASE}/fapi/v1/premiumIndex", {"symbol": symbol}, "binance")
    if pi is None:
        return None
    try:
        funding = float(pi["lastFundingRate"])
    except Exception:
        return None
    period = interval if interval in ("5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d") else "1h"
    hist, _ = _deriv_get(f"{BINANCE_FAPI_BASE}/futures/data/openInterestHist",
                         {"symbol": symbol, "period": period, "limit": DERIV_OI_BARS + 1}, "binance")
    oi_change = None
    try:
        if isinstance(hist, list) and len(hist) >= 2:
            first = float(hist[0]["sumOpenInterestValue"])
            last_ = float(hist[-1]["sumOpenInterestValue"])
            if first > 0:
                oi_change = (last_ / first - 1) * 100
    except Exception:
        oi_change = None
    return {"funding": funding, "oi_change_pct": oi_change, "source": "binance"}


def _fetch_okx_deriv(symbol):
    """مزوّد احتياطي (Funding فقط) لو Binance Futures محجوب من خادم التشغيل."""
    if not symbol.endswith("USDT"):
        return None
    inst = f"{symbol[:-4]}-USDT-SWAP"
    data, _ = _deriv_get(f"{OKX_BASE}/api/v5/public/funding-rate", {"instId": inst}, "okx")
    try:
        if not data or data.get("code") != "0" or not data.get("data"):
            return None
        return {"funding": float(data["data"][0]["fundingRate"]), "oi_change_pct": None, "source": "okx"}
    except Exception:
        return None


def fetch_derivatives(symbol, interval):
    """Funding + تغيّر OI. لا ترفع استثناءً أبدًا؛ ترجع None لو غير متاح. مُخزَّنة مؤقتًا طوال التشغيلة."""
    key = (symbol, interval)
    if key in _DERIV_CACHE:
        return _DERIV_CACHE[key]
    result = None
    try:
        result = _fetch_binance_deriv(symbol, interval) or _fetch_okx_deriv(symbol)
    except Exception:
        result = None
    _DERIV_CACHE[key] = result
    return result


def derivatives_verdict(deriv, price_change_pct):
    """
    ازدحام Long (Long Squeeze Risk) = Funding مرتفع + OI صاعد + السعر صاعد، أو Funding متطرف وحده.
    البيانات غير المتاحة = محايد (لا رفض ولا نقطة إضافية).
    """
    if not deriv or deriv.get("funding") is None:
        return {"status": "unavailable", "crowded": False, "clean": False, "reason": None}
    f = deriv["funding"]
    oi = deriv.get("oi_change_pct")
    crowded, reason = False, None
    if f >= FUNDING_EXTREME:
        crowded, reason = True, "funding_extreme"
    elif (f >= FUNDING_HIGH and oi is not None and oi >= OI_RISE_PCT
          and price_change_pct is not None and price_change_pct > 0):
        crowded, reason = True, "long_crowded"
    return {"status": "ok" if oi is not None else "partial", "crowded": crowded,
            "clean": not crowded, "reason": reason}


# ---------------- الدمج: نظام Confluence ----------------

def experimental_confluence(ind, i, symbol, interval, atrp, near_resistance):
    """
    البوابات الإلزامية (كلها يجب أن تتحقق): TTM Squeeze Fire (التوقيت) → فلاتر ATR%/المقاومة →
    Anchored VWAP (السياق) → CVD (التأكيد) → Volume Profile (المستويات) → OI+Funding (لا ازدحام Long).
    الدرجة (4 إلى 7) = 4 بوابات أساسية + نقطة لكل من: vp_strong، cvd_strong، deriv_clean (بيانات متاحة ونظيفة).
    """
    res = {"triggered": False, "score": 0, "details": {}, "vp_levels": [],
           "context": {"engine": EXPERIMENTAL_ENGINE_VERSION}}
    ctx = res["context"]
    highs, lows, closes, vols = ind["highs"][:i + 1], ind["lows"][:i + 1], ind["closes"][:i + 1], ind["vols"][:i + 1]
    taker_buy = ind["taker_buy"][:i + 1]
    _funnel("analyzed")

    # 4) التوقيت: انفجار Squeeze
    ttm = ttm_squeeze_signal(highs, lows, closes)
    ctx.update({"squeeze_bars": ttm["squeeze_bars"], "bars_since_fire": ttm["bars_since_fire"],
                "ttm_momentum": ttm["momentum"]})
    if not ttm["ok"]:
        ctx["reject"] = "no_ttm_fire"
        return res
    _funnel("ttm")

    # شرطا استبعاد صريحان موروثان من النسخة السابقة
    if atrp < EXPERIMENTAL_MIN_ATR_PCT or near_resistance:
        ctx["reject"] = "low_atr" if atrp < EXPERIMENTAL_MIN_ATR_PCT else "near_resistance"
        return res
    _funnel("filters")
    atrv = atr_value_at(ind, i)

    # 1) السياق: Anchored VWAP
    av = avwap_context(highs, lows, closes, vols, atrv)
    if av:
        ctx.update({"avwap": av["avwap"], "avwap_dist_atr": av["dist_atr"], "avwap_anchor_bars_ago": av["anchor_bars_ago"]})
    if not av or not av["ok"]:
        ctx["reject"] = "avwap"
        return res
    _funnel("avwap")

    # 3) التأكيد: CVD
    cvd = cvd_context(vols, taker_buy, closes)
    if cvd:
        ctx.update({"cvd_flow_ratio": cvd["flow_ratio"], "cvd_bearish_div": cvd["bearish_div"]})
    if not cvd or not cvd["ok"]:
        ctx["reject"] = "cvd"
        return res
    _funnel("cvd")

    # 2) المستويات: Volume Profile
    vpc = volume_profile_context(highs, lows, closes, vols, atrv)
    if vpc:
        ctx.update({"vp_state": vpc["state"], "poc": vpc["poc"], "vah": vpc["vah"], "val": vpc["val"]})
    if not vpc or not vpc["ok"]:
        ctx["reject"] = "volume_profile"
        return res
    _funnel("vp")

    # 5) الازدحام: OI + Funding (آخر خطوة لأنها الوحيدة التي تحتاج شبكة)
    dv = {"status": "disabled", "crowded": False, "clean": False, "reason": None}
    if EXPERIMENTAL_USE_DERIVATIVES:
        deriv = fetch_derivatives(symbol, interval)
        price_chg = None
        if len(closes) > DERIV_OI_BARS and closes[-1 - DERIV_OI_BARS] > 0:
            price_chg = (closes[-1] / closes[-1 - DERIV_OI_BARS] - 1) * 100
        dv = derivatives_verdict(deriv, price_chg)
        if deriv:
            ctx.update({"funding": deriv.get("funding"), "oi_change_pct": deriv.get("oi_change_pct"),
                        "deriv_source": deriv.get("source")})
        with _EXP_LOCK:
            _DERIV_STATS["crowded" if dv["crowded"] else dv["status"]] = \
                _DERIV_STATS.get("crowded" if dv["crowded"] else dv["status"], 0) + 1
    ctx["deriv_status"] = dv["status"]
    if dv["crowded"]:
        ctx["reject"] = f"crowded:{dv['reason']}"
        return res
    _funnel("deriv")

    ema7, ema14 = ind.get("ema7"), ind.get("ema14")
    trend_support = bool(ema7 and ema14 and ema7[i] is not None and ema14[i] is not None and ema7[i] > ema14[i])
    vol_avg = ind["vol_avg"][i]
    vol_spike = vol_avg is not None and vols[-1] > vol_avg * EXPERIMENTAL_VOL_MULT
    res["details"] = {
        "avwap_ok": True, "vp_ok": True, "cvd_ok": True, "squeeze_fire": True,
        "vp_strong": bool(vpc["strong"]), "cvd_strong": bool(cvd["strong"]),
        "deriv_clean": bool(dv["clean"]),
        "trend_support": trend_support,
        "volume_confirm": bool(vol_spike and cvd["ok"]),
    }
    res["score"] = 4 + int(vpc["strong"]) + int(cvd["strong"]) + int(dv["clean"])
    res["vp_levels"] = [vpc["poc"], vpc["vah"]]
    res["triggered"] = True
    _funnel("signal")
    return res


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
    "experimental": ["trend_support", "volume_confirm", "vp_strong", "cvd_strong", "deriv_clean", "htf_aligned", "vol_confirm", "ranging", "near_resistance"],
}

# عوامل النوعين breakout/experimental تُحفظ داخل قاموس details وبمفتاح موجود فقط عند تحققها
# (الغياب = False). نسطّحها إلى حقول bool صريحة حتى يراها النموذج التكيفي وtrade_stats.
ADAPTIVE_DETAIL_FACTORS = {
    "breakout": ("trend_support", "macd_bull", "rsi_ok"),
    "experimental": ("trend_support", "volume_confirm", "vp_strong", "cvd_strong", "deriv_clean"),
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


# كاش شموع مشترك خلال التشغيلة الواحدة (يُستخدم لشموع الفريم الأعلى HTF) — يمنع تكرار نفس الطلب
_KLINES_CACHE = {}


def fetch_klines_cached(symbol, interval, limit):
    key = (symbol, interval, limit)
    hit = _KLINES_CACHE.get(key)
    if hit is not None:
        return hit
    data = fetch_klines(symbol, interval, limit)
    _KLINES_CACHE[key] = data
    return data



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
                        trend_dir = 1 if r["trend_up"] else -1
                        htf_aligned = htf_bullish_strict if r["trend_up"] else htf_bearish_strict
                        final_score += (trend_dir * 0.5) if htf_aligned else (-trend_dir * 0.5)
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

        # بونص Squeeze في درجة الإشارة الرسمية (يستخدم squeeze المفلتر بالاتجاه أعلاه).
        # score_before_squeeze_bonus = الدرجة قبل البونص (حقل early_gate_score، للتوافق).
        score_before_squeeze_bonus = final_score
        if squeeze and not accumulation:
            final_score += 1.5
        elif squeeze and accumulation:
            final_score += 0.5

        # الإشارة المبكرة — نفس منطق النسخة القديمة بالضبط:
        # squeeze الخام (بدون فلتر الاتجاه الخاص ببونص الدرجة الرسمية) + accumulation + divergence + momentum.
        # أهداف تقديرية: وقف خسارة أوسع (ATR×2) لأن الدخول أقل تأكيدًا، وعدد الأهداف = عدد الشروط
        # المتحققة (1 احتمالية، 2 مؤكدة، 3+ مؤكدة قوية)، مع إيقاف توليد الأهداف عند أول هدف
        # يتجاوز أقرب مقاومة معروفة.
        squeeze_early = volatility_squeeze(ind["bb_upper"], ind["bb_lower"], ind["closes"])
        accumulation = silent_accumulation(ind["closes"], ind["vols"], ind["obv"])
        early_entry = early_sl = None
        early_tps = []
        early_confidence = None
        momentum = momentum_strength(ind["macd"], ind["signal"], ind["rsi"], last)
        if squeeze_early or accumulation:
            conditions_met = sum([squeeze_early, accumulation, r["divergence"], momentum])
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
                early_tp_count = conditions_met  # عدد الأهداف = عدد الشروط المتحققة فعليًا (1 إلى 4)
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

        # إشارة تجريبية (confluence_v1): Anchored VWAP + Volume Profile + CVD + TTM Squeeze + OI/Funding —
        # مستقلة عن الرسمية والانفجار. كل البوابات إلزامية (انظر experimental_confluence)، والفلتران
        # الصريحان الموروثان (ATR% الضعيف / القرب من مقاومة) يُطبَّقان داخلها أيضًا.
        exp = experimental_confluence(ind, last, symbol, interval, atrp, r["near_resistance"])
        experimental = exp["triggered"]
        experimental_score = exp["score"]
        experimental_details = exp["details"]
        experimental_context = exp["context"]
        experimental_entry = experimental_sl = None
        experimental_tps = []
        if experimental:
            experimental_entry = ind["closes"][last]
            atrv = atr_value(ind)
            experimental_sl = experimental_entry - atrv * EXPERIMENTAL_SL_ATR_MULT
            risk = experimental_entry - experimental_sl
            # هدف واحد فقط: TP1 = 1R، يُقلَّم عند أقرب مستوى فوق الدخول من: المقاومة السابقة أو POC/VAH
            # من Volume Profile (مستويات الحجم تعمل كمغناطيس/مقاومة). لو بقي هامش الربح تحت الحد الأدنى
            # بعد التقليم يُرفض لاحقًا بفلتر meets_min_profit (لا مساحة كافية للحركة).
            tp1 = experimental_entry + risk
            caps = []
            resistance = r.get("resistance")
            if resistance and resistance > experimental_entry:
                caps.append(resistance)
            caps.extend(lvl for lvl in exp["vp_levels"] if lvl > experimental_entry)
            if caps and min(caps) < tp1:
                tp1 = min(caps)
            experimental_tps = [tp1]
            experimental_context["tp1_capped"] = bool(caps and min(caps) < experimental_entry + risk)

        # خطة دخول (شراء فقط — السوق الفوري لا يدعم فتح صفقة بيع مكشوفة):
        # وقف الخسارة من التقلب الفعلي (ATR) للعملة + هدف واحد فقط TP1.
        entry = sl = None
        tps = []
        # Divergence مُستبعد كليًا من الإشارة الرسمية (2026-09-14، تشديد إضافي): لا يكتفى
        # بعدم منحه وزنًا في score_at، بل تُرفض خطة الدخول الرسمية كليًا لو كان حاضرًا،
        # حتى لو حقّقت باقي العوامل درجة كافية -- Divergence يبقى مفيدًا فقط كأحد شروط
        # الإشارة المبكرة (r["divergence"] ما زال يُحسب ويُرجع لهذا الغرض).
        if final_score >= 1 and not r["divergence"]:
            entry = ind["closes"][last]
            atrv = atr_value(ind)
            sl = entry - atrv * 1.5
            risk = entry - sl

            # هدف واحد فقط: TP1 = 1R، مع احترام أقرب مقاومة إن كانت قبل TP1.
            tps = [entry + risk]
            resistance = r.get("resistance")
            if resistance and tps[0] >= resistance:
                tps[0] = resistance

        return {
            "symbol": symbol,
            "price": ind["closes"][last],
            "above_ema50": (ind["closes"][last] > ind["ema50"][last]) if ind.get("ema50") else None,  # لحساب breadth
            "change_pct": float(t["priceChangePercent"]),
            "score": final_score,
            "early_gate_score": score_before_squeeze_bonus,
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
            "squeeze": squeeze_early,          # الخام (يُستخدم لأهلية الإشارة المبكرة، كالقديم)
            "squeeze_trend_filtered": squeeze,  # المفلتر بالاتجاه (يُستخدم فقط لبونص الدرجة الرسمية)
            "accumulation": accumulation,
            "momentum": momentum,
            "breakout": breakout,
            "breakout_score": breakout_score,
            "breakout_details": breakout_details,
            "experimental": experimental,
            "experimental_score": experimental_score,
            "experimental_details": experimental_details,
            "experimental_context": experimental_context,
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
        if r.get("breakout_tps"):
            lines.append(f"TP1: {r['breakout_tps'][0]:.6g}")
        lines.append(f"SL: {r['breakout_sl']:.6g}")

    return "\n".join(lines)


def format_experimental_alert(r):
    """
    إشارة تجريبية (confluence_v1): Anchored VWAP + Volume Profile + CVD + TTM Squeeze (+ OI/Funding
    لو البيانات متاحة ونظيفة). نفس قالب المبكرة والانفجار: المصدر بالإنجليزية الخام.
    """
    e_score = r.get("experimental_score", 0)
    dot = "🧪🟢" if e_score >= 6 else ("🧪🟡" if e_score >= 5 else "🧪⚪")
    details = r.get("experimental_details", {}) or {}
    ctx = r.get("experimental_context", {}) or {}
    factors = ["avwap", f"vp({ctx.get('vp_state', '?')})", "cvd", "ttm_squeeze"]
    if details.get("deriv_clean"):
        factors.append("oi_funding")
    source_label = "+".join(factors)

    lines = [
        f"{dot} إشارة تجريبية",
        r['symbol'].replace('USDT', '/USDT'),
        f"المصدر: {source_label}",
    ]

    if r.get("experimental_entry") is not None:
        lines.append(f"الدخول: {r['experimental_entry']:.6g}")
        if r.get("experimental_tps"):
            lines.append(f"TP1: {r['experimental_tps'][0]:.6g}")
        lines.append(f"SL: {r['experimental_sl']:.6g}")

    levels = []
    if ctx.get("avwap") is not None:
        levels.append(f"AVWAP {ctx['avwap']:.6g}")
    if ctx.get("poc") is not None:
        levels.append(f"POC {ctx['poc']:.6g}")
    if ctx.get("vah") is not None:
        levels.append(f"VAH {ctx['vah']:.6g}")
    if ctx.get("funding") is not None:
        levels.append(f"Funding {ctx['funding'] * 100:.3f}%")
    if levels:
        lines.append(" | ".join(levels))

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


def _read_archive_chain(main_gist_files):
    """يقرأ سلسلة Gists الأرشيف بأمان: ملف سلسلة تالف يرفع GistContentError (لا يُعتبر فارغًا
    فتُكتب سلسلة جديدة فوقه وتضيع الأرشيفات القديمة). لو الملف غائب لكن توجد ملفات أرشيف
    داخل الـGist الرئيسي، تُدرج GIST_ID أولًا بالسلسلة كي لا تُهمَل تلك الأرشيفات."""
    raw = _gist_get_file(ARCHIVE_CHAIN_FILE, main_gist_files)
    chain = []
    if raw:
        try:
            chain = json.loads(raw)
            if not isinstance(chain, list):
                raise ValueError("ليست قائمة")
        except Exception as e:
            raise GistContentError(f"{ARCHIVE_CHAIN_FILE} تالف — لن يُكتب فوقه: {e}") from e
    main_has_archive = any(fn.startswith(ARCHIVE_PREFIX) for fn in main_gist_files)
    if main_has_archive and GIST_ID and GIST_ID not in chain:
        chain.insert(0, GIST_ID)
    return chain


def get_active_archive_gist(main_gist_files):
    """
    يرجع (معرف Gist الأرشيف النشط حاليًا للكتابة، قائمة سلسلة الـGists، ملفاته الحالية).
    ينشئ Gist أرشيف جديد تلقائيًا لما عدد ملفات الأرشيف بالنشط الحالي يقارب حد GitHub
    الفعلي (300 ملف/Gist) — هذا يمنع فشل الحفظ الصامت بعد تراكم عدد كبير جدًا من
    الصفقات المغلقة على المدى الطويل (كل Gist أرشيف قديم يبقى مقروءًا للأبد، فقط
    التوسع الجديد ينتقل لـGist لاحق).
    """
    chain = _read_archive_chain(main_gist_files)

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
        old_chain = None  # تالف: get_active_archive_gist يكون قد رفع الخطأ قبل الوصول هنا
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
    # الإشارة التجريبية تغيّر منطقها بالكامل (Ichimoku/MFI -> confluence_v1): صفقات المنطق القديم لا
    # تُحتسب في تعلّم النموذج التكيفي (عوامل مختلفة تمامًا)، فتبدأ التجريبية من insufficient_history.
    if history:
        history = [t for t in history
                   if not (_adaptive_type(t) == "experimental"
                           and t.get("experimental_engine") != EXPERIMENTAL_ENGINE_VERSION)]
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
    mode = _effective_regime_mode(market_regime, signal_type)
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
        mode = _effective_regime_mode(market_regime, signal_type)
        print(f"🧭 regime={market_regime} | {signal_type} ({mode}): استُبعد {dropped} من {len(candidates)}")
    return kept


def _load_archive_trades(gist_files):
    """كل صفقات ملفات الأرشيف عبر سلسلة الـGists (بدون السجل النشط). يرفع الاستثناء عند أي فشل."""
    chain = _read_archive_chain(gist_files)
    out = []
    for gid in chain:
        files = gist_files if gid == GIST_ID else _gist_get_all_files_for(gid)
        for fn in sorted(f for f in files if f.startswith(ARCHIVE_PREFIX)):
            out.extend(_gist_read_json_list(files[fn], fn))
    return out


def _dedupe_trades(trades):
    seen, merged = set(), []
    for t in trades:
        k = (t.get("type", "official"), t.get("symbol"), t.get("opened_at"))
        if k in seen:
            continue
        seen.add(k)
        merged.append(t)
    return merged


def load_closed_full(gist_files):
    """يحمّل الأرشيف الكامل للصفقات المغلقة (السجل النشط + كل ملفات الأرشيف عبر سلسلة
    الـGists) لبناء النموذج التكيفي. للقراءة فقط ولا يكتب شيئًا. عند أي فشل نرجع للسجل
    النشط فقط حتى لا يتعطل البوت. تُزال التكرارات بمفتاح (type, symbol, opened_at)."""
    active = load_closed(gist_files)
    try:
        all_trades = _load_archive_trades(gist_files)
        all_trades.extend(active)
        merged = _dedupe_trades(all_trades)
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
    state_obj = {"alerted": sorted(alerted_symbols), "btc_dominance_prev": btc_dominance}
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
        full_for_stats = list(history)  # السجل النشط الكامل قبل أي اقتطاع

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

        # الإحصائيات تُحسب من الأرشيف الكامل (كل ملفات الأرشيف + السجل النشط). لو تعذّرت قراءة
        # الأرشيف لا نكتب stats.json (كي لا تُستبدل إحصائيات كاملة بأخرى ناقصة).
        stats = None
        try:
            stats_trades = _dedupe_trades(_load_archive_trades(gist_files) + full_for_stats)
            stats = compute_stats(stats_trades)
        except Exception as e:
            print(f"⚠️ تعذّر تحميل الأرشيف الكامل للإحصائيات — لم يُحدَّث stats.json هذه الدورة: {e}")
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
            **_regime_trade_fields(),
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
            **_regime_trade_fields(),
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
            **_regime_trade_fields(),
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
            **_regime_trade_fields(),
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
            "experimental_context": r.get("experimental_context"),
            "experimental_engine": EXPERIMENTAL_ENGINE_VERSION,
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
        # صفقة أُغلقت سابقًا لكن لم يُحفظ إغلاقها بالسجل (تعذّرت قراءة closed_trades.json بتلك الدورة):
        # تُرحَّل مباشرة للسجل بدون إعادة تقييم ولا إعادة إرسال رسالة النتيجة.
        if pos.get("closed_reason") and pos.get("exit_price") is not None and pos.get("closed_at"):
            closed_now.append(pos)
            continue

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


def compute_market_regime(closes, highs, lows, details=None):
    """
    يحدد نظام السوق العام استنادًا لسلسلة شموع (BTCUSDT عادة):
    1) قوة الاتجاه: ADX(14) مع Hysteresis (دخول ranging تحت REGIME_ADX_ENTER_RANGING،
       وخروج منه فوق REGIME_ADX_EXIT_RANGING) تُحاكى على كامل التاريخ فتبقى النتيجة ثابتة
       بين التشغيلات بدون تخزين حالة.
    2) عند وجود اتجاه: ميل EMA50 (حالي مقابل قبل MARKET_REGIME_SLOPE_LOOKBACK شمعة) كنسبة مئوية؛
       إن كان أقل من REGIME_MIN_SLOPE_PCT يُعتبر ranging.
    3) تأكيد الجهة: يلزم REGIME_MIN_CONFIRMS من (السعر فوق/تحت EMA50) و(+DI أعلى/أدنى من -DI)،
       وإلا يُعتبر ranging (أي strict بدل allow/block).
    يُرجع: "trending_up" / "trending_down" / "ranging" / None (بيانات غير كافية)
    details: قاموس اختياري يُملأ بالقيم المستخدمة (للّوق فقط).
    """
    min_len = MARKET_REGIME_EMA_PERIOD + MARKET_REGIME_SLOPE_LOOKBACK + 1
    if len(closes) < min_len:
        return None

    adx_vals, pdi_vals, mdi_vals = compute_adx_di(highs, lows, closes)
    adx_series = [a for a in adx_vals if a is not None]
    if not adx_series:
        return None
    adx_last = adx_series[-1]

    # محاكاة Hysteresis على التاريخ: نبدأ من حالة مبنية على أول قراءة (عتبة وسطية)
    mid = (REGIME_ADX_ENTER_RANGING + REGIME_ADX_EXIT_RANGING) / 2
    is_ranging = adx_series[0] < mid
    for a in adx_series[1:]:
        if is_ranging and a > REGIME_ADX_EXIT_RANGING:
            is_ranging = False
        elif not is_ranging and a < REGIME_ADX_ENTER_RANGING:
            is_ranging = True

    ema50 = ema(closes, MARKET_REGIME_EMA_PERIOD)
    prev_ema = ema50[-1 - MARKET_REGIME_SLOPE_LOOKBACK]
    slope_pct = ((ema50[-1] / prev_ema) - 1) * 100 if prev_ema else 0.0

    if details is not None:
        details.update({"adx": round(adx_last, 2), "slope_pct": round(slope_pct, 3)})

    if is_ranging:
        if details is not None:
            details["reason"] = "ADX (hysteresis) ranging"
        return "ranging"
    if abs(slope_pct) < REGIME_MIN_SLOPE_PCT:
        if details is not None:
            details["reason"] = "slope below min"
        return "ranging"

    up = slope_pct > 0
    confirms = 0
    if (closes[-1] > ema50[-1]) == up:
        confirms += 1
    pdi, mdi = pdi_vals[-1], mdi_vals[-1]
    if pdi is not None and mdi is not None and (pdi > mdi) == up:
        confirms += 1
    if details is not None:
        details["confirms"] = confirms
    if confirms < REGIME_MIN_CONFIRMS:
        if details is not None:
            details["reason"] = "direction not confirmed"
        return "ranging"
    return "trending_up" if up else "trending_down"


# ---------------- نظام السوق الاحترافي: المكوّنات ----------------

_REGIME_CTX = {}   # آخر سياق احترافي محسوب (يُملأ داخل fetch_market_regime)


def _vote_from_label(label):
    return {"trending_up": 1.0, "trending_down": -1.0, "ranging": 0.0}.get(label)


def _pivots(values, span, kind):
    """قمم/قيعان مؤكدة (قيمة أعلى/أدنى من span شمعة على الجانبين). يرجع [(index, value), ...]."""
    out = []
    n = len(values)
    for i in range(span, n - span):
        w = values[i - span:i + span + 1]
        hit = values[i] == (max(w) if kind == "high" else min(w))
        if not hit:
            continue
        if out and i - out[-1][0] <= span:
            continue  # هضبة (قيم متساوية متجاورة) = نفس القمة/القاع
        out.append((i, values[i]))
    return out


def market_structure_vote(highs, lows, closes, span=None, lookback=None):
    """
    هيكل السوق: آخر قمتين وآخر قاعين مؤكدين.
      HH+HL = +1 | LH+LL = -1 | مختلط = 0
    كسر هيكلي: إغلاق فوق آخر قمة = +0.5 على الأقل، وتحت آخر قاع = -0.5 على الأكثر.
    يُرجع (vote | None, تفاصيل).
    """
    span = span or REGIME_PIVOT_SPAN
    lookback = lookback or REGIME_STRUCTURE_LOOKBACK
    start = max(0, len(closes) - lookback)
    ph = _pivots(highs[start:], span, "high")
    pl = _pivots(lows[start:], span, "low")
    if len(ph) < 2 or len(pl) < 2:
        return None, {}
    hh = 1 if ph[-1][1] > ph[-2][1] else -1
    hl = 1 if pl[-1][1] > pl[-2][1] else -1
    vote = (hh + hl) / 2
    last = closes[-1]
    bos = None
    if last > ph[-1][1]:
        vote, bos = max(vote, 0.5), "up"
    elif last < pl[-1][1]:
        vote, bos = min(vote, -0.5), "down"
    return vote, {"hh": hh > 0, "hl": hl > 0, "bos": bos}


def _atr_pct_series(highs, lows, closes, period=14):
    """سلسلة ATR% (Wilder) لكل شمعة من الفهرس period فصاعدًا."""
    n = len(closes)
    if n < period + 2:
        return []
    tr = [0.0] * n
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    atr = sum(tr[1:period + 1]) / period
    out = [atr / closes[period] * 100]
    for i in range(period + 1, n):
        atr = (atr * (period - 1) + tr[i]) / period
        out.append(atr / closes[i] * 100)
    return out


def volatility_regime(highs, lows, closes):
    """حالة التقلب = موقع ATR% الحالي ضمن تاريخه (percentile): low / normal / high / extreme."""
    series = _atr_pct_series(highs, lows, closes)
    if len(series) < 100:
        return None
    cur = series[-1]
    pct = 100.0 * sum(1 for v in series if v <= cur) / len(series)
    if pct >= REGIME_VOL_EXTREME_PCT:
        state = "extreme"
    elif pct >= REGIME_VOL_HIGH_PCT:
        state = "high"
    elif pct <= REGIME_VOL_LOW_PCT:
        state = "low"
    else:
        state = "normal"
    return {"state": state, "pct": round(pct, 1), "atr_pct": round(cur, 3)}


def market_breadth(tickers, results):
    """
    اتساع السوق (مهم لبوت يتداول العملات البديلة، لأن BTC وحده قد يضلّل):
      advancers   = نسبة العملات السائلة الصاعدة خلال 24س (من ticker، بلا طلبات إضافية)
      above_ema50 = نسبة العملات المفحوصة التي إغلاقها فوق EMA50 على إطار السكانر
    يُرجع (vote في [-1..+1] | None, تفاصيل).
    """
    parts = {}
    if tickers:
        adv = dec = 0
        for t in tickers:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT") or sym.endswith(EXCLUDE_SUFFIX) or sym in EXCLUDE_SYMS:
                continue
            try:
                if float(t["quoteVolume"]) < LIQUIDITY_FLOOR:
                    continue
                ch = float(t["priceChangePercent"])
            except (KeyError, TypeError, ValueError):
                continue
            if ch > 0:
                adv += 1
            elif ch < 0:
                dec += 1
        if adv + dec >= REGIME_BREADTH_MIN_TICKERS:
            parts["advancers"] = adv / (adv + dec)
    if results:
        flags = [r["above_ema50"] for r in results if r.get("above_ema50") is not None]
        if len(flags) >= REGIME_BREADTH_MIN_RESULTS:
            parts["above_ema50"] = sum(1 for f in flags if f) / len(flags)
    if not parts:
        return None, {}
    votes = [max(-1.0, min(1.0, (v - 0.5) / REGIME_BREADTH_BAND)) for v in parts.values()]
    return sum(votes) / len(votes), {k: round(v, 3) for k, v in parts.items()}


def combine_regime_votes(votes):
    """
    يجمع الأصوات الموزونة في درجة [-1..+1] ثم يحوّلها لتسمية.
    المكوّن الغائب (None) يُستبعد من البسط والمقام معًا (لا يُعامل كصفر).
    يُرجع (score, label, note) حيث note = None | "htf_veto" | "fast_derisk"، أو (None, None, None) لو لا توجد أصوات.
    """
    weights = {"primary": REGIME_W_PRIMARY, "htf": REGIME_W_HTF, "htf2": REGIME_W_HTF2,
               "structure": REGIME_W_STRUCTURE, "breadth": REGIME_W_BREADTH}
    num = den = 0.0
    for k, w in weights.items():
        v = votes.get(k)
        if v is None or w <= 0:
            continue
        num += w * v
        den += w
    if den == 0:
        return None, None, None
    score = num / den
    if score >= REGIME_UP_SCORE:
        label = "trending_up"
    elif score <= REGIME_DOWN_SCORE:
        label = "trending_down"
    else:
        label = "ranging"
    note = None
    htf = votes.get("htf")
    if REGIME_HTF_VETO and htf is not None:
        if (label == "trending_up" and htf < 0) or (label == "trending_down" and htf > 0):
            label, note = "ranging", "htf_veto"
    if REGIME_FAST_DERISK and label == "ranging":
        p, st = votes.get("primary"), votes.get("structure")
        if p is not None and p < 0 and (st is None or st <= 0) and (htf is None or htf <= 0):
            label, note = "trending_down", "fast_derisk"
    return score, label, note


def regime_phase(label, votes):
    """وصف مقروء للمرحلة (للّوق والتحليل اللاحق فقط، لا يغيّر السياسة)."""
    if label != "ranging":
        return label
    p, h, s = votes.get("primary"), votes.get("htf"), votes.get("structure")
    if h is not None and h < 0 and ((s is not None and s > 0) or (p is not None and p >= 0)):
        return "early_recovery"
    if h is not None and h > 0 and p is not None and p <= 0:
        return "pullback_in_uptrend"
    return "range"


def _effective_regime_mode(market_regime, signal_type):
    """سياسة REGIME_POLICY بعد تشديد التقلب: عند extreme يصير allow->strict و strict->block."""
    mode = (REGIME_POLICY.get(market_regime) or {}).get(signal_type, "allow")
    if REGIME_VOL_ESCALATE and _REGIME_CTX.get("vol_state") == "extreme":
        mode = {"allow": "strict", "strict": "block"}.get(mode, mode)
    return mode


def _regime_trade_fields():
    """حقول تشخيصية صغيرة تُسجَّل مع كل صفقة جديدة (لتحليل الأداء حسب حالة السوق لاحقًا)."""
    if not _REGIME_CTX:
        return {}
    return {
        "regime_score": _REGIME_CTX.get("score"),
        "regime_phase": _REGIME_CTX.get("phase"),
        "regime_vol": _REGIME_CTX.get("vol_state"),
        "regime_risk_mult": _REGIME_CTX.get("risk_mult"),
    }


def _btc_series(interval, limit):
    kl = drop_unclosed_candle(fetch_klines("BTCUSDT", interval, limit))
    return ([float(k[4]) for k in kl], [float(k[2]) for k in kl], [float(k[3]) for k in kl])


def fetch_market_regime(tickers=None, results=None):
    """
    يحسب market_regime الحالي مرة واحدة لكل دورة مسح (مقياس عام للسوق، وليس خاص بعملة).
    REGIME_MODEL="legacy": المنطق القديم (BTC على إطار السكانر فقط).
    REGIME_MODEL="pro": تصويت موزون (primary + HTF + HTF2 + structure + breadth) مع Veto للفريم الأعلى،
    وحالة تقلب تضبط risk_mult وتشدّد السياسة عند extreme. يرجع نفس التسميات الثلاث أو None.
    """
    global _REGIME_CTX
    _REGIME_CTX = {}

    if REGIME_MODEL != "pro":
        closes, highs, lows = _btc_series(INTERVAL, SCAN_LIMIT)
        info = {}
        regime = compute_market_regime(closes, highs, lows, info)
        if info:
            print("Regime details:", info)
        return regime

    closes, highs, lows = _btc_series(INTERVAL, REGIME_FETCH_LIMIT)
    info = {}
    primary_label = compute_market_regime(closes, highs, lows, info)
    if primary_label is None:
        return None
    labels = {"primary": primary_label}
    votes = {"primary": _vote_from_label(primary_label)}

    htf1 = HTF_MAP.get(INTERVAL)
    htf2 = HTF_MAP.get(htf1) if htf1 else None
    for key, tf in (("htf", htf1), ("htf2", htf2)):
        if not tf:
            continue
        try:
            c, h, l = _btc_series(tf, SCAN_LIMIT)
            lab = compute_market_regime(c, h, l)
            labels[key] = lab
            votes[key] = _vote_from_label(lab)
        except Exception as e:
            print(f"⚠️ regime: تعذّر حساب إطار {tf} — يُستبعد من التصويت: {e}")

    s_vote, s_info = market_structure_vote(highs, lows, closes)
    votes["structure"] = s_vote
    b_vote, b_info = market_breadth(tickers, results)
    votes["breadth"] = b_vote
    vol = volatility_regime(highs, lows, closes)

    score, label, note = combine_regime_votes(votes)
    if label is None:
        return None
    phase = regime_phase(label, votes)
    vol_state = vol["state"] if vol else None
    risk_mult = REGIME_RISK_MULT.get(vol_state, 1.0)

    _REGIME_CTX = {
        "label": label, "score": round(score, 3), "phase": phase, "note": note,
        "votes": votes, "labels": labels, "structure": s_info, "breadth": b_info,
        "vol": vol, "vol_state": vol_state, "risk_mult": risk_mult,
    }
    fmt = lambda v: "n/a" if v is None else f"{v:+.1f}"
    vol_txt = f" (p{vol['pct']:.0f}, ATR% {vol['atr_pct']})" if vol else ""
    print(
        f"🧭 Regime(pro)={label} [{phase}] score={score:+.2f}"
        f"{f' ({note})' if note else ''} | primary={fmt(votes.get('primary'))} "
        f"htf={fmt(votes.get('htf'))} htf2={fmt(votes.get('htf2'))} "
        f"structure={fmt(votes.get('structure'))} breadth={fmt(votes.get('breadth'))} {b_info} | "
        f"vol={vol_state}{vol_txt} risk×{risk_mult}"
    )
    if info:
        print("Regime details (primary):", info)
    return label


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

    results = run_scan(tickers)

    # regime يُحسب بعد المسح (يحتاج نتائجه لقياس breadth) وقبل اختيار الإشارات،
    # فيبقى جزءًا من قرار الترتيب والتسجيل. أي فشل = None = السماح (fail-open) كما كان.
    market_regime = None
    try:
        market_regime = fetch_market_regime(tickers, results)
        print(f"Market Regime (BTCUSDT {INTERVAL}, model={REGIME_MODEL}): {market_regime}")
    except Exception as e:
        print("تعذّر حساب market_regime:", e)
    results = apply_adaptive_ranking(results, adaptive_model, market_regime)
    log_adaptive_rejections(results)

    # الإشارة الرسمية: نفس فلتر النسخة القديمة بالضبط (الدرجة >= 1.5 مع تأكيد الحجم والتقلب والاستقرار
    # وغير عرضية وبعيدة عن المقاومة وربح TP1 كافٍ). النموذج التكيفي وسياسة regime اختياريان (معطّلان افتراضيًا).
    strong = [
        r for r in results
        if r["score"] >= 1.5 and r["vol_confirm"] and r["atr_pct"] >= 0.08 and r["persistent"]
        and not r["ranging"] and not r["near_resistance"]
        and meets_min_profit(r["entry"], r["tps"])
    ]
    if OFFICIAL_USE_ADAPTIVE:
        strong = [r for r in strong if adaptive_pass(r, "official")]
    if OFFICIAL_USE_REGIME:
        strong = regime_filter(strong, "official", market_regime)
    strong_symbols = {r["symbol"] for r in strong}

    # إشارات مبكرة (انضغاط تقلب / تراكم صامت) لعملات لم تصل بعد لإشارة شراء كاملة —
    # تُميَّز بمفتاح منفصل (":early") في ذاكرة التنبيهات كي لا تتعارض مع إشارات الشراء الرسمية.
    # تُصفّى هنا أيضًا بنفس شرط الحد الأدنى لنسبة الربح (MIN_PROFIT_PCT) قبل اعتبارها مؤهلة
    # أصلاً — وليس فقط عند الإرسال — كي لا تُسجَّل كـ"مُنبَّه عليها" في الذاكرة وتُحرَم من
    # الإرسال لاحقًا إن تحسّن ربحها المتوقع
    # شرط النسخة القديمة: score < 1.5 (هنا الدرجة قبل بونص Squeeze الرسمي، early_gate_score،
    # كي لا يُقصي البونص الإشارات المبكرة) + (squeeze أو accumulation) + ثقة محددة + ربح TP1 كافٍ.
    # شرط "ليست ضمن strong" يمنع تكرار نفس العملة كرسمية ومبكرة معًا.
    early_eligible = [
        r for r in results
        if r.get("early_gate_score", r["score"]) < 1.5
        and r["symbol"] not in strong_symbols
        and (r["squeeze"] or r["accumulation"])
        and r.get("early_confidence") is not None
        and r.get("early_entry") is not None
        and meets_min_profit(r["early_entry"], r["early_tps"])
        and adaptive_pass(r, "early")
    ]
    early_eligible = regime_filter(early_eligible, "early", market_regime)

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

    # إشارات تجريبية (confluence_v1: AVWAP + Volume Profile + CVD + TTM Squeeze + OI/Funding) — مستقلة، تُستبعد العملات
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
    fresh_early = [r for r in early_eligible if _engine_key(r["symbol"], "early") not in prev_alerted]
    fresh_breakout = [r for r in breakout_eligible if _engine_key(r["symbol"], "breakout") not in prev_alerted]
    fresh_experimental = [r for r in experimental_eligible if _engine_key(r["symbol"], "experimental") not in prev_alerted]

    # لا تنبيه ولا صفقة جديدة لإشارة لها صفقة مفتوحة أصلًا بنفس الرمز والنوع
    fresh = [r for r in fresh if not _already_open(open_positions, r["symbol"], "official")]
    fresh_early = [r for r in fresh_early if not _already_open(open_positions, r["symbol"], "early")]
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
          f"إشارات مبكرة جديدة: {len(fresh_early)} | إشارات انفجار جديدة: {len(fresh_breakout)} | "
          f"إشارات تجريبية جديدة: {len(fresh_experimental)}")
    print(f"🧪 قمع التجريبية (عملات اجتازت كل مرحلة): {funnel_summary()}")
    print(f"🧪 OI/Funding: {_DERIV_STATS} | إخفاقات المزوّدين المتتالية: {_DERIV_FAILS}")

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
    open_new_positions(open_positions, fresh, market_regime)
    open_new_early_positions(open_positions, fresh_early, market_regime)
    open_new_breakout_positions(open_positions, fresh_breakout, market_regime)
    open_new_experimental_positions(open_positions, fresh_experimental, market_regime)

    # حفظ موحّد: ذاكرة الإشارات (رسمية + مبكرة + انفجار + تجريبية) + BTC Dominance + الصفقات المفتوحة + أرشيف الصفقات المغلقة حديثًا
    # + إحصائيات أداء محسوبة من السجل المحدَّث (خيار 3: تتبع فقط، بدون تعديل تلقائي على منطق البوت)
    save_all_state(
        build_alerted_keys(strong, early_eligible, breakout_eligible, experimental_eligible),
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
