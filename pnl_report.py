#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pnl_report.py  (v2)
===================
يجاوب على سؤال واحد بشكل نهائي: هل البوت رابح أم خاسر؟

لكل نوع إشارة (official / early / breakout / experimental) ثم للكل معًا، يحسب:
  - كم تربح في المتوسط بالصفقة الرابحة، وكم تخسر في المتوسط بالصفقة الخاسرة
  - نسبة النجاح الفعلية، ونسبة النجاح المطلوبة للتعادل
  - الصافي المتوقع لكل صفقة (expectancy) مع هامش ثقة تقريبي 95%
  - Profit Factor
  - حكم نهائي: إيجابي / سلبي / غير حاسم / عيّنة صغيرة

مصدر الربح والخسارة لكل صفقة (بالترتيب):
  1) أول حقل موجود من: net_pnl_pct / pnl_pct / profit_pct / ... (انظر PNL_KEYS)
  2) وإلا: يُحسب من سعر الدخول وسعر الخروج ناقص الرسوم (TRADING_FEE_PCT)
  لو لم يُعثر على أي منهما يُطبع تشخيص بأسماء حقول عيّنة من السجلات.

الإعداد (متغيرات بيئة):
    GIST_TOKEN, GIST_ID            نفس scanner.py (لو موجودان يُقرأ من الـ Gist دائمًا)
    BREAKEVEN_BAND_PCT             نطاق التعادل، افتراضي 0.1
    MIN_TRADES_FOR_VERDICT         أقل عدد صفقات لإصدار حكم، افتراضي 30
    TRADING_FEE_PCT                رسوم الدخول+الخروج للحساب البديل، افتراضي 0.2

تشغيل:
    python pnl_report.py
(بدون GIST_TOKEN/GIST_ID يقرأ closed_trades.json المحلي بجانب السكربت)
"""

import os
import sys
import json
import math
import statistics

import requests

GIST_FILENAME = "closed_trades.json"
ARCHIVE_PREFIX = "closed_trades_archive_"

BREAKEVEN_BAND_PCT = float(os.environ.get("BREAKEVEN_BAND_PCT", "0.1"))
MIN_TRADES_FOR_VERDICT = int(os.environ.get("MIN_TRADES_FOR_VERDICT", "30"))
TRADING_FEE_PCT = float(os.environ.get("TRADING_FEE_PCT", "0.2"))

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
def _read_json_file(file_entry, filename):
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
    return json.loads(content)


def load_trades():
    """يرجع (الصفقات، وصف المصدر، ملاحظات). الأولوية للـ Gist لو المفاتيح موجودة."""
    token = os.environ.get("GIST_TOKEN")
    gist_id = os.environ.get("GIST_ID")
    local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "closed_trades.json")

    if token and gist_id:
        r = requests.get(
            f"https://api.github.com/gists/{gist_id}",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        r.raise_for_status()
        files = r.json().get("files", {})

        notes = [f"عدد ملفات الـ Gist: {len(files)}"]
        all_trades = []
        for name in sorted(fn for fn in files if fn.startswith(ARCHIVE_PREFIX)):
            part = _read_json_file(files[name], name)
            notes.append(f"{name}: {len(part)} صفقة")
            all_trades.extend(part)
        if GIST_FILENAME in files:
            part = _read_json_file(files[GIST_FILENAME], GIST_FILENAME)
            notes.append(f"{GIST_FILENAME}: {len(part)} صفقة")
            all_trades.extend(part)
        else:
            sys.exit(f"لم يتم العثور على '{GIST_FILENAME}' داخل الـ Gist. الملفات المتوفرة: {list(files.keys())[:20]}")
        return all_trades, "Gist", notes

    if os.path.exists(local_path):
        with open(local_path, "r", encoding="utf-8") as f:
            trades = json.load(f)
        return trades, "ملف محلي closed_trades.json", [f"{len(trades)} صفقة"]

    sys.exit(
        "خطأ: لم يتم ضبط GIST_TOKEN و GIST_ID، ولا يوجد closed_trades.json محليًا بجانب السكربت."
    )


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
