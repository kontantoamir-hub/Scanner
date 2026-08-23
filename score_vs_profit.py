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
score_vs_profit.py
===================
يعرض لكل صفقة رسمية/مبكرة مغلقة: الرمز، الدرجة، والعائد الفعلي الحقيقي
(نسبة تغيّر سعر الدخول/الخروج pnl_pct) — بدل الاكتفاء بمعرفة هل وصلت لكل
الأهداف أو لا (targets_report.py). مرتّب من الأسوأ للأفضل عائدًا لتسهيل رصد
حالات "درجة عالية لكن عائد سلبي" (مشكلة شراء القمة) بسرعة.

تشغيل:
    export GIST_TOKEN=xxx GIST_ID=xxx
    python score_vs_profit.py
"""


def main():
    trades = load_trades()
    subset = [t for t in trades if t.get("type") in ("official", "early") and classify(t) != "neutral"]
    if not subset:
        sys.exit("لا توجد صفقات رسمية/مبكرة مغلقة بعد.")

    rows = []
    for t in subset:
        p = pnl_pct(t)
        if p is None:
            continue
        rows.append({
            "symbol": t.get("symbol", "?"),
            "type": TYPE_LABELS.get(t.get("type"), t.get("type")),
            "score": t.get("score"),
            "pnl": p,
            "outcome": "✅ ربح" if classify(t) == "win" else "❌ خسارة",
        })

    rows.sort(key=lambda r: r["pnl"])

    print(f"📊 الدرجة مقابل العائد الفعلي — {len(rows)} صفقة (مرتّبة من الأسوأ للأفضل)")
    print(f"\n{'الرمز':<14}{'النوع':<10}{'الدرجة':<10}{'العائد الفعلي':<16}{'النتيجة'}")
    print("-" * 70)
    for r in rows:
        score_txt = f"{r['score']:.2f}" if r["score"] is not None else "—"
        print(f"{r['symbol']:<14}{r['type']:<10}{score_txt:<10}{r['pnl']:+.2f}%{'':<8}{r['outcome']}")

    # إحصاء سريع: هل الدرجات العالية فعليًا أعلى عائدًا؟ (يفترض أن تكون العلاقة طردية)
    pos_score = [r["pnl"] for r in rows if r["score"] is not None and r["score"] >= 2.5]
    low_score = [r["pnl"] for r in rows if r["score"] is not None and r["score"] < 2.5]
    print("\n" + "=" * 72)
    if pos_score:
        print(f"متوسط العائد للدرجات ≥2.5: {sum(pos_score) / len(pos_score):+.2f}% ({len(pos_score)} صفقة)")
    if low_score:
        print(f"متوسط العائد للدرجات <2.5: {sum(low_score) / len(low_score):+.2f}% ({len(low_score)} صفقة)")
    print("=" * 72)


if __name__ == "__main__":
    main()
