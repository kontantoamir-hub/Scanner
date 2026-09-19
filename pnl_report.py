#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pnl_report.py  (v3)
===================
يجاوب على سؤال واحد بشكل نهائي: هل البوت رابح أم خاسر؟

لكل نوع إشارة (official / early / breakout / experimental) ثم للكل معًا، يحسب:
  - كم تربح في المتوسط بالصفقة الرابحة، وكم تخسر في المتوسط بالصفقة الخاسرة
  - نسبة النجاح الفعلية، ونسبة النجاح المطلوبة للتعادل
  - الصافي المتوقع لكل صفقة (expectancy) مع هامش ثقة تقريبي 95%
  - Profit Factor
  - حكم نهائي: إيجابي / سلبي / غير حاسم / عيّنة صغيرة

ما الذي تغيّر في v3 (إصلاح التوقف عند 150 صفقة):
  scanner.py يحتفظ في closed_trades.json بآخر 150 صفقة فقط (ACTIVE_HISTORY_SIZE)،
  ويرحّل الأقدم إلى ملفات closed_trades_archive_NNNN.json داخل Gist أرشيف *منفصل*
  (يُنشأ عبر _create_new_gist وليس هو GIST_ID). معرّفات هذه الـ Gists مسجّلة في
  الملف archive_gists_chain.json داخل الـ Gist الرئيسي (وأيضًا archive_index.json).
  النسخة السابقة كانت تقرأ الـ Gist الرئيسي فقط فتتجاهل كل الأرشيف.
  الآن: يقرأ السلسلة، ويجلب كل Gist أرشيف، ويجمع كل الصفقات + السجل النشط.

مصدر الربح والخسارة لكل صفقة (بالترتيب):
  1) أول حقل موجود من: net_pnl_pct / pnl_pct / profit_pct / ... (انظر PNL_KEYS)
  2) وإلا: يُحسب من سعر الدخول وسعر الخروج ناقص الرسوم (TRADING_FEE_PCT)
  لو لم يُعثر على أي منهما يُطبع تشخيص بأسماء حقول عيّنة من السجلات.

الإعداد (متغيرات بيئة):
    GIST_TOKEN, GIST_ID            نفس scanner.py (لو موجودان يُقرأ من الـ Gist دائمًا)
    BREAKEVEN_BAND_PCT             نطاق التعادل، افتراضي 0.1
    MIN_TRADES_FOR_VERDICT         أقل عدد صفقات لإصدار حكم، افتراضي 30
    TRADING_FEE_PCT                رسوم الدخول+الخروج للحساب البديل، افتراضي 0.2
    ALLOW_PARTIAL                  لو 1: يكمل حتى لو فشل جلب Gist أرشيف (افتراضي 0 = يتوقف)

تشغيل:
    python pnl_report.py
(بدون GIST_TOKEN/GIST_ID يقرأ closed_trades.json المحلي بجانب السكربت
 + أي ملفات closed_trades_archive_*.json محلية بجانبه)
"""

import os
import sys
import glob
import json
import math
import statistics

import requests

GIST_FILENAME = "closed_trades.json"
ARCHIVE_PREFIX = "closed_trades_archive_"
ARCHIVE_CHAIN_FILE = "archive_gists_chain.json"   # نفس اسم الملف في scanner.py
ARCHIVE_INDEX_FILE = "archive_index.json"         # نفس اسم الملف في scanner.py

BREAKEVEN_BAND_PCT = float(os.environ.get("BREAKEVEN_BAND_PCT", "0.1"))
MIN_TRADES_FOR_VERDICT = int(os.environ.get("MIN_TRADES_FOR_VERDICT", "30"))
TRADING_FEE_PCT = float(os.environ.get("TRADING_FEE_PCT", "0.2"))
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


# ---------------------------------------------------------------- تحميل البيانات
def _headers(token):
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}


def _fetch_gist_files(gist_id, token):
    """يرجع قاموس ملفات Gist معيّن (اسم -> بيانات الملف)."""
    r = requests.get(f"https://api.github.com/gists/{gist_id}", headers=_headers(token), timeout=30)
    r.raise_for_status()
    return r.json().get("files", {})


def _read_json_file(file_entry, filename, token=None):
    """يقرأ محتوى ملف JSON من Gist، ويجلب النسخة الكاملة من raw_url لو كان مبتورًا."""
    content = file_entry.get("content")
    if file_entry.get("truncated") or content is None:
        raw_url = file_entry.get("raw_url")
        try:
            rr = requests.get(raw_url, headers=_headers(token) if token else None, timeout=30)
            rr.raise_for_status()
            content = rr.text
        except Exception as e:
            raise RuntimeError(f"تعذّر جلب المحتوى الكامل لـ {filename}: {e}") from e
    return json.loads(content or "[]")


def _archive_names(files):
    return sorted(fn for fn in files if fn.startswith(ARCHIVE_PREFIX))


def _find_archive_gist_ids(main_files, token, main_id):
    """يستخرج معرّفات Gists الأرشيف بالترتيب من archive_gists_chain.json
    (والباقي غير الموجود فيها من archive_index.json كاحتياط)."""
    ids = []
    if ARCHIVE_CHAIN_FILE in main_files:
        chain = _read_json_file(main_files[ARCHIVE_CHAIN_FILE], ARCHIVE_CHAIN_FILE, token)
        if isinstance(chain, list):
            ids.extend(str(x) for x in chain if x)
    if ARCHIVE_INDEX_FILE in main_files:
        try:
            index = _read_json_file(main_files[ARCHIVE_INDEX_FILE], ARCHIVE_INDEX_FILE, token)
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
    """يحذف السجلات المتطابقة تمامًا (حماية من تكرار ناتج عن ترحيل/إعادة تشغيل)."""
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


def _load_from_gist(token, gist_id):
    main_files = _fetch_gist_files(gist_id, token)
    notes = [f"الـ Gist الرئيسي: {len(main_files)} ملف"]

    if GIST_FILENAME not in main_files:
        sys.exit(f"لم يتم العثور على '{GIST_FILENAME}' داخل الـ Gist. "
                 f"الملفات المتوفرة: {list(main_files.keys())[:20]}")

    all_trades = []
    problems = []

    # 1) ملفات الأرشيف داخل الـ Gist الرئيسي نفسه (لو وُجدت)
    for name in _archive_names(main_files):
        part = _read_json_file(main_files[name], name, token)
        notes.append(f"[الرئيسي] {name}: {len(part)} صفقة")
        all_trades.extend(part)

    # 2) Gists الأرشيف المنفصلة حسب السلسلة
    archive_ids = _find_archive_gist_ids(main_files, token, gist_id)
    notes.append(f"عدد Gists الأرشيف في السلسلة: {len(archive_ids)}")
    for aid in archive_ids:
        if aid == gist_id:
            continue  # تمت قراءته أعلاه
        try:
            files = _fetch_gist_files(aid, token)
            names = _archive_names(files)
            count = 0
            for name in names:
                part = _read_json_file(files[name], name, token)
                count += len(part)
                all_trades.extend(part)
            notes.append(f"[أرشيف {aid[:8]}…] {len(names)} ملف، {count} صفقة")
        except Exception as e:
            problems.append(f"Gist أرشيف {aid}: {e}")

    if problems:
        msg = "⚠️ فشل جلب جزء من الأرشيف:\n   - " + "\n   - ".join(problems)
        if ALLOW_PARTIAL:
            print(msg + "\n   (ALLOW_PARTIAL=1 → سيكمل بنتائج ناقصة)")
            notes.append("⚠️ النتائج ناقصة بسبب فشل جلب بعض الأرشيف")
        else:
            sys.exit(msg + "\nتوقّف لتجنّب تقرير مضلّل. أعد المحاولة أو ضع ALLOW_PARTIAL=1.")

    # 3) السجل النشط (آخر 150) — في النهاية لأنه الأحدث
    active = _read_json_file(main_files[GIST_FILENAME], GIST_FILENAME, token)
    notes.append(f"{GIST_FILENAME} (النشط): {len(active)} صفقة")
    all_trades.extend(active)

    return all_trades, "Gist (النشط + كل الأرشيف)", notes


def _load_local():
    base = os.path.dirname(os.path.abspath(__file__))
    local_path = os.path.join(base, GIST_FILENAME)
    if not os.path.exists(local_path):
        return None

    trades, notes = [], []
    for path in sorted(glob.glob(os.path.join(base, ARCHIVE_PREFIX + "*.json"))):
        with open(path, "r", encoding="utf-8") as f:
            part = json.load(f)
        notes.append(f"{os.path.basename(path)}: {len(part)} صفقة")
        trades.extend(part)
    with open(local_path, "r", encoding="utf-8") as f:
        active = json.load(f)
    notes.append(f"{GIST_FILENAME}: {len(active)} صفقة")
    trades.extend(active)
    return trades, "ملفات محلية", notes


def load_trades():
    """يرجع (الصفقات، وصف المصدر، ملاحظات). الأولوية للـ Gist لو المفاتيح موجودة."""
    token = os.environ.get("GIST_TOKEN")
    gist_id = os.environ.get("GIST_ID")

    if token and gist_id:
        trades, source, notes = _load_from_gist(token, gist_id)
    else:
        local = _load_local()
        if local is None:
            sys.exit("خطأ: لم يتم ضبط GIST_TOKEN و GIST_ID، ولا يوجد closed_trades.json محليًا بجانب السكربت.")
        trades, source, notes = local

    trades, removed = _dedupe(trades)
    if removed:
        notes.append(f"تم حذف {removed} سجل مكرر تمامًا")
    return trades, source, notes


# ---------------------------------------------------------------- الحسابات
def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_pnl(trade):
    """يرجع (الربح/الخسارة الصافية بالمئة، طريقة الحساب) أو (None, None)."""
    for key in PNL_KEYS:
        val = _num(trade.get(key))
        if val is not None:
            return val, f"field:{key}"

    entry = next((v for v in (_num(trade.get(k)) for k in ENTRY_KEYS) if v), None)
    exit_ = next((v for v in (_num(trade.get(k)) for k in EXIT_KEYS) if v), None)
    if entry and exit_ and entry > 0 and exit_ > 0:
        return (exit_ / entry - 1) * 100 - TRADING_FEE_PCT, "computed"

    return None, None


def analyze(group):
    pnls, missing, how = [], 0, {}
    for t in group:
        p, h = get_pnl(t)
        if p is None:
            missing += 1
        else:
            pnls.append(p)
            how[h] = how.get(h, 0) + 1

    n = len(pnls)
    res = {"total": len(group), "n": n, "missing": missing, "how": how}
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
        return f"⚪ عيّنة صغيرة ({n} صفقة، الحد {MIN_TRADES_FOR_VERDICT}) — لا حكم بعد، الاتجاه الأولي {lean}"
    if r["lo"] > 0:
        return "🟢 إيجابي (بثقة تقريبية 95%)"
    if r["hi"] < 0:
        return "🔴 سلبي (بثقة تقريبية 95%)"
    return "🟡 غير حاسم — هامش الثقة يشمل الصفر، يلزم صفقات أكثر"


def short_verdict(r):
    if r["n"] == 0:
        return "لا بيانات"
    if r["n"] < MIN_TRADES_FOR_VERDICT:
        return "عيّنة صغيرة"
    if r["lo"] > 0:
        return "إيجابي"
    if r["hi"] < 0:
        return "سلبي"
    return "غير حاسم"


# ---------------------------------------------------------------- الطباعة
def print_block(title, group):
    r = analyze(group)
    print("\n" + "=" * 70)
    print(f"{title} — إجمالي: {r['total']} صفقة")
    print("=" * 70)

    if r["missing"]:
        print(f"⚠️ {r['missing']} صفقة بدون بيانات ربح/خسارة (استُبعدت من الحساب)")
    if r["n"] == 0:
        print("لا توجد بيانات ربح/خسارة قابلة للحساب.")
        return r

    if r["how"].get("computed"):
        print(f"ℹ️ {r['how']['computed']} صفقة حُسب ربحها من سعر الدخول والخروج ناقص رسوم {TRADING_FEE_PCT}%")

    if r["wins"]:
        print(f"  رابحة : {r['wins']} صفقة | متوسط الربح في الصفقة الرابحة   : {r['avg_win']:+.2f}%")
    else:
        print("  رابحة : 0 صفقة")
    if r["losses"]:
        print(f"  خاسرة : {r['losses']} صفقة | متوسط الخسارة في الصفقة الخاسرة : {r['avg_loss']:+.2f}%")
    else:
        print("  خاسرة : 0 صفقة")
    if r["flat"]:
        print(f"  تعادل (±{BREAKEVEN_BAND_PCT}%) : {r['flat']} صفقة")

    print(f"  نسبة النجاح الفعلية            : {r['win_rate']:.1f}%")
    if r["be_wr"] is not None:
        print(f"  نسبة النجاح المطلوبة للتعادل   : {r['be_wr']:.1f}%")
    print(f"  الصافي المتوقع لكل صفقة        : {r['mean']:+.2f}%   (هامش تقريبي 95%: {r['lo']:+.2f}% إلى {r['hi']:+.2f}%)")
    print(f"  الصافي الإجمالي                : {r['total_pnl']:+.2f}%")
    if r["pf"] is not None:
        print(f"  Profit Factor                  : {r['pf']:.2f}")
    print(f"  الحكم: {verdict(r)}")
    return r


def print_diagnostics(trades):
    """يطبع عيّنة سجل لكل نوع فيه صفقات بلا بيانات ربح، لمعرفة أسماء الحقول الفعلية."""
    printed_header = False
    for ttype, label in TYPES:
        sample = next((t for t in trades if t.get("type") == ttype and get_pnl(t)[0] is None), None)
        if sample is None:
            continue
        if not printed_header:
            print("\n" + "=" * 70)
            print("🔧 تشخيص: صفقات بلا بيانات ربح/خسارة — عيّنة سجل لكل نوع")
            print("(انسخ هذا الجزء وأرسله لتحديد اسم الحقل الصحيح)")
            print("=" * 70)
            printed_header = True
        txt = json.dumps(sample, ensure_ascii=False)
        if len(txt) > 900:
            txt = txt[:900] + " …"
        print(f"\n{label}:\n{txt}")


def main():
    trades, source, notes = load_trades()
    if not isinstance(trades, list):
        sys.exit("خطأ: الملف لا يحتوي على قائمة صفقات كما هو متوقع.")

    print(f"📥 مصدر البيانات: {source}")
    for n in notes:
        print(f"   - {n}")
    counts = " | ".join(f"{lbl} {sum(1 for t in trades if t.get('type') == k)}" for k, lbl in TYPES)
    print(f"   إجمالي المحمَّل: {len(trades)} صفقة ({counts})")

    results = []
    for ttype, label in TYPES:
        group = [t for t in trades if t.get("type") == ttype]
        results.append((label, print_block(label, group)))

    all_group = [t for t in trades if t.get("type") in {k for k, _ in TYPES}]
    results.append(("📊 الكل", print_block("📊 الكل (الأنواع الأربعة معًا)", all_group)))

    print("\n" + "=" * 70)
    print("الخلاصة")
    print("=" * 70)
    for label, r in results:
        if r["n"] == 0:
            print(f"  {label}: لا بيانات")
        else:
            print(f"  {label}: {r['n']} صفقة | صافي/صفقة {r['mean']:+.2f}% | {short_verdict(r)}")

    print_diagnostics(trades)

    print("\nملاحظة: الأرقام مبنية على الصفقات المغلقة فقط.")
    print("الهامش الإحصائي تقريبي، والأهم أن تتراكم صفقات أكثر وعلى ظروف سوق مختلفة.")


if __name__ == "__main__":
    main()
