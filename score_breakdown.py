"""
score_breakdown.py
-------------------
يقرأ سجل الصفقات المغلقة (closed_trades.json) من نفس الـ Gist المستخدم في scanner.py،
ويرتب الصفقات حسب الدرجة (score): لكل درجة -> عدد الصفقات، عدد الرابحة/الخاسرة،
ونسبة النجاح. يعرض جدولاً إجمالياً، ثم تفصيلاً منفصلاً لكل نوع (رسمية/مبكرة) لأن
الفجوة بين النوعين كبيرة (انظر analyze_trades.py / trade_stats.py).

تعريف النجاح/الخسارة (نفس منطق compute_stats في scanner.py):
  - رابحة: closed_reason == "ALL_TP" أو تحقق هدف واحد على الأقل (hit_tps غير فارغة)
  - خاسرة: closed_reason == "SL" ولم يتحقق أي هدف (hit_tps فارغة)
  - غير ذلك (EXPIRED بدون أي هدف): محايدة -> تُستبعد من نسبة النجاح (نفس قرارك بإزالة
    تصنيف "محايدة" من التقارير)، لكن تُذكر في العدّاد الإجمالي لكل درجة.

التشغيل: يدوي فقط (لا يُجدوَل)، يحتاج GIST_TOKEN وGIST_ID كمتغيرات بيئة.
"""

import os
import json
import requests

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")
CLOSED_GIST_FILE = "closed_trades.json"


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
    """يرجع 'win' / 'loss' / 'neutral' بنفس منطق compute_stats في scanner.py."""
    reason = trade.get("closed_reason", "UNKNOWN")
    hit = len(trade.get("hit_tps") or [])
    if reason == "ALL_TP" or hit > 0:
        return "win"
    if reason == "SL" and hit == 0:
        return "loss"
    return "neutral"


def score_key(trade):
    """
    يقرّب الدرجة لأقرب نصف نقطة (0.5) لأن final_score يتضمن تعديل توافق الفريم
    الأعلى (±0.5)، فلولا التقريب لظهرت درجات مثل 3.5/2.5/4.5 كفئات منفصلة كثيرة
    ومشتتة. يرجع None لو لا توجد درجة.
    """
    score = trade.get("score")
    if score is None:
        return None
    return round(score * 2) / 2


def build_breakdown(trades):
    """
    يبني قاموسًا: {score: {"total": n, "win": n, "loss": n, "neutral": n}}
    """
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

    # ترتيب تنازلي حسب الدرجة (أعلى درجة أولاً)
    for score in sorted(breakdown.keys(), reverse=True):
        b = breakdown[score]
        decided = b["win"] + b["loss"]  # نسبة النجاح مبنية على الصفقات المحسومة فقط (بدون المحايدة)
        win_rate = f"{b['win'] / decided * 100:.1f}%" if decided else "—"
        print(f"{score:>8} | {b['total']:>12} | {b['win']:>7} | {b['loss']:>7} | {b['neutral']:>7} | {win_rate:>12}")


def main():
    trades = load_closed()
    if not trades:
        print("لا توجد صفقات مغلقة بعد.")
        return

    print(f"إجمالي الصفقات المغلقة: {len(trades)}")

    # 1) جدول إجمالي (كل الأنواع مع بعض)
    print_table("ترتيب الدرجات حسب نسبة النجاح — الإجمالي", build_breakdown(trades))

    # 2) تفصيل منفصل حسب النوع، لأن الفجوة رسمية/مبكرة كبيرة (تشويه لو دُمجوا)
    types_present = sorted({t.get("type", "official") for t in trades})
    type_labels = {"official": "رسمية", "early": "مبكرة", "breakout": "انفجار"}
    for ttype in types_present:
        subset = [t for t in trades if t.get("type", "official") == ttype]
        label = type_labels.get(ttype, ttype)
        print_table(f"ترتيب الدرجات حسب نسبة النجاح — نوع: {label} ({len(subset)} صفقة)", build_breakdown(subset))


if __name__ == "__main__":
    main()
