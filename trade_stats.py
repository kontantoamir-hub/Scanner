"""
trade_stats.py — تقرير مستقل لإحصائيات الصفقات المغلقة

يقرأ closed_trades.json من نفس الـ Gist المستخدم في scanner.py، ويحسب:
- عدد الصفقات المنجزة: رابحة / خاسرة (تصنيف "محايدة" أُزيل بالكامل من كل التحليل لأن
  إشارة الإغلاق المحايد ⚪ نفسها أُزيلت أصلاً من البوت — لا تُحسب أي صفقة محايدة ضمن
  أي إحصائية أدناه: لا بالملخص العام، ولا حسب النوع، ولا حسب المؤشر، ولا حسب "بدون مؤشر")
- نسبة الربح ونسبة الخسارة لكل فئة
- تصنيف حسب نوع الإشارة (رسمية / مبكرة / انفجار / تجريبية)، مع مجموع نسب الربح الإجمالية
  لكل الصفقات الرابحة ومجموع نسب الخسارة الإجمالية لكل الصفقات الخاسرة (رسمية/مبكرة/تجريبية فقط)
- بعد الملخص العام لكل نوع، يُعرض تفصيل كل نوع إشارة بقسم منفصل تمامًا (رسمية، ثم مبكرة، ثم
  انفجار، ثم تجريبية)، مفصول بخط طويل بين كل نوع والذي يليه — بحيث لا تختلط مؤشرات/عوامل نوع
  بآخر أبدًا
- لكل صفقة رسمية أو مبكرة: نوع المؤشر (المؤشرات) التي كانت حاضرة وقت الدخول والنتيجة
- لكل صفقة انفجار: عوامل جودة الاختراق (دعم الاتجاه / MACD / RSI) والنتيجة
  (مؤشرات Squeeze/Accumulation/Divergence لا تُحسب لصفقات الانفجار لأنها غير محسوبة أصلاً
  عند فتح هذا النوع من الصفقات في scanner.py — استخدام عوامل الانفجار الخاصة بدل ذلك
  يمنع تلوّث فئة "بدون مؤشر إضافي" بصفقات الانفجار التي لا علاقة لها بها)
- لكل صفقة تجريبية: عوامل جودة الإشارة التجريبية (دعم الاتجاه EMA7/14 / تأكيد حجم-OBV /
  تدفق أموال MFI صاعد) والنتيجة (نفس منطق عزل الانفجار أعلاه: لا علاقة لهذه الصفقات
  بمؤشرات Squeeze/Accumulation/Divergence، فتُعامل بقسمها الخاص تمامًا)
- نسبة نجاح كل مؤشر على حدة (squeeze / accumulation / divergence / momentum / extended) عبر
  الصفقات الرسمية/المبكرة
- لصفقات "بدون مؤشر إضافي" تحديدًا: تفصيل حسب المؤشرات الأساسية الثمانية التي تصنع الدرجة
  (rsi_state / macd_bull / bb_state / vol_confirm / ranging / near_resistance / obv_confirm /
  htf_aligned) — متاح فقط للصفقات المفتوحة بعد إضافة هذه الحقول لسجل الصفقة في scanner.py؛
  الصفقات الأقدم لا تحتوي هذه الحقول وتُستثنى تلقائيًا من هذا القسم فقط دون التأثير على بقية التقرير
- قسم مخصص للمبكرة فقط (مبني على حقل "factors" المحفوظ مع كل صفقة مبكرة، وهو التركيبة
  الفعلية الدقيقة التي أطلقت الإشارة): نسبة نجاح/فشل كل مؤشر من الأربعة لوحده (مساهمة
  حاضرة بصرف النظر عن باقي المؤشرات)، ونسبة نجاح/فشل حسب عدد المؤشرات المتعاونة معًا
  (1/2/3/4) — الصفقات المبكرة الأقدم من إضافة حقل factors تُستثنى تلقائيًا من هذا القسم فقط

لا يُعدّل أي شيء في منطق البوت أو ملفاته — قراءة وعرض فقط (يمكن تشغيله يدويًا
عبر workflow_dispatch أو محليًا بدون أي تأثير على عمل scanner.py).

المتغيرات المطلوبة (نفس Secrets المستخدمة في scanner.py):
  GIST_TOKEN, GIST_ID
اختياري لإرسال التقرير عبر تيليجرام بدل الطباعة فقط:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""

import os
import json
import datetime as dt
import requests

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")
CLOSED_GIST_FILE = "closed_trades.json"     # السجل النشط: أحدث الصفقات فقط
ARCHIVE_PREFIX = "closed_trades_archive_"   # ملفات الأرشيف المرقّمة (تحوي كل التاريخ الأقدم)
OPEN_POSITIONS_GIST_FILE = "open_positions.json"  # الصفقات المفتوحة قيد المتابعة حاليًا (نفس ملف scanner.py)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# مؤشرات الصفقات الرسمية/المبكرة (تُحسب فقط لهذين النوعين في scanner.py)
INDICATOR_KEYS = ["squeeze", "accumulation", "divergence", "momentum", "extended"]
INDICATOR_LABELS = {
    "squeeze": "انضغاط تقلب (Squeeze)",
    "accumulation": "تراكم صامت (Accumulation)",
    "divergence": "دايفرجنس (Divergence)",
    "momentum": "زخم (Momentum)",
    "extended": "امتداد زائد (Overextension)",
}

# عوامل جودة صفقات الانفجار (breakout_details في scanner.py)
BREAKOUT_FACTOR_KEYS = ["trend_support", "macd_bull", "rsi_ok"]
BREAKOUT_FACTOR_LABELS = {
    "trend_support": "دعم اتجاه EMA7/14",
    "macd_bull": "MACD إيجابي",
    "rsi_ok": "RSI في نطاق صحي",
}

# عوامل جودة الإشارة التجريبية (experimental_details في scanner.py) — Ichimoku
# Tenkan/Kijun هو المحفّز نفسه (كل صفقة تجريبية تملكه بالتعريف فلا داعي لتتبعه كعامل)،
# والعوامل الثلاثة التالية هي فقط ما يرفع الدرجة (0-3) بعد تحقق المحفّز
EXPERIMENTAL_FACTOR_KEYS = ["trend_support", "volume_confirm", "mfi_bullish"]
EXPERIMENTAL_FACTOR_LABELS = {
    "trend_support": "دعم اتجاه EMA7/14",
    "volume_confirm": "تأكيد حجم + OBV",
    "mfi_bullish": "تدفق أموال صاعد (MFI)",
}

# المؤشرات الأربعة المستقلة اللي تطلق الإشارة المبكرة (تطابق الأربعة بـscanner.py منذ
# تحديث 2026-09-02) — مبنية على حقل "factors" المحفوظ مع كل صفقة مبكرة (التركيبة الفعلية
# الدقيقة اللي أطلقت الإشارة)، بدل الاعتماد على الأعلام الخام squeeze/accumulation/divergence
# اللي تُحسب لكل صفقة (رسمية أو مبكرة) بصرف النظر هل ساهمت بإطلاق الإشارة المبكرة أصلاً أو لا
EARLY_FACTOR_KEYS = ["accumulation", "divergence", "momentum", "squeeze"]
EARLY_FACTOR_LABELS = {
    "accumulation": "تراكم صامت (Accumulation)",
    "divergence": "دايفرجنس (Divergence)",
    "momentum": "زخم (Momentum)",
    "squeeze": "انضغاط تقلب (Squeeze)",
}
EARLY_COMBO_LABELS = {1: "مؤشر واحد", 2: "مؤشرين", 3: "3 مؤشرات", 4: "4 مؤشرات"}

# المؤشرات الأساسية اللي تصنع الدرجة (score) — تُحفظ فقط في الصفقات المفتوحة بعد تحديث
# schema الحفظ في scanner.py؛ تُستخدم لتفصيل صفقات "بدون مؤشر إضافي" تحديدًا (see below)
BASE_INDICATOR_KEYS = [
    "rsi_state", "macd_bull", "bb_state", "vol_confirm",
    "ranging", "near_resistance", "obv_confirm", "htf_aligned",
]
BASE_INDICATOR_LABELS = {
    "rsi_state": "RSI",
    "macd_bull": "MACD إيجابي",
    "bb_state": "بولينجر",
    "vol_confirm": "تأكيد الحجم",
    "ranging": "سوق عرضي (ADX منخفض)",
    "near_resistance": "قرب مقاومة",
    "obv_confirm": "تأكيد OBV",
    "htf_aligned": "توافق فريم أعلى",
}

TYPE_LABELS = {
    "official": "رسمية",
    "early": "مبكرة",
    "breakout": "انفجار",
    "experimental": "تجريبية",
}

# ترتيب عرض الأنواع بكل أقسام التقرير (الملخص العلوي + التفصيل حسب النوع)
TYPE_ORDER = ["official", "early", "breakout", "experimental"]

# الأنواع التي تُحسب لها الأعلام التشخيصية الخام (squeeze/accumulation/.../extended)
# وتفصيل "بدون مؤشر إضافي" — الانفجار والتجريبية لهما عوامل جودة خاصة بهما بدل هذا
TYPES_WITH_RAW_INDICATORS = ("official", "early")

# فاصل بصري طويل بين تفصيل كل نوع إشارة وآخر بالتقرير
SECTION_DIVIDER = "_" * 33


def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def _read_json_file(file_entry, filename):
    """يقرأ محتوى ملف من استجابة Gist، مع التحقق من احتمال البتر (truncated) لملف كبير جدًا
    والجلب من raw_url في هذه الحالة بدل الاعتماد على content فقط."""
    if file_entry.get("truncated"):
        raw_url = file_entry.get("raw_url")
        try:
            rr = requests.get(raw_url, timeout=15)
            rr.raise_for_status()
            content = rr.text
        except Exception as e:
            print(f"⚠️ تعذّر جلب المحتوى الكامل غير المبتور لـ {filename}: {e}")
            content = file_entry.get("content", "[]")
    else:
        content = file_entry.get("content", "[]")
    try:
        return json.loads(content)
    except Exception as e:
        print(f"⚠️ تعذّر تحليل {filename}: {e}")
        return []


def load_closed_trades():
    """يجمع السجل النشط (closed_trades.json) مع كل ملفات الأرشيف المرقّمة، ليعطي
    التاريخ الكامل للصفقات المغلقة بلا أي سقف على العدد الإجمالي."""
    if not GIST_TOKEN or not GIST_ID:
        raise SystemExit("❌ GIST_TOKEN أو GIST_ID غير موجودين في متغيرات البيئة.")
    r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=_gist_headers(), timeout=15)
    r.raise_for_status()
    files = r.json().get("files", {})

    all_trades = []

    # ملفات الأرشيف أولًا (الأقدم زمنيًا)، مرتبة حسب رقمها
    archive_names = sorted(fn for fn in files if fn.startswith(ARCHIVE_PREFIX))
    for name in archive_names:
        all_trades.extend(_read_json_file(files[name], name))

    # ثم السجل النشط (الأحدث)
    if CLOSED_GIST_FILE in files:
        all_trades.extend(_read_json_file(files[CLOSED_GIST_FILE], CLOSED_GIST_FILE))

    return all_trades


def load_open_positions():
    """يقرأ الصفقات المفتوحة حاليًا (قيد المتابعة) من نفس الـ Gist — تُستخدم فقط لعرض
    عددها ضمن ملخص التقرير، بدون أي تأثير على حساب أي إحصائية أخرى (المبنية بالكامل
    على السجل المغلق فقط)."""
    if not GIST_TOKEN or not GIST_ID:
        return []
    try:
        r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=_gist_headers(), timeout=15)
        r.raise_for_status()
        files = r.json().get("files", {})
        if OPEN_POSITIONS_GIST_FILE not in files:
            return []
        return _read_json_file(files[OPEN_POSITIONS_GIST_FILE], OPEN_POSITIONS_GIST_FILE)
    except Exception as e:
        print(f"⚠️ تعذّر جلب الصفقات المفتوحة حاليًا: {e}")
        return []


def classify(trade):
    """يحدد نتيجة الصفقة: win / loss / neutral، بناءً على سبب الإغلاق وعدد الأهداف المتحققة.
    ملاحظة: نتيجة "neutral" ما زالت تُحسب هنا للحفاظ على البيانات القديمة قابلة للقراءة،
    لكن build_report يتجاهلها بالكامل في كل الإحصائيات لأن إشارة الإغلاق المحايد أُزيلت من البوت."""
    reason = trade.get("closed_reason", "UNKNOWN")
    hit = len(trade.get("hit_tps") or [])
    if reason == "ALL_TP" or hit > 0:
        return "win"
    if reason == "SL" and hit == 0:
        return "loss"
    return "neutral"  # INVALIDATED أو EXPIRED بدون أي هدف محقق


def pnl_pct(trade):
    entry, exit_price = trade.get("entry"), trade.get("exit_price")
    if entry and exit_price:
        return (exit_price - entry) / entry * 100
    return None


def duration_hours(trade):
    try:
        t0 = dt.datetime.strptime(trade["opened_at"], "%Y-%m-%d %H:%M:%S")
        t1 = dt.datetime.strptime(trade["closed_at"], "%Y-%m-%d %H:%M:%S")
        return (t1 - t0).total_seconds() / 3600
    except Exception:
        return None


def active_indicators(trade):
    """يرجع قائمة أسماء المؤشرات/العوامل التي كانت True وقت فتح هذه الصفقة (رسمية/مبكرة فقط)."""
    return [k for k in INDICATOR_KEYS if trade.get(k) is True]


def active_breakout_factors(trade):
    """يرجع قائمة عوامل جودة الاختراق التي كانت True وقت فتح صفقة انفجار."""
    details = trade.get("breakout_details") or {}
    return [k for k in BREAKOUT_FACTOR_KEYS if details.get(k) is True]


def active_experimental_factors(trade):
    """يرجع قائمة عوامل جودة الإشارة التجريبية التي كانت True وقت فتح صفقة تجريبية."""
    details = trade.get("experimental_details") or {}
    return [k for k in EXPERIMENTAL_FACTOR_KEYS if details.get(k) is True]


def base_indicator_state_label(key, value):
    """يحوّل قيمة مؤشر أساسي إلى وصف عربي قابل للعرض حسب نوع الحقل."""
    if key == "rsi_state":
        return {1: "تشبع بيعي (RSI<35)", -1: "تشبع شرائي (RSI>65)", 0: "محايد"}.get(value, "غير معروف")
    if key == "bb_state":
        return {1: "عند الحد السفلي", -1: "عند الحد العلوي", 0: "منتصف النطاق"}.get(value, "غير معروف")
    # باقي الحقول منطقية (True/False)، وhtf_aligned ممكن تكون None لو لم تُفحص
    if value is True:
        return "حاضر"
    if value is False:
        return "غائب"
    return "لم يُفحص"


def build_report(trades, open_count=None):
    if not trades:
        msg = "لا توجد صفقات مغلقة بعد في السجل."
        if open_count is not None:
            msg += f"\n🔄 مفتوحة حاليًا: {open_count} صفقة"
        return msg, []

    total = len(trades)
    wins = [t for t in trades if classify(t) == "win"]
    losses = [t for t in trades if classify(t) == "loss"]

    win_rate = len(wins) / total * 100
    loss_rate = len(losses) / total * 100

    # --- تصنيف حسب النوع (رسمية / مبكرة / انفجار / تجريبية) ---
    # صفقات "neutral" (⚪) تُستبعد بالكامل من هذا التصنيف وكل ما يليه، لأن هذه الإشارة أُزيلت
    # أصلاً من البوت. لكل نوع نجمع أيضًا مجموع نسب الربح لكل الصفقات الرابحة ومجموع نسب
    # الخسارة لكل الصفقات الخاسرة (win_pnl_sum / loss_pnl_sum).
    type_stats = {}
    for t in trades:
        outcome = classify(t)
        if outcome == "neutral":
            continue
        ttype = t.get("type", "official")
        s = type_stats.setdefault(
            ttype, {"total": 0, "win": 0, "loss": 0, "win_pnl_sum": 0.0, "loss_pnl_sum": 0.0}
        )
        s["total"] += 1
        s[outcome] += 1
        pnl = pnl_pct(t)
        if pnl is not None:
            if outcome == "win":
                s["win_pnl_sum"] += pnl
            else:
                s["loss_pnl_sum"] += pnl

    # --- نجاح كل مؤشر على حدة (أعلام تشخيصية خام) — منفصلة لكل نوع (رسمية لوحدها، مبكرة لوحدها)
    # كي لا تختلط تفاصيل نوع بآخر عند عرض القسم الخاص بكل نوع
    indicator_stats_by_type = {
        tt: {k: {"total": 0, "win": 0, "loss": 0} for k in INDICATOR_KEYS} for tt in TYPES_WITH_RAW_INDICATORS
    }
    no_indicator_stats_by_type = {tt: {"total": 0, "win": 0, "loss": 0} for tt in TYPES_WITH_RAW_INDICATORS}

    # --- نجاح كل عامل جودة انفجار على حدة ---
    breakout_factor_stats = {k: {"total": 0, "win": 0, "loss": 0} for k in BREAKOUT_FACTOR_KEYS}

    # --- نجاح كل عامل جودة تجريبية على حدة ---
    experimental_factor_stats = {k: {"total": 0, "win": 0, "loss": 0} for k in EXPERIMENTAL_FACTOR_KEYS}

    # --- تفصيل صفقات "بدون مؤشر إضافي" حسب المؤشرات الأساسية (state -> stats) — منفصلة لكل نوع أيضًا
    base_indicator_stats_by_type = {
        tt: {k: {} for k in BASE_INDICATOR_KEYS} for tt in TYPES_WITH_RAW_INDICATORS
    }
    # صفقات "بدون مؤشر إضافي" لكن أقدم من تحديث الحفظ (لا تحوي الحقول الثمانية) — لكل نوع
    no_indicator_no_basedata_by_type = {tt: 0 for tt in TYPES_WITH_RAW_INDICATORS}

    # --- قسم مخصص للمبكرة فقط: مبني على حقل "factors" (التركيبة الفعلية اللي أطلقت الإشارة) ---
    # (أ) نجاح كل مؤشر من الأربعة لما يكون هو الوحيد الحاضر فعليًا (بدون أي مؤشر ثانٍ معه —
    #     يطابق مستوى ثقة "احتمالية" بالضبط)، وليس مجرد ظهوره ضمن أي تركيبة
    early_factor_stats = {k: {"total": 0, "win": 0, "loss": 0} for k in EARLY_FACTOR_KEYS}
    # (ب) نجاح حسب عدد المؤشرات المتعاونة معًا (1/2/3/4)
    early_combo_stats = {n: {"total": 0, "win": 0, "loss": 0} for n in (1, 2, 3, 4)}
    early_no_factor_data = 0  # صفقات مبكرة أقدم من إضافة حقل factors -> تُستثنى من هذا القسم فقط

    per_trade_lines = []
    for t in trades:
        outcome = classify(t)
        if outcome == "neutral":
            continue  # مُستبعدة بالكامل من التحليل والتفصيل، لأن هذه الإشارة أُزيلت من البوت

        ttype = t.get("type", "official")
        pnl = pnl_pct(t)
        dur = duration_hours(t)

        if ttype == "breakout":
            factors = active_breakout_factors(t)
            for k in factors:
                breakout_factor_stats[k]["total"] += 1
                breakout_factor_stats[k][outcome] += 1
            inds_ar = "، ".join(BREAKOUT_FACTOR_LABELS[k] for k in factors) if factors else "بدون عوامل مسجّلة"
        elif ttype == "experimental":
            factors = active_experimental_factors(t)
            for k in factors:
                experimental_factor_stats[k]["total"] += 1
                experimental_factor_stats[k][outcome] += 1
            inds_ar = "، ".join(EXPERIMENTAL_FACTOR_LABELS[k] for k in factors) if factors else "بدون عوامل مسجّلة"
        elif ttype in TYPES_WITH_RAW_INDICATORS:
            inds = active_indicators(t)
            if inds:
                for k in inds:
                    indicator_stats_by_type[ttype][k]["total"] += 1
                    indicator_stats_by_type[ttype][k][outcome] += 1
                inds_ar = "، ".join(INDICATOR_LABELS[k] for k in inds)
            else:
                no_indicator_stats_by_type[ttype]["total"] += 1
                no_indicator_stats_by_type[ttype][outcome] += 1
                inds_ar = "بدون مؤشر إضافي"

                has_base_data = any(k in t for k in BASE_INDICATOR_KEYS)
                if not has_base_data:
                    no_indicator_no_basedata_by_type[ttype] += 1
                else:
                    for k in BASE_INDICATOR_KEYS:
                        if k not in t:
                            continue
                        label = base_indicator_state_label(k, t.get(k))
                        s = base_indicator_stats_by_type[ttype][k].setdefault(
                            label, {"total": 0, "win": 0, "loss": 0}
                        )
                        s["total"] += 1
                        s[outcome] += 1

            if ttype == "early":
                early_factors = t.get("factors")
                if not early_factors:
                    early_no_factor_data += 1
                else:
                    n = len(early_factors)
                    if n == 1 and early_factors[0] in early_factor_stats:
                        k = early_factors[0]
                        early_factor_stats[k]["total"] += 1
                        early_factor_stats[k][outcome] += 1
                    if n in early_combo_stats:
                        early_combo_stats[n]["total"] += 1
                        early_combo_stats[n][outcome] += 1
        else:
            inds_ar = "—"  # نوع غير معروف (لا يُفترض حدوثه) — بدون تفصيل إضافي

        outcome_ar = {"win": "✅ ربح", "loss": "❌ خسارة"}[outcome]
        pnl_txt = f"{pnl:+.2f}%" if pnl is not None else "—"
        dur_txt = f"{dur:.0f}س" if dur is not None else "—"
        per_trade_lines.append(
            f"{t.get('symbol','?')} | نوع: {TYPE_LABELS.get(ttype, ttype)} | score: {t.get('score','?')} | "
            f"{outcome_ar} | عائد: {pnl_txt} | مدة: {dur_txt} | المؤشرات: {inds_ar}"
        )

    # --- بناء نص التقرير المختصر ---
    lines = [
        "📊 تقرير الصفقات المنجزة",
        f"الإجمالي: {total} صفقة",
        f"✅ رابحة: {len(wins)} ({win_rate:.1f}%)",
        f"❌ خاسرة: {len(losses)} ({loss_rate:.1f}%)",
    ]
    if open_count is not None:
        lines.append(f"🔄 مفتوحة حاليًا: {open_count} صفقة")
    lines.append("")
    lines.append("— نسبة النجاح حسب النوع —")
    for ttype in TYPE_ORDER:
        s = type_stats.get(ttype)
        if not s or s["total"] == 0:
            continue
        wr = s["win"] / s["total"] * 100
        line = f"{TYPE_LABELS[ttype]}: {s['total']} صفقة | نجاح {wr:.0f}% (رابحة {s['win']} / خاسرة {s['loss']})"
        if ttype in ("official", "early", "experimental"):
            line += f" | مجموع ربح الرابحة: {s['win_pnl_sum']:+.2f}% | مجموع خسارة الخاسرة: {s['loss_pnl_sum']:+.2f}%"
        lines.append(line)

    # --- تفصيل مستقل لكل نوع إشارة على حدة، مفصول بخط طويل بين كل نوع والذي يليه، بحيث لا
    # تختلط مؤشرات/عوامل نوع بآخر إطلاقًا ---
    types_with_data = [tt for tt in TYPE_ORDER if type_stats.get(tt, {}).get("total", 0) > 0]
    for ttype in types_with_data:
        s = type_stats[ttype]
        block = []
        wr = s["win"] / s["total"] * 100
        header = f"{TYPE_LABELS[ttype]}: {s['total']} صفقة | نجاح {wr:.0f}% (رابحة {s['win']} / خاسرة {s['loss']})"
        if ttype in ("official", "early", "experimental"):
            header += f" | مجموع ربح الرابحة: {s['win_pnl_sum']:+.2f}% | مجموع خسارة الخاسرة: {s['loss_pnl_sum']:+.2f}%"
        block.append(header)

        if ttype in TYPES_WITH_RAW_INDICATORS:
            ind_stats = indicator_stats_by_type[ttype]
            nist = no_indicator_stats_by_type[ttype]
            if any(ind_stats[k]["total"] > 0 for k in INDICATOR_KEYS) or nist["total"] > 0:
                block.append("")
                block.append("— نسبة النجاح حسب المؤشر —")
                for k in INDICATOR_KEYS:
                    st = ind_stats[k]
                    if st["total"] == 0:
                        continue
                    wrk = st["win"] / st["total"] * 100
                    block.append(f"{INDICATOR_LABELS[k]}: {st['total']} صفقة | نجاح {wrk:.0f}% (رابحة {st['win']} / خاسرة {st['loss']})")

                if nist["total"] > 0:
                    wrk = nist["win"] / nist["total"] * 100
                    block.append(f"بدون مؤشر إضافي: {nist['total']} صفقة | نجاح {wrk:.0f}% (رابحة {nist['win']} / خاسرة {nist['loss']})")

                    with_base_data = nist["total"] - no_indicator_no_basedata_by_type[ttype]
                    if with_base_data > 0:
                        block.append("")
                        block.append(
                            f"— تفصيل 'بدون مؤشر إضافي' حسب المؤشرات الأساسية ({with_base_data} صفقة تحوي بيانات) —"
                        )
                        for k in BASE_INDICATOR_KEYS:
                            states = base_indicator_stats_by_type[ttype][k]
                            if not states:
                                continue
                            block.append(f"{BASE_INDICATOR_LABELS[k]}:")
                            for label, st2 in states.items():
                                if st2["total"] == 0:
                                    continue
                                wrk2 = st2["win"] / st2["total"] * 100
                                block.append(
                                    f"  • {label}: {st2['total']} صفقة | نجاح {wrk2:.0f}% (رابحة {st2['win']} / خاسرة {st2['loss']})"
                                )
                        if no_indicator_no_basedata_by_type[ttype] > 0:
                            block.append(
                                f"(ملاحظة: {no_indicator_no_basedata_by_type[ttype]} صفقة من 'بدون مؤشر إضافي' أقدم "
                                f"من تحديث الحفظ التشخيصي ولا تحوي بيانات المؤشرات الأساسية، فاستُبعدت من هذا "
                                f"التفصيل فقط)"
                            )
                    else:
                        block.append(
                            f"(كل صفقات 'بدون مؤشر إضافي' الـ{nist['total']} أقدم من تحديث الحفظ التشخيصي — لا "
                            f"تتوفر بيانات المؤشرات الأساسية بعد، ستظهر تدريجيًا مع الصفقات الجديدة)"
                        )

            if ttype == "early":
                early_total_with_data = sum(st["total"] for st in early_factor_stats.values())
                if early_total_with_data > 0 or early_no_factor_data > 0:
                    block.append("")
                    block.append("— نسبة النجاح لكل مؤشر لوحده (بدون أي مؤشر ثانٍ معه) —")
                    for k in EARLY_FACTOR_KEYS:
                        st = early_factor_stats[k]
                        if st["total"] == 0:
                            continue
                        wrk = st["win"] / st["total"] * 100
                        lrk = st["loss"] / st["total"] * 100
                        block.append(
                            f"{EARLY_FACTOR_LABELS[k]}: {st['total']} صفقة | نجاح {wrk:.0f}% ({st['win']}) | فشل {lrk:.0f}% ({st['loss']})"
                        )

                    block.append("")
                    block.append("— نسبة النجاح حسب عدد المؤشرات المتعاونة معًا —")
                    for n in (1, 2, 3, 4):
                        st = early_combo_stats[n]
                        if st["total"] == 0:
                            continue
                        wrk = st["win"] / st["total"] * 100
                        lrk = st["loss"] / st["total"] * 100
                        block.append(
                            f"{EARLY_COMBO_LABELS[n]}: {st['total']} صفقة | نجاح {wrk:.0f}% ({st['win']}) | فشل {lrk:.0f}% ({st['loss']})"
                        )

                    if early_no_factor_data > 0:
                        block.append(
                            f"(ملاحظة: {early_no_factor_data} صفقة مبكرة أقدم من إضافة حقل factors ولا تحوي "
                            f"التركيبة الدقيقة، فاستُبعدت من هذا التفصيل فقط دون التأثير على بقية التقرير)"
                        )

        elif ttype == "breakout":
            block.append("")
            block.append("— نسبة النجاح حسب عوامل جودة الانفجار —")
            for k in BREAKOUT_FACTOR_KEYS:
                st = breakout_factor_stats[k]
                if st["total"] == 0:
                    continue
                wrk = st["win"] / st["total"] * 100
                block.append(f"{BREAKOUT_FACTOR_LABELS[k]}: {st['total']} صفقة | نجاح {wrk:.0f}% (رابحة {st['win']} / خاسرة {st['loss']})")

        elif ttype == "experimental":
            block.append("")
            block.append("— نسبة النجاح حسب عوامل جودة التجريبية —")
            for k in EXPERIMENTAL_FACTOR_KEYS:
                st = experimental_factor_stats[k]
                if st["total"] == 0:
                    continue
                wrk = st["win"] / st["total"] * 100
                block.append(f"{EXPERIMENTAL_FACTOR_LABELS[k]}: {st['total']} صفقة | نجاح {wrk:.0f}% (رابحة {st['win']} / خاسرة {st['loss']})")

        lines.append("")
        lines.append(SECTION_DIVIDER)
        lines.append("")
        lines.extend(block)

    return "\n".join(lines), per_trade_lines


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    # تيليجرام يحدد طول الرسالة بـ 4096 حرف تقريبًا — نقسم لو تجاوز
    chunk = 3800
    for i in range(0, len(text), chunk):
        try:
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text[i:i + chunk]}, timeout=15)
        except Exception as e:
            print("تعذّر إرسال التقرير عبر تيليجرام:", e)


def main():
    trades = load_closed_trades()
    open_positions = load_open_positions()
    summary, per_trade_lines = build_report(trades, open_count=len(open_positions))

    print(summary)
    print("\n— تفصيل كل صفقة —")
    for line in per_trade_lines:
        print(line)

    # يُرسل الملخص فقط عبر تيليجرام (التفصيل الكامل لكل صفقة يبقى في سجل التشغيل GitHub Actions
    # تجنبًا لإغراق المحادثة برسالة طويلة جدًا)
    send_telegram(summary)


if __name__ == "__main__":
    main()
