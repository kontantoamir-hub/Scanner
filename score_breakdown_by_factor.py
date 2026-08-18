"""
score_breakdown_by_factor.py
------------------------------
امتداد لـ score_breakdown.py: يتحقق هل العلاقة العكسية بين الدرجة الرسمية
ونسبة نجاح الإشارة المبكرة (كل ما انخفضت الدرجة زاد النجاح) موجودة بشكل
مستقل داخل كل مؤشر تشخيصي لوحده (accumulation/squeeze)، أو إنها ناتجة فقط
عن اختلاط المؤشرين مع بعض بالعينة الكلية.

لكل مؤشر من الأربعة (squeeze, accumulation, divergence, extended)، يقسّم
صفقات النوع "مبكرة" فقط إلى حاضر/غائب، ويطبع جدول درجة/نجاح لكل قسم.

التشغيل: يدوي فقط، يحتاج GIST_TOKEN وGIST_ID كمتغيرات بيئة.
"""

import os
import json
import requests

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")
CLOSED_GIST_FILE = "closed_trades.json"

FACTORS = ["squeeze", "accumulation", "divergence", "extended"]
FACTOR_LABELS = {
    "squeeze": "انضغاط تقلب (Squeeze)",
    "accumulation": "تراكم صامت (Accumulation)",
    "divergence": "انحراف صعودي (Divergence)",
    "extended": "امتداد زائد (Extended)",
}


def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def load_closed():
    if not GIST_TOKEN or not GIST_ID:
        raise SystemExit("⚠️ GIST_TOKEN أو GIST_ID غير موجودين كمتغيرات بيئة.")
    r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=_gist_headers(), timeout=15)
    r.raise_for_status()
    files = r.json().get("files", {})
    if CLOSED_GIST_FILE not in files:
        raise SystemExit(f"⚠️ الملف {CLOSED_GIST_FILE} غير موجود في الـ Gist.")
    content = files[CLOSED_GIST_FILE]["content"]
    try:
        return json.loads(content)
    except Exception as e:
        raise SystemExit(f"تعذّر تحليل {CLOSED_GIST_FILE}: {e}")


def outcome_of(trade):
    """نفس منطق compute_stats في scanner.py."""
    reason = trade.get("closed_reason", "UNKNOWN")
    hit = len(trade.get("hit_tps") or [])
    if reason == "ALL_TP" or hit > 0:
        return "win"
    if reason == "SL" and hit == 0:
        return "loss"
    return "neutral"


def score_key(trade):
    """تقريب الدرجة لأقرب 0.5، نفس score_breakdown.py."""
    score = trade.get("score")
    if score is None:
        return None
    return round(score * 2) / 2


def build_breakdown(trades):
    breakdown = {}
    for t in trades:
        key = score_key(t)
        if key is None:
            continue
        b = breakdown.setdefault(key, {"total": 0, "win": 0, "loss": 0, "neutral": 0})
        b["total"] += 1
        b[outcome_of(t)] += 1
    return breakdown


def print_table(title, breakdown):
    print(f"\n{title}")
    print("-" * len(title))
    if not breakdown:
        print("لا توجد بيانات.")
        return

    header = f"{'الدرجة':>8} | {'عدد الصفقات':>12} | {'رابحة':>7} | {'خاسرة':>7} | {'محايدة':>7} | {'نسبة النجاح':>12}"
    print(header)
    print("-" * len(header))
    for score in sorted(breakdown.keys(), reverse=True):
        b = breakdown[score]
        decided = b["win"] + b["loss"]
        win_rate = f"{b['win'] / decided * 100:.1f}%" if decided else "—"
        print(f"{score:>8} | {b['total']:>12} | {b['win']:>7} | {b['loss']:>7} | {b['neutral']:>7} | {win_rate:>12}")


def main():
    trades = load_closed()
    if not trades:
        print("لا توجد صفقات مغلقة بعد.")
        return

    early_trades = [t for t in trades if t.get("type") == "early"]
    print(f"إجمالي الصفقات المبكرة المغلقة: {len(early_trades)}")

    for factor in FACTORS:
        label = FACTOR_LABELS[factor]
        present = [t for t in early_trades if t.get(factor) is True]
        absent = [t for t in early_trades if t.get(factor) is False]

        print(f"\n{'='*70}")
        print(f"المؤشر: {label}")
        print(f"{'='*70}")

        print_table(f"— حاضر ({len(present)} صفقة)", build_breakdown(present))
        print_table(f"— غائب ({len(absent)} صفقة)", build_breakdown(absent))


if __name__ == "__main__":
    main()