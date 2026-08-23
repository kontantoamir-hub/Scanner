import os
import sys
import json
import datetime as dt
from collections import defaultdict

import requests

GIST_FILENAME = "closed_trades.json"          # السجل النشط: أحدث الصفقات فقط
ARCHIVE_PREFIX = "closed_trades_archive_"     # ملفات الأرشيف المرقّمة (تحوي كل التاريخ الأقدم)

INDICATOR_KEYS = ["squeeze", "accumulation", "divergence", "extended"]
INDICATOR_LABELS = {
    "squeeze": "انضغاط تقلب (Squeeze)",
    "accumulation": "تراكم صامت (Accumulation)",
    "divergence": "دايفرجنس (Divergence)",
    "extended": "امتداد زائد (Overextension)",
}
TYPE_LABELS = {"official": "رسمية", "early": "مبكرة", "breakout": "انفجار"}


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
    return json.loads(content)


def load_trades():
    """يجمع السجل النشط + كل ملفات الأرشيف المرقّمة = التاريخ الكامل بلا أي سقف.
    يدعم أيضًا وجود closed_trades.json محلي بجانب السكربت للتشغيل بدون شبكة."""
    local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "closed_trades.json")
    if os.path.exists(local_path):
        with open(local_path, "r", encoding="utf-8") as f:
            return json.load(f)

    token = os.environ.get("GIST_TOKEN")
    gist_id = os.environ.get("GIST_ID")
    if not token or not gist_id:
        sys.exit("خطأ: لا يوجد closed_trades.json محليًا، ولم يتم ضبط GIST_TOKEN/GIST_ID.")

    r = requests.get(
        f"https://api.github.com/gists/{gist_id}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
        timeout=15,
    )
    r.raise_for_status()
    files = r.json().get("files", {})

    all_trades = []
    archive_names = sorted(fn for fn in files if fn.startswith(ARCHIVE_PREFIX))
    for name in archive_names:
        all_trades.extend(_read_json_file(files[name], name))
    if GIST_FILENAME in files:
        all_trades.extend(_read_json_file(files[GIST_FILENAME], GIST_FILENAME))

    if not all_trades and GIST_FILENAME not in files:
        sys.exit(f"لم يتم العثور على '{GIST_FILENAME}' داخل الـ Gist. الملفات المتوفرة: {list(files.keys())}")

    return all_trades


def classify(trade):
    """win / loss / neutral — نفس منطق التصنيف المعتمد في trade_stats.py.
    "neutral" (INVALIDATED/EXPIRED بدون أي هدف محقق) تُستبعد من كل التحليلات."""
    reason = trade.get("closed_reason", "UNKNOWN")
    hit = len(trade.get("hit_tps") or [])
    if reason == "ALL_TP" or hit > 0:
        return "win"
    if reason == "SL" and hit == 0:
        return "loss"
    return "neutral"


def pnl_pct(trade):
    entry, exit_price = trade.get("entry"), trade.get("exit_price")
    if entry and exit_price:
        return (exit_price - entry) / entry * 100
    return None


def pct(part, whole):
    return (part / whole * 100) if whole else 0.0


"""
score_breakdown.py
===================
يجمّع الصفقات حسب الدرجة (score) مقرّبة لأقرب 0.5، ويعرض نسبة النجاح لكل
فئة درجة — منفصلة حسب النوع (رسمية/مبكرة)، لأن العلاقة بين الدرجة والنجاح
معكوسة أحيانًا بين النوعين (راجع official_success_factors.py لمشكلة
"شراء القمة" بالدرجات العالية للرسمية).

تشغيل:
    export GIST_TOKEN=xxx GIST_ID=xxx
    python score_breakdown.py
"""


def round_to_half(score):
    if score is None:
        return None
    return round(score * 2) / 2


def analyze_type(ttype, trades):
    subset = [t for t in trades if t.get("type") == ttype and classify(t) != "neutral"]
    if not subset:
        print(f"\n(لا توجد صفقات {TYPE_LABELS.get(ttype, ttype)} مغلقة بعد)")
        return

    buckets = defaultdict(lambda: {"total": 0, "win": 0})
    for t in subset:
        b = round_to_half(t.get("score"))
        if b is None:
            continue
        buckets[b]["total"] += 1
        if classify(t) == "win":
            buckets[b]["win"] += 1

    print(f"\n{'=' * 72}")
    print(f"— {TYPE_LABELS.get(ttype, ttype)} — تجميع حسب الدرجة (أقرب 0.5) —")
    print("=" * 72)
    print(f"\n{'الدرجة':<12}{'عدد الصفقات':<16}{'نسبة النجاح'}")
    print("-" * 50)
    for b in sorted(buckets.keys()):
        s = buckets[b]
        wr = pct(s["win"], s["total"])
        print(f"{b:<12}{s['total']:<16}{wr:.1f}% ({s['win']}/{s['total']})")


def main():
    trades = load_trades()
    print("📊 تحليل توزيع النجاح حسب الدرجة (score)")
    for ttype in ["official", "early"]:
        analyze_type(ttype, trades)
    print("\n" + "=" * 72)
    print("ملاحظة: فئات الدرجة ذات العينة الصغيرة (<10 صفقة) غير موثوقة إحصائيًا بعد.")
    print("=" * 72)


if __name__ == "__main__":
    main()
