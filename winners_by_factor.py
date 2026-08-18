"""
winners_by_factor.py
----------------------
لكل مؤشر تشخيصي (accumulation / squeeze / divergence / extended)، من بين
الصفقات المغلقة الرابحة فقط (نفس تعريف outcome_of في score_breakdown.py):
  - عدد الصفقات التي حققت ربحًا ≥ 1% (نسبة (سعر الخروج - الدخول) / الدخول × 100)
  - مجموع كل نسب الربح لكل الصفقات الرابحة (بغض النظر عن حد الـ1%)
  - قائمة بأسماء العملات ونسبة ربح كل واحدة (للصفقات ≥1%)

يشمل صفقات النوعين (رسمية/مبكرة) معًا لأن الحقول التشخيصية موجودة في الاثنين.

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
MIN_PROFIT_PCT = 1.0  # الحد الأدنى لنسبة الربح المطلوب عدّها ضمن "ناجحة بوضوح"


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


def pnl_pct_of(trade):
    """نسبة الربح/الخسارة: (سعر الخروج - الدخول) / الدخول × 100."""
    entry, exit_price = trade.get("entry"), trade.get("exit_price")
    if not entry or not exit_price:
        return None
    return (exit_price - entry) / entry * 100


def analyze_factor(trades, factor):
    """
    يرجع: (عدد الرابحة الكلي, عدد الرابحة بربح>=1%, مجموع كل نسب ربح الرابحة,
    قائمة (رمز, نسبة) للصفقات >=1% مرتبة تنازليًا)
    """
    winners = [t for t in trades if t.get(factor) is True and outcome_of(t) == "win"]

    total_win = len(winners)
    total_pct_sum = 0.0
    qualifying = []  # (symbol, pnl_pct) لصفقات >= 1%

    for t in winners:
        pct = pnl_pct_of(t)
        if pct is None:
            continue
        total_pct_sum += pct
        if pct >= MIN_PROFIT_PCT:
            qualifying.append((t.get("symbol", "?"), pct))

    qualifying.sort(key=lambda x: x[1], reverse=True)
    return total_win, len(qualifying), total_pct_sum, qualifying


def print_factor_report(label, total_win, qualifying_count, total_pct_sum):
    print(f"\n{label}")
    print("-" * len(label))
    print(f"  إجمالي الصفقات الرابحة: {total_win}")
    print(f"  رابحة بنسبة ≥ {MIN_PROFIT_PCT:.0f}%: {qualifying_count}")
    print(f"  مجموع كل نسب الربح (كل الرابحة): {total_pct_sum:+.2f}%")


def print_qualifying_list(qualifying):
    if not qualifying:
        print("  (لا توجد صفقات برح ≥ الحد الأدنى)")
        return
    for symbol, pct in qualifying:
        print(f"    {symbol:<15} {pct:+.2f}%")


def main():
    trades = load_closed()
    if not trades:
        print("لا توجد صفقات مغلقة بعد.")
        return

    print(f"إجمالي الصفقات المغلقة: {len(trades)}")

    for factor in FACTORS:
        label = FACTOR_LABELS[factor]
        total_win, qualifying_count, total_pct_sum, qualifying = analyze_factor(trades, factor)
        print_factor_report(label, total_win, qualifying_count, total_pct_sum)
        print_qualifying_list(qualifying)


if __name__ == "__main__":
    main()