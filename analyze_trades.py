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
analyze_trades.py
==================
يحسب متوسط العائد الصافي الفعلي (pnl_pct) لكل فئة تشخيصية، منفصلة حسب نوع
الإشارة (رسمية/مبكرة) قبل أي استنتاج — لأن كل عامل تشخيصي له معنى مختلف
حسب النوع (مثلاً extended تُعاقب الدرجة الرسمية فتُفلتر أغلب حالاتها، بينما
لا تؤثر على أهلية الإشارة المبكرة، فتجميعها معًا يُنتج نتائج مضلِّلة).

تشغيل:
    export GIST_TOKEN=xxx GIST_ID=xxx
    python analyze_trades.py
"""


def avg_pnl(trades):
    vals = [pnl_pct(t) for t in trades if pnl_pct(t) is not None]
    return (sum(vals) / len(vals)) if vals else None


def analyze_type(ttype, trades):
    subset = [t for t in trades if t.get("type") == ttype and classify(t) != "neutral"]
    if not subset:
        print(f"\n(لا توجد صفقات {TYPE_LABELS.get(ttype, ttype)} مغلقة بعد)")
        return

    wins = [t for t in subset if classify(t) == "win"]
    total = len(subset)
    wr = len(wins) / total * 100
    ap = avg_pnl(subset)
    print(f"\n{'=' * 72}")
    print(f"— {TYPE_LABELS.get(ttype, ttype)} — إجمالي {total} صفقة | نجاح {wr:.1f}% | "
          f"متوسط عائد صافٍ: {ap:+.2f}%" if ap is not None else f"— {TYPE_LABELS.get(ttype, ttype)} —")
    print("=" * 72)

    print(f"\n{'العامل':<28}{'حاضر: عدد':<12}{'حاضر: متوسط عائد':<20}{'غائب: عدد':<12}{'غائب: متوسط عائد'}")
    print("-" * 96)
    for key in INDICATOR_KEYS:
        present = [t for t in subset if t.get(key) is True]
        absent = [t for t in subset if t.get(key) is not True]
        ap_p, ap_a = avg_pnl(present), avg_pnl(absent)
        ap_p_txt = f"{ap_p:+.2f}%" if ap_p is not None else "—"
        ap_a_txt = f"{ap_a:+.2f}%" if ap_a is not None else "—"
        print(f"{INDICATOR_LABELS[key]:<28}{len(present):<12}{ap_p_txt:<20}{len(absent):<12}{ap_a_txt}")

    # بدون أي مؤشر إضافي
    no_ind = [t for t in subset if not any(t.get(k) is True for k in INDICATOR_KEYS)]
    if no_ind:
        ap_n = avg_pnl(no_ind)
        ap_n_txt = f"{ap_n:+.2f}%" if ap_n is not None else "—"
        print(f"{'بدون مؤشر إضافي':<28}{len(no_ind):<12}{ap_n_txt}")

    # تقاطعات شائعة (كل مؤشرين معًا) — فقط لو عينة كل تقاطع لا تقل عن 3 صفقات
    print("\n— تقاطعات (مؤشرين معًا) بعينة ≥3 صفقات —")
    for i, k1 in enumerate(INDICATOR_KEYS):
        for k2 in INDICATOR_KEYS[i + 1:]:
            combo = [t for t in subset if t.get(k1) is True and t.get(k2) is True]
            if len(combo) >= 3:
                ap_c = avg_pnl(combo)
                w = sum(1 for t in combo if classify(t) == "win")
                print(f"  {INDICATOR_LABELS[k1]} + {INDICATOR_LABELS[k2]}: {len(combo)} صفقة | "
                      f"نجاح {pct(w, len(combo)):.0f}% | متوسط عائد: {ap_c:+.2f}%")


def main():
    trades = load_trades()
    print("📊 تحليل متوسط العائد الصافي لكل فئة تشخيصية (مقسّم حسب النوع)")
    for ttype in ["official", "early"]:
        analyze_type(ttype, trades)
    print("\n" + "=" * 72)
    print("ملاحظة: لا تُتخذ قرارات فلترة إلا بعد عينة كافية (30+ صفقة) لكل عامل داخل كل نوع.")
    print("=" * 72)


if __name__ == "__main__":
    main()
