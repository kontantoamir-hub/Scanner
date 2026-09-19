#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pnl_report.py
=============
يجاوب على سؤال واحد بشكل نهائي: هل البوت رابح أم خاسر؟

لكل نوع إشارة (official / early / breakout / experimental) ثم للكل معًا، يحسب:
  - كم تربح في المتوسط بالصفقة الرابحة
  - كم تخسر في المتوسط بالصفقة الخاسرة
  - نسبة النجاح الفعلية، ونسبة النجاح المطلوبة للتعادل
  - الصافي المتوقع لكل صفقة (expectancy) مع هامش ثقة تقريبي 95%
  - Profit Factor
  - حكم نهائي: إيجابي / سلبي / غير حاسم / عيّنة صغيرة

الربح والخسارة تُقرأ من الحقل net_pnl_pct (الصافي بعد الرسوم) في closed_trades.json.

الإعداد (متغيرات بيئة، كلها اختيارية عدا Gist لو لا يوجد ملف محلي):
    GIST_TOKEN, GIST_ID            نفس scanner.py
    BREAKEVEN_BAND_PCT             نطاق التعادل، افتراضي 0.1  (±0.1% لا تُحسب ربحًا ولا خسارة)
    MIN_TRADES_FOR_VERDICT         أقل عدد صفقات لإصدار حكم، افتراضي 30

تشغيل:
    python pnl_report.py
(أو ضع closed_trades.json محليًا بجانب السكربت لتشغيله بدون شبكة)
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

# الحقل الأساسي هو net_pnl_pct؛ البقية أسماء بديلة احتياطية فقط
PNL_KEYS = ("net_pnl_pct", "pnl_pct", "profit_pct")

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
    local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "closed_trades.json")
    if os.path.exists(local_path):
        with open(local_path, "r", encoding="utf-8") as f:
            return json.load(f)

    token = os.environ.get("GIST_TOKEN")
    gist_id = os.environ.get("GIST_ID")
    if not token or not gist_id:
        sys.exit(
            "خطأ: لا يوجد closed_trades.json محليًا، ولم يتم ضبط "
            "GIST_TOKEN و GIST_ID كمتغيرات بيئة لجلبه من الـ Gist."
        )

    r = requests.get(
        f"https://api.github.com/gists/{gist_id}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
        timeout=15,
    )
    r.raise_for_status()
    files = r.json().get("files", {})

    all_trades = []
    for name in sorted(fn for fn in files if fn.startswith(ARCHIVE_PREFIX)):
        all_trades.extend(_read_json_file(files[name], name))
    if GIST_FILENAME in files:
        all_trades.extend(_read_json_file(files[GIST_FILENAME], GIST_FILENAME))

    if not all_trades and GIST_FILENAME not in files:
        sys.exit(f"لم يتم العثور على '{GIST_FILENAME}' داخل الـ Gist. الملفات المتوفرة: {list(files.keys())}")

    return all_trades


# ---------------------------------------------------------------- الحسابات
def get_pnl(trade):
    """يرجع الربح/الخسارة الصافية للصفقة كنسبة مئوية، أو None لو الحقل غير موجود."""
    for key in PNL_KEYS:
        val = trade.get(key)
        if val is None:
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


def analyze(group):
    pnls, missing = [], 0
    for t in group:
        p = get_pnl(t)
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
        print(f"⚠️ {r['missing']} صفقة بدون حقل ربح/خسارة (استُبعدت من الحساب)")
    if r["n"] == 0:
        print("لا توجد بيانات ربح/خسارة قابلة للحساب.")
        return r

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


def main():
    trades = load_trades()
    if not isinstance(trades, list):
        sys.exit("خطأ: الملف لا يحتوي على قائمة صفقات كما هو متوقع.")

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

    print("\nملاحظة: الأرقام مبنية على الصفقات المغلقة فقط، وعلى الحقل net_pnl_pct.")
    print("الهامش الإحصائي تقريبي، والأهم أن تتراكم صفقات أكثر وعلى ظروف سوق مختلفة.")


if __name__ == "__main__":
    main()
