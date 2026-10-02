#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trade_stats.py  (نسخة موحّدة)
=============================
تقرير واحد يدمج:
  (أ) تقرير الربحية (كان في pnl_report.py): متوسط الربح/الخسارة، نسبة النجاح مقابل نسبة
      التعادل، الصافي المتوقع لكل صفقة مع هامش ثقة 95%، Profit Factor، الحكم، "وضعية البوت".
  (ب) التفصيل حسب المؤشرات/العوامل (كان في trade_stats.py) — لكن كل سطر تفصيلي الآن يعرض
      "صافي/صفقة" بعد الرسوم وليس نسبة النجاح فقط، وأي مجموعة أقل من MIN_TRADES_FOR_VERDICT
      صفقة تُعلَّم بـ ⚪ عيّنة صغيرة بدل عرض نسبة قد تكون ضجيجًا.

كل الأرقام تُقرأ من لقطة واحدة (السجل النشط + كل الأرشيف عبر archive_gists_chain.json).
للقراءة والعرض فقط — لا يعدّل شيئًا في scanner.py أو في الـ Gist.

المتغيرات المطلوبة (نفس Secrets):
    GIST_TOKEN, GIST_ID
اختياري:
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID   إرسال التقرير عبر تيليجرام (رسالة لكل قسم)
    TRADING_FEE_PCT                    الرسوم (دخول+خروج)، افتراضي 0.2
    BREAKEVEN_BAND_PCT                 نطاق التعادل حول الصفر، افتراضي 0.1
    MIN_TRADES_FOR_VERDICT             أقل عدد صفقات لإصدار حكم، افتراضي 30
    ALLOW_PARTIAL                      لو 1: يكمل حتى لو فشل جلب Gist أرشيف (افتراضي 0 = يتوقف)
"""

import os
import sys
import json
import math
import statistics
import datetime as dt

import requests

# ---------------------------------------------------------------- الإعدادات
GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

CLOSED_GIST_FILE = "closed_trades.json"
ARCHIVE_PREFIX = "closed_trades_archive_"
ARCHIVE_CHAIN_FILE = "archive_gists_chain.json"
ARCHIVE_INDEX_FILE = "archive_index.json"
OPEN_POSITIONS_GIST_FILE = "open_positions.json"

TRADING_FEE_PCT = float(os.environ.get("TRADING_FEE_PCT", "0.2"))
BREAKEVEN_BAND_PCT = float(os.environ.get("BREAKEVEN_BAND_PCT", "0.1"))
MIN_TRADES_FOR_VERDICT = int(os.environ.get("MIN_TRADES_FOR_VERDICT", "30"))
ALLOW_PARTIAL = os.environ.get("ALLOW_PARTIAL", "0") == "1"

PNL_KEYS = ("net_pnl_pct", "pnl_pct", "profit_pct", "net_profit_pct",
            "realized_pnl_pct", "result_pct", "pnl_percent", "profit_percent")
ENTRY_KEYS = ("entry", "entry_price", "open_price", "buy_price")
EXIT_KEYS = ("exit_price", "close_price", "closed_price", "sell_price", "exit")

TYPES = [
    ("official", "🔴 رسمية"),
    ("early", "🔵 مبكرة"),
    ("breakout", "🟠 انفجار"),
    ("experimental", "🟣 تجريبية"),
]
TYPE_KEYS = {k for k, _ in TYPES}
TYPE_SHORT = {"official": "رسمية", "early": "مبكرة", "breakout": "انفجار", "experimental": "تجريبية"}

# الأنواع التي تُحسب لها الأعلام الخام (squeeze/accumulation/...) وتفصيل "بدون مؤشر إضافي"
RAW_TYPES = ("official", "early")

INDICATOR_KEYS = ["squeeze", "accumulation", "divergence", "momentum", "extended"]
INDICATOR_LABELS = {
    "squeeze": "انضغاط تقلب (Squeeze)",
    "accumulation": "تراكم صامت (Accumulation)",
    "divergence": "دايفرجنس (Divergence)",
    "momentum": "زخم (Momentum)",
    "extended": "امتداد زائد (Overextension)",
}

BREAKOUT_FACTOR_KEYS = ["trend_support", "macd_bull", "rsi_ok"]
BREAKOUT_FACTOR_LABELS = {
    "trend_support": "دعم اتجاه EMA7/14",
    "macd_bull": "MACD إيجابي",
    "rsi_ok": "RSI في نطاق صحي",
}

EXPERIMENTAL_FACTOR_KEYS = ["trend_support", "volume_confirm", "mfi_bullish"]
EXPERIMENTAL_FACTOR_LABELS = {
    "trend_support": "دعم اتجاه EMA7/14",
    "volume_confirm": "تأكيد حجم + OBV",
    "mfi_bullish": "تدفق أموال صاعد (MFI)",
}

EARLY_FACTOR_KEYS = ["accumulation", "divergence", "momentum", "squeeze"]
EARLY_FACTOR_LABELS = {
    "accumulation": "تراكم صامت (Accumulation)",
    "divergence": "دايفرجنس (Divergence)",
    "momentum": "زخم (Momentum)",
    "squeeze": "انضغاط تقلب (Squeeze)",
}
EARLY_COMBO_LABELS = {1: "مؤشر واحد", 2: "مؤشرين", 3: "3 مؤشرات", 4: "4 مؤشرات"}

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

SEP = "═" * 31
THIN = "─" * 31


# ---------------------------------------------------------------- تحميل البيانات
def _headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def _fetch_gist_files(gist_id):
    r = requests.get(f"https://api.github.com/gists/{gist_id}", headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json().get("files", {})


def _read_json_file(file_entry, filename):
    """يقرأ ملف JSON من Gist، ويجلب النسخة الكاملة من raw_url لو كان مبتورًا."""
    content = file_entry.get("content")
    if file_entry.get("truncated") or content is None:
        raw_url = file_entry.get("raw_url")
        try:
            rr = requests.get(raw_url, headers=_headers(), timeout=30)
            rr.raise_for_status()
            content = rr.text
        except Exception as e:
            raise RuntimeError(f"تعذّر جلب المحتوى الكامل لـ {filename}: {e}") from e
    return json.loads(content or "[]")


def _archive_names(files):
    return sorted(fn for fn in files if fn.startswith(ARCHIVE_PREFIX))


def _find_archive_gist_ids(main_files):
    ids = []
    if ARCHIVE_CHAIN_FILE in main_files:
        try:
            chain = _read_json_file(main_files[ARCHIVE_CHAIN_FILE], ARCHIVE_CHAIN_FILE)
            if isinstance(chain, list):
                ids.extend(str(x) for x in chain if x)
        except Exception as e:
            print(f"⚠️ تعذّر تحليل {ARCHIVE_CHAIN_FILE}: {e}")
    if ARCHIVE_INDEX_FILE in main_files:
        try:
            index = _read_json_file(main_files[ARCHIVE_INDEX_FILE], ARCHIVE_INDEX_FILE)
            if isinstance(index, dict):
                ids.extend(str(k) for k in index.keys() if str(k) not in ids)
        except Exception:
            pass
    seen, ordered = set(), []
    for gid in ids:
        if gid not in seen:
            seen.add(gid)
            ordered.append(gid)
    return ordered


def _dedupe(trades):
    seen, out = set(), []
    for t in trades:
        try:
            key = json.dumps(t, sort_keys=True, ensure_ascii=False)
        except Exception:
            out.append(t)
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out, len(trades) - len(out)


def load_closed_trades():
    """السجل النشط + كل ملفات الأرشيف (في الـ Gist الرئيسي وفي Gists الأرشيف المنفصلة)،
    كلها في لقطة واحدة، مع حذف التكرار التام."""
    if not GIST_TOKEN or not GIST_ID:
        sys.exit("❌ GIST_TOKEN أو GIST_ID غير موجودين في متغيرات البيئة.")

    main_files = _fetch_gist_files(GIST_ID)
    if CLOSED_GIST_FILE not in main_files:
        sys.exit(f"لم يتم العثور على '{CLOSED_GIST_FILE}' داخل الـ Gist. "
                 f"الملفات المتوفرة: {list(main_files.keys())[:20]}")

    all_trades, problems, notes = [], [], []

    for name in _archive_names(main_files):
        part = _read_json_file(main_files[name], name)
        notes.append(f"[الرئيسي] {name}: {len(part)} صفقة")
        all_trades.extend(part)

    archive_ids = _find_archive_gist_ids(main_files)
    notes.append(f"عدد Gists الأرشيف في السلسلة: {len(archive_ids)}")
    for aid in archive_ids:
        if aid == GIST_ID:
            continue
        try:
            files = _fetch_gist_files(aid)
            count = 0
            for name in _archive_names(files):
                part = _read_json_file(files[name], name)
                count += len(part)
                all_trades.extend(part)
            notes.append(f"[أرشيف {aid[:8]}…] {count} صفقة")
        except Exception as e:
            problems.append(f"Gist أرشيف {aid}: {e}")

    if problems:
        msg = "⚠️ فشل جلب جزء من الأرشيف:\n   - " + "\n   - ".join(problems)
        if ALLOW_PARTIAL:
            print(msg + "\n   (ALLOW_PARTIAL=1 → سيكمل بنتائج ناقصة)")
            notes.append("⚠️ النتائج ناقصة بسبب فشل جلب بعض الأرشيف")
        else:
            sys.exit(msg + "\nتوقّف لتجنّب تقرير مضلّل. أعد المحاولة أو ضع ALLOW_PARTIAL=1.")

    active = _read_json_file(main_files[CLOSED_GIST_FILE], CLOSED_GIST_FILE)
    notes.append(f"{CLOSED_GIST_FILE} (النشط): {len(active)} صفقة")
    all_trades.extend(active)

    all_trades, removed = _dedupe(all_trades)
    if removed:
        notes.append(f"تم حذف {removed} سجل مكرر تمامًا")
    return all_trades, notes


def load_open_positions():
    try:
        files = _fetch_gist_files(GIST_ID)
        if OPEN_POSITIONS_GIST_FILE not in files:
            return []
        return _read_json_file(files[OPEN_POSITIONS_GIST_FILE], OPEN_POSITIONS_GIST_FILE)
    except Exception as e:
        print(f"⚠️ تعذّر جلب الصفقات المفتوحة حاليًا: {e}")
        return []


# ---------------------------------------------------------------- الحسابات
def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_pnl(trade):
    """الربح/الخسارة الصافية% للصفقة: أول حقل جاهز من PNL_KEYS، وإلا يُحسب من سعر الدخول
    والخروج ناقص الرسوم. يرجع (القيمة، الطريقة) أو (None, None)."""
    for key in PNL_KEYS:
        val = _num(trade.get(key))
        if val is not None:
            return val, f"field:{key}"
    entry = next((v for v in (_num(trade.get(k)) for k in ENTRY_KEYS) if v), None)
    exit_ = next((v for v in (_num(trade.get(k)) for k in EXIT_KEYS) if v), None)
    if entry and exit_ and entry > 0 and exit_ > 0:
        return (exit_ / entry - 1) * 100 - TRADING_FEE_PCT, "computed"
    return None, None


def has_pnl(trade):
    return get_pnl(trade)[0] is not None


def classify(trade):
    p, _ = get_pnl(trade)
    if p is None:
        return None
    if p > BREAKEVEN_BAND_PCT:
        return "win"
    if p < -BREAKEVEN_BAND_PCT:
        return "loss"
    return "flat"


def analyze(group):
    pnls, missing = [], 0
    for t in group:
        p, _ = get_pnl(t)
        if p is None:
            missing += 1
        else:
            pnls.append(p)

    n = len(pnls)
    res = {"total": len(group), "n": n, "missing": missing}
    if n == 0:
        return res

    wins = [p for p in pnls if p > BREAKEVEN_BAND_PCT]
    losses = [p for p in pnls if p < -BREAKEVEN_BAND_PCT]
    flat = n - len(wins) - len(losses)

    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    mean = sum(pnls) / n
    sd = statistics.stdev(pnls) if n >= 2 else 0.0
    se = sd / math.sqrt(n) if n >= 2 else 0.0

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else None
    be_wr = (abs(avg_loss) / (avg_win + abs(avg_loss)) * 100) if (wins and losses) else None

    res.update({
        "wins": len(wins), "losses": len(losses), "flat": flat,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "win_rate": len(wins) / n * 100,
        "be_wr": be_wr,
        "mean": mean, "total_pnl": sum(pnls),
        "lo": mean - 1.96 * se, "hi": mean + 1.96 * se,
        "pf": pf,
    })
    return res


def verdict(r):
    n, mean = r["n"], r["mean"]
    if n < MIN_TRADES_FOR_VERDICT:
        lean = "إيجابي" if mean > 0 else "سلبي"
        return f"⚪ عيّنة صغيرة ({n}/{MIN_TRADES_FOR_VERDICT}) — لا حكم بعد، الاتجاه الأولي {lean}"
    if r["lo"] > 0:
        return "🟢 إيجابي (بثقة تقريبية 95%)"
    if r["hi"] < 0:
        return "🔴 سلبي (بثقة تقريبية 95%)"
    return "🟡 غير حاسم — هامش الثقة يشمل الصفر، يلزم صفقات أكثر"


def bot_status(r):
    """وضعية البوت من Profit Factor: (أيقونة، اسم)."""
    pf = r.get("pf")
    if pf is None:
        return ("🟢🟢", "ممتاز (بدون خسائر)") if r.get("wins") else ("⚪", "لا بيانات كافية")
    if pf < 1:
        return "🔴", "خاسر"
    if pf < 1.3:
        return "🟡", "رابح ضعيف"
    if pf <= 2:
        return "🟢", "رابح جيد"
    return "🟢🟢", "ممتاز"


def pf_text(r):
    return f"PF {r['pf']:.2f}" if r.get("pf") is not None else "PF ∞"


def status_line(r):
    icon, name = bot_status(r)
    txt = f"🤖 الوضعية: {icon} {name} ({pf_text(r)})"
    if r["n"] < MIN_TRADES_FOR_VERDICT:
        txt += " ⚪ لكن العيّنة صغيرة"
    return txt


# ---------------------------------------------------------------- بناء الأقسام
def overall_block(title, group):
    """قسم كامل لنوع (أو للكل): ربحية + وضعية + حكم. يرجع (الأسطر، نتيجة التحليل)."""
    r = analyze(group)
    lines = [SEP, f"{title} — {r['total']} صفقة", SEP]
    if r["missing"]:
        lines.append(f"⚠️ {r['missing']} صفقة بدون بيانات ربح/خسارة (استُبعدت من الحساب)")
    if r["n"] == 0:
        lines.append("لا توجد بيانات ربح/خسارة قابلة للحساب.")
        return lines, r

    lines.append(status_line(r))
    if r["wins"]:
        lines.append(f"رابحة {r['wins']} | متوسط {r['avg_win']:+.2f}%")
    else:
        lines.append("رابحة 0")
    if r["losses"]:
        lines.append(f"خاسرة {r['losses']} | متوسط {r['avg_loss']:+.2f}%")
    else:
        lines.append("خاسرة 0")
    if r["flat"]:
        lines.append(f"تعادل (±{BREAKEVEN_BAND_PCT}%) {r['flat']}")
    lines.append(f"نجاح فعلي {r['win_rate']:.1f}%" +
                 (f" | مطلوب للتعادل {r['be_wr']:.1f}%" if r["be_wr"] is not None else ""))
    lines.append(f"صافي/صفقة {r['mean']:+.2f}% (95%: {r['lo']:+.2f}% إلى {r['hi']:+.2f}%)")
    lines.append(f"الصافي الإجمالي {r['total_pnl']:+.2f}%")
    lines.append(f"الحكم: {verdict(r)}")
    return lines, r


def group_line(label, subset):
    """سطر تفصيلي لمجموعة فرعية. أقل من MIN_TRADES_FOR_VERDICT => ⚪ عيّنة صغيرة."""
    r = analyze(subset)
    if r["n"] == 0:
        return None
    if r["n"] < MIN_TRADES_FOR_VERDICT:
        return f"{label}: {r['n']} ⚪ عيّنة صغيرة (رابحة {r['wins']} / خاسرة {r['losses']})"
    if r["lo"] > 0:
        icon = "🟢"
    elif r["hi"] < 0:
        icon = "🔴"
    else:
        icon = "🟡"
    return f"{label}: {r['n']} | نجاح {r['win_rate']:.0f}% | صافي {r['mean']:+.2f}% {icon}"


def base_state_label(key, value):
    if key == "rsi_state":
        return {1: "تشبع بيعي (RSI<35)", -1: "تشبع شرائي (RSI>65)", 0: "محايد"}.get(value, "غير معروف")
    if key == "bb_state":
        return {1: "عند الحد السفلي", -1: "عند الحد العلوي", 0: "منتصف النطاق"}.get(value, "غير معروف")
    if value is True:
        return "حاضر"
    if value is False:
        return "غائب"
    return "لم يُفحص"


def _section(title, rows):
    rows = [r for r in rows if r]
    if not rows:
        return []
    return ["", f"— {title} —"] + rows


def details_lines(ttype, group):
    """التفصيل حسب المؤشرات/العوامل لنوع واحد فقط (لا يختلط نوع بآخر)."""
    group = [t for t in group if has_pnl(t)]
    out = []

    if ttype in RAW_TYPES:
        ind_lists = {k: [] for k in INDICATOR_KEYS}
        no_ind = []
        base_lists = {k: {} for k in BASE_INDICATOR_KEYS}
        no_basedata = 0

        for t in group:
            inds = [k for k in INDICATOR_KEYS if t.get(k) is True]
            if inds:
                for k in inds:
                    ind_lists[k].append(t)
            else:
                no_ind.append(t)
                if not any(k in t for k in BASE_INDICATOR_KEYS):
                    no_basedata += 1
                else:
                    for k in BASE_INDICATOR_KEYS:
                        if k in t:
                            base_lists[k].setdefault(base_state_label(k, t.get(k)), []).append(t)

        rows = [group_line(INDICATOR_LABELS[k], ind_lists[k]) for k in INDICATOR_KEYS]
        rows.append(group_line("بدون مؤشر إضافي", no_ind))
        out += _section("حسب المؤشر", rows)

        if no_ind:
            with_data = len(no_ind) - no_basedata
            if with_data > 0:
                rows = []
                for k in BASE_INDICATOR_KEYS:
                    states = base_lists[k]
                    if not states:
                        continue
                    rows.append(f"{BASE_INDICATOR_LABELS[k]}:")
                    for label, lst in states.items():
                        ln = group_line(f"  • {label}", lst)
                        if ln:
                            rows.append(ln)
                out += _section(f"تفصيل 'بدون مؤشر إضافي' ({with_data} صفقة تحوي بيانات)", rows)
                if no_basedata:
                    out.append(f"(ملاحظة: {no_basedata} صفقة أقدم من تحديث الحفظ التشخيصي فاستُبعدت من هذا التفصيل فقط)")
            else:
                out += ["", f"(كل صفقات 'بدون مؤشر إضافي' الـ{len(no_ind)} أقدم من تحديث الحفظ التشخيصي — "
                            f"ستظهر البيانات تدريجيًا مع الصفقات الجديدة)"]

        if ttype == "early":
            single = {k: [] for k in EARLY_FACTOR_KEYS}
            combo = {n: [] for n in (1, 2, 3, 4)}
            no_factor = 0
            for t in group:
                f = t.get("factors")
                if not f:
                    no_factor += 1
                    continue
                n = len(f)
                if n == 1 and f[0] in single:
                    single[f[0]].append(t)
                if n in combo:
                    combo[n].append(t)
            out += _section("نجاح كل مؤشر لوحده (بدون أي مؤشر ثانٍ معه)",
                            [group_line(EARLY_FACTOR_LABELS[k], single[k]) for k in EARLY_FACTOR_KEYS])
            out += _section("حسب عدد المؤشرات المتعاونة معًا",
                            [group_line(EARLY_COMBO_LABELS[n], combo[n]) for n in (1, 2, 3, 4)])
            if no_factor:
                out.append(f"(ملاحظة: {no_factor} صفقة مبكرة أقدم من إضافة حقل factors فاستُبعدت من هذا التفصيل فقط)")

    elif ttype == "breakout":
        lists = {k: [] for k in BREAKOUT_FACTOR_KEYS}
        for t in group:
            det = t.get("breakout_details") or {}
            for k in BREAKOUT_FACTOR_KEYS:
                if det.get(k) is True:
                    lists[k].append(t)
        out += _section("حسب عوامل جودة الانفجار",
                        [group_line(BREAKOUT_FACTOR_LABELS[k], lists[k]) for k in BREAKOUT_FACTOR_KEYS])

    elif ttype == "experimental":
        lists = {k: [] for k in EXPERIMENTAL_FACTOR_KEYS}
        for t in group:
            det = t.get("experimental_details") or {}
            for k in EXPERIMENTAL_FACTOR_KEYS:
                if det.get(k) is True:
                    lists[k].append(t)
        out += _section("حسب عوامل جودة التجريبية",
                        [group_line(EXPERIMENTAL_FACTOR_LABELS[k], lists[k]) for k in EXPERIMENTAL_FACTOR_KEYS])

    return out


def duration_hours(trade):
    try:
        t0 = dt.datetime.strptime(trade["opened_at"], "%Y-%m-%d %H:%M:%S")
        t1 = dt.datetime.strptime(trade["closed_at"], "%Y-%m-%d %H:%M:%S")
        return (t1 - t0).total_seconds() / 3600
    except Exception:
        return None


def per_trade_line(t):
    outcome = classify(t)
    if outcome is None:
        return None
    ttype = t.get("type", "official")
    pnl, _ = get_pnl(t)
    dur = duration_hours(t)
    if ttype == "breakout":
        det = t.get("breakout_details") or {}
        names = [BREAKOUT_FACTOR_LABELS[k] for k in BREAKOUT_FACTOR_KEYS if det.get(k) is True]
    elif ttype == "experimental":
        det = t.get("experimental_details") or {}
        names = [EXPERIMENTAL_FACTOR_LABELS[k] for k in EXPERIMENTAL_FACTOR_KEYS if det.get(k) is True]
    else:
        names = [INDICATOR_LABELS[k] for k in INDICATOR_KEYS if t.get(k) is True] or ["بدون مؤشر إضافي"]
    outcome_ar = {"win": "✅ ربح", "loss": "❌ خسارة", "flat": "⚪ تعادل"}[outcome]
    return (f"{t.get('symbol', '?')} | نوع: {TYPE_SHORT.get(ttype, ttype)} | score: {t.get('score', '?')} | "
            f"{outcome_ar} | عائد: {pnl:+.2f}% | مدة: {'%.0fس' % dur if dur is not None else '—'} | "
            f"المؤشرات: {'، '.join(names) if names else 'بدون عوامل مسجّلة'}")


def build_messages(trades, open_count=None):
    """يرجع قائمة رسائل (كل قسم رسالة مستقلة) لتُطبع وتُرسل بالترتيب."""
    valid = [t for t in trades if t.get("type") in TYPE_KEYS]
    skipped = [t for t in trades if t.get("type") not in TYPE_KEYS]
    print(f"🔎 loaded={len(trades)} | valid={len(valid)} | skipped(type)={len(skipped)}")
    print("   أنواع مستبعدة:", {str(t.get('type')) for t in skipped})
    if not valid:
        msg = "لا توجد صفقات مغلقة بعد في السجل."
        if open_count is not None:
            msg += f"\n🔄 مفتوحة حاليًا: {open_count} صفقة"
        return [msg], []

    all_lines, all_r = overall_block("📊 الكل", valid)

    # --- رأس التقرير ---
    head = ["📊 تقرير الصفقات الموحّد",
            f"المغلقة: {len(valid)}" + (f" | 🔄 المفتوحة: {open_count}" if open_count is not None else ""),
            f"الرسوم المحسوبة: {TRADING_FEE_PCT}% لكل صفقة"]
    if all_r["n"]:
        icon, name = bot_status(all_r)
        head += ["", f"🤖 وضعية البوت العامة: {icon} {name} ({pf_text(all_r)})"]
    messages = ["\n".join(head)]

    # --- قسم لكل نوع: ربحية + تفصيل ---
    results = []
    for ttype, label in TYPES:
        group = [t for t in valid if t.get("type") == ttype]
        if not group:
            continue
        lines, r = overall_block(label, group)
        lines += details_lines(ttype, group)
        messages.append("\n".join(lines))
        results.append((label, r))

    # --- الكل ---
    messages.append("\n".join(all_lines))
    results.append(("📊 الكل", all_r))

    # --- الخلاصة + دليل الوضعية ---
    summ = [SEP, "🏁 الخلاصة", SEP]
    for label, r in results:
        if r["n"] == 0:
            summ.append(f"{label}: لا بيانات")
            continue
        icon, name = bot_status(r)
        state = "⚪ عيّنة صغيرة" if r["n"] < MIN_TRADES_FOR_VERDICT else f"{icon} {name}"
        summ.append(f"{label} {r['n']} | {r['mean']:+.2f}% | {pf_text(r)} | {state}")
    summ += ["", "— دليل وضعية البوت (PF) —",
             "🔴 أقل من 1: خاسر",
             "🟡 من 1 إلى 1.3: رابح ضعيف",
             "🟢 من 1.3 إلى 2: رابح جيد",
             "🟢🟢 فوق 2: ممتاز",
             f"⚪ أقل من {MIN_TRADES_FOR_VERDICT} صفقة: لا حكم نهائي",
             "",
             "ملاحظة: الأرقام مبنية على الصفقات المغلقة فقط، والهامش الإحصائي تقريبي."]
    messages.append("\n".join(summ))

    per_trade = [ln for ln in (per_trade_line(t) for t in valid) if ln]
    return messages, per_trade


# ---------------------------------------------------------------- تيليجرام
def _split_message(text, limit=3800):
    """يقسّم نصًا طويلًا على حدود الأسطر (حد تيليجرام 4096 حرفًا)."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


def send_telegram(messages):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for msg in messages:
        for part in _split_message(msg):
            try:
                resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": part}, timeout=15)
                if resp.status_code != 200:
                    print(f"⚠️ تيليجرام رفض الرسالة ({resp.status_code}): {resp.text[:200]}")
            except Exception as e:
                print("تعذّر إرسال التقرير عبر تيليجرام:", e)


def main():
    trades, notes = load_closed_trades()
    open_positions = load_open_positions()

    print("📥 مصدر البيانات: Gist (النشط + كل الأرشيف)")
    for n in notes:
        print(f"   - {n}")
    print(f"   إجمالي المحمَّل: {len(trades)} صفقة\n")

    messages, per_trade = build_messages(trades, open_count=len(open_positions))
    print("\n\n".join(messages))
    print("\n— تفصيل كل صفقة —")
    for line in per_trade:
        print(line)

    # التفصيل الكامل لكل صفقة يبقى في سجل GitHub Actions فقط (لا يُرسل لتيليجرام)
    send_telegram(messages)


if __name__ == "__main__":
    main()
