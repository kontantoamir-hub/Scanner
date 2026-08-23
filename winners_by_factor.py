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
winners_by_factor.py
=====================
يحلل الصفقات الرابحة فقط (win)، مقسّمة حسب كل عامل تشخيصي حاضر وقت الفتح:
عدد الصفقات الرابحة، مجموع نسب الربح، متوسط الربح لكل صفقة، ونسبة التأهل
لربح ≥1% (عتبة MIN_PROFIT_PCT المعتمدة في scanner.py). يساعد على مقارنة
"جودة" كل مصدر إشارة من بين الصفقات الناجحة فقط (بمعزل عن نسبة النجاح
الإجمالية التي يغطيها analyze_trades.py/trade_stats.py).

تشغيل:
    export GIST_TOKEN=xxx GIST_ID=xxx
    python winners_by_factor.py
"""

QUALIFY_THRESHOLD_PCT = 1.0


def analyze_factor(wins, key):
    group = [t for t in wins if t.get(key) is True]
    if not group:
        return None
    pnls = [pnl_pct(t) for t in group if pnl_pct(t) is not None]
    if not pnls:
        return None
    total_pnl = sum(pnls)
    avg = total_pnl / len(pnls)
    qualified = sum(1 for p in pnls if p >= QUALIFY_THRESHOLD_PCT)
    qualify_rate = pct(qualified, len(pnls))
    return {
        "count": len(group),
        "total_pnl": total_pnl,
        "avg_pnl": avg,
        "qualify_rate": qualify_rate,
    }


def main():
    trades = load_trades()
    wins = [t for t in trades if t.get("type") in ("official", "early") and classify(t) == "win"]
    if not wins:
        sys.exit("لا توجد صفقات رابحة (رسمية/مبكرة) بعد.")

    print(f"📊 تحليل الصفقات الرابحة فقط حسب المصدر التشخيصي — إجمالي {len(wins)} صفقة رابحة")
    print(f"\n{'العامل':<28}{'عدد الرابحة':<14}{'مجموع الربح':<16}{'متوسط/صفقة':<16}{'تأهل ≥1%'}")
    print("-" * 96)

    rows = []
    for key in INDICATOR_KEYS:
        r = analyze_factor(wins, key)
        if r:
            rows.append((key, r))

    rows.sort(key=lambda x: -x[1]["avg_pnl"])
    for key, r in rows:
        print(f"{INDICATOR_LABELS[key]:<28}{r['count']:<14}{r['total_pnl']:+.2f}%"
              f"{'':<8}{r['avg_pnl']:+.2f}%{'':<10}{r['qualify_rate']:.1f}%")

    # بدون أي مؤشر إضافي
    no_ind = [t for t in wins if not any(t.get(k) is True for k in INDICATOR_KEYS)]
    pnls = [pnl_pct(t) for t in no_ind if pnl_pct(t) is not None]
    if pnls:
        avg = sum(pnls) / len(pnls)
        qualify_rate = pct(sum(1 for p in pnls if p >= QUALIFY_THRESHOLD_PCT), len(pnls))
        print(f"{'بدون مؤشر إضافي':<28}{len(pnls):<14}{sum(pnls):+.2f}%{'':<8}{avg:+.2f}%{'':<10}{qualify_rate:.1f}%")

    print("\n" + "=" * 72)
    print("ملاحظة: احذر من هيمنة صفقة استثنائية واحدة على مجموع الربح لأي عامل بعينة")
    print("صغيرة — راجع 'متوسط/صفقة' لا 'مجموع الربح' وحده عند المقارنة بين العوامل.")
    print("=" * 72)


if __name__ == "__main__":
    main()
