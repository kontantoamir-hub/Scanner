"""
score_ranges.py
----------------
يقرأ كل الصفقات المغلقة (closed_trades.json + كل ملفات الأرشيف closed_trades_archive_*.json)
من نفس الـ Gist الذي يستخدمه scanner.py، ويحسب نسبة النجاح الفعلية لكل درجة (score)،
منفصلة بين النوعين "official" و"early" (لأن العلاقة بين الدرجة والنجاح مختلفة بينهما).

ثم يقترح تقسيم كل نوع إلى 3 نطاقات (ضعيفة / متوسطة / قوية) بحيث كل نطاق يحوي
عدد صفقات كافٍ (وليس نطاقات ثابتة عشوائية)، عبر تقسيم الدرجات المرتبة إلى 3 مجموعات
متقاربة الحجم (بالعدد)، ثم حساب متوسط النجاح الفعلي لكل مجموعة.

تشغيل يدوي فقط (GitHub Action منفصل مثل باقي سكربتات التحليل)، لا يعدّل أي بيانات،
فقط يطبع تقرير نصي.

المتغيرات البيئية المطلوبة: GIST_TOKEN, GIST_ID
"""

import os
import json
import requests

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")

CLOSED_GIST_FILE = "closed_trades.json"
ARCHIVE_PREFIX = "closed_trades_archive_"

MIN_SAMPLE_PER_SCORE = 5  # تحت هذا العدد، الدرجة تُدمج مع أقرب مجموعة بدل تصنيف منفرد غير موثوق


def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def _gist_get_all_files():
    if not GIST_TOKEN or not GIST_ID:
        raise SystemExit("❌ GIST_TOKEN أو GIST_ID غير موجودين بمتغيرات البيئة.")
    r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=_gist_headers(), timeout=15)
    r.raise_for_status()
    return r.json().get("files", {})


def load_all_closed_trades():
    """يجمع closed_trades.json + كل ملفات الأرشيف في قائمة واحدة."""
    files = _gist_get_all_files()
    all_trades = []

    if CLOSED_GIST_FILE in files:
        try:
            all_trades.extend(json.loads(files[CLOSED_GIST_FILE]["content"]))
        except Exception as e:
            print(f"⚠️ تعذّر قراءة {CLOSED_GIST_FILE}: {e}")

    archive_names = sorted(fn for fn in files if fn.startswith(ARCHIVE_PREFIX))
    for fname in archive_names:
        try:
            all_trades.extend(json.loads(files[fname]["content"]))
        except Exception as e:
            print(f"⚠️ تعذّر قراءة {fname}: {e}")

    return all_trades


def pnl_pct(trade):
    entry = trade.get("entry")
    exit_price = trade.get("exit_price")
    if entry is None or exit_price is None:
        return None
    return (exit_price - entry) / entry * 100


def build_score_table(trades, trade_type):
    """يرجع dict: score (مقرّب لأقرب 0.5) -> {count, wins, pnl_sum}"""
    table = {}
    for t in trades:
        if t.get("type") != trade_type:
            continue
        score = t.get("score")
        if score is None:
            continue
        p = pnl_pct(t)
        if p is None:
            continue
        key = round(score * 2) / 2  # تقريب لأقرب 0.5
        row = table.setdefault(key, {"count": 0, "wins": 0, "pnl_sum": 0.0})
        row["count"] += 1
        row["pnl_sum"] += p
        if p > 0:
            row["wins"] += 1
    return table


def split_into_3_ranges(score_table):
    """
    يرتب الدرجات تصاعديًا، ثم يقسمها لـ3 مجموعات متقاربة بعدد الصفقات (وليس عدد قيم الدرجة)،
    بحيث كل مجموعة تمثل نطاق درجات متجاور (ضعيفة = أقل الدرجات، قوية = أعلاها) —
    الترتيب حسب رقم الدرجة نفسه، والتصنيف (ضعيفة/متوسطة/قوية) بحسب نسبة النجاح الفعلية
    لكل مجموعة وليس افتراض أن الأعلى رقمًا هو الأقوى.
    """
    scores_sorted = sorted(score_table.keys())
    total_trades = sum(row["count"] for row in score_table.values())
    if total_trades == 0 or not scores_sorted:
        return []

    target_per_group = total_trades / 3
    groups = []
    current_group = []
    current_count = 0

    for s in scores_sorted:
        current_group.append(s)
        current_count += score_table[s]["count"]
        if current_count >= target_per_group and len(groups) < 2:
            groups.append(current_group)
            current_group = []
            current_count = 0
    if current_group:
        groups.append(current_group)
    elif not groups:
        groups.append(scores_sorted)

    # دمج أي مجموعة فارغة محتملة بالمجموعة المجاورة
    groups = [g for g in groups if g]

    summarized = []
    for g in groups:
        count = sum(score_table[s]["count"] for s in g)
        wins = sum(score_table[s]["wins"] for s in g)
        pnl_sum = sum(score_table[s]["pnl_sum"] for s in g)
        summarized.append({
            "range": (min(g), max(g)),
            "count": count,
            "success_pct": round(wins / count * 100, 1) if count else None,
            "avg_pnl": round(pnl_sum / count, 2) if count else None,
        })
    return summarized


def label_by_success(summaries):
    """
    يرتب المجموعات الثلاث حسب نسبة النجاح الفعلية (وليس حسب رقم الدرجة) ويعطيها
    التسمية (ضعيفة/متوسطة/قوية) — هذا يتفادى افتراض أن الدرجة الأعلى = أداء أفضل،
    وهو افتراض أثبتت بياناتك أنه غير صحيح دائمًا.
    """
    labels = ["ضعيفة", "متوسطة", "قوية"]
    ranked = sorted(summaries, key=lambda x: (x["success_pct"] is not None, x["success_pct"]))
    for i, item in enumerate(ranked):
        item["label"] = labels[i] if i < len(labels) else labels[-1]
    # نرجعها مرتبة حسب رقم الدرجة (للعرض الطبيعي: من الأقل للأعلى) مع احتفاظها بالتسمية المحسوبة
    return sorted(ranked, key=lambda x: x["range"][0])


def print_report(trade_type_label, summaries):
    print(f"\n=== {trade_type_label} ===")
    if not summaries:
        print("لا توجد بيانات كافية.")
        return
    for s in summaries:
        lo, hi = s["range"]
        rng_txt = f"{lo:.1f}" if lo == hi else f"{lo:.1f} - {hi:.1f}"
        print(f"  نطاق الدرجة {rng_txt}  ->  التصنيف: {s['label']:<6}  "
              f"| عدد الصفقات: {s['count']:<4} | نجاح: {s['success_pct']}%  | متوسط عائد/صفقة: {s['avg_pnl']}%")


def main():
    trades = load_all_closed_trades()
    print(f"إجمالي الصفقات المغلقة المقروءة: {len(trades)}")

    for trade_type, label in [("official", "رسمية (official)"), ("early", "مبكرة (early)")]:
        table = build_score_table(trades, trade_type)
        # دمج الدرجات ذات العينة الصغيرة جدًا مع أقرب درجة مجاورة لتفادي تصنيف غير موثوق
        small = {s: r for s, r in table.items() if r["count"] < MIN_SAMPLE_PER_SCORE}
        if small:
            print(f"\n⚠️ [{label}] درجات بعينة صغيرة جدًا (<{MIN_SAMPLE_PER_SCORE} صفقات) قد تُدمج تلقائيًا "
                  f"ضمن الحساب: {sorted(small.keys())}")
        summaries = split_into_3_ranges(table)
        summaries = label_by_success(summaries)
        print_report(label, summaries)

    print("\nملاحظة: هذا تقرير قراءة فقط، لم يتم تعديل أي بيانات أو ملفات بالـ Gist.")


if __name__ == "__main__":
    main()
