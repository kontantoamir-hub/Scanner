#!/usr/bin/env python3
"""
symbol_accuracy.py
-------------------
يقرأ closed_trades.json من نفس الـ Gist المستخدم بالبوت، ويحسب لكل عملة:
- عدد الصفقات المغلقة (رسمية + مبكرة + انفجار، حسب المتغير INCLUDE_TYPES)
- نسبة النجاح (Win Rate)
- متوسط العائد الصافي لكل صفقة (إن وُجد حقل عائد)
ثم يرتّب النتائج من الأفضل للأسوأ، ويعرض بشكل منفصل:
  1) العملات التي "دائمًا" تُحلَّل بشكل صحيح (Win Rate = 100%)
  2) العملات التي "دائمًا" يُخطئ فيها البوت (Win Rate = 0%)
  3) ترتيب كامل لكل العملات حسب نسبة النجاح

تشغيل يدوي فقط (لا يُدرج بجدولة GitHub Actions التلقائية)، بنفس نمط
analyze_trades.py / trade_stats.py / targets_report.py.

المتغيرات البيئية المطلوبة (نفس أسرار المستودع):
  GIST_TOKEN   - توكن GitHub بصلاحية gist
  GIST_ID      - معرّف الـ Gist المستخدم لتخزين closed_trades.json
  MIN_TRADES   - الحد الأدنى من الصفقات لاعتبار العملة ضمن "دائمًا صح/دائمًا خطأ" (افتراضي 3)
  INCLUDE_TYPES- أنواع الصفقات المشمولة، مفصولة بفاصلة (افتراضي: official,early,breakout)
"""

import os
import sys
import json
import requests
from collections import defaultdict

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")
MIN_TRADES = int(os.environ.get("MIN_TRADES", "3"))
INCLUDE_TYPES = set(
    t.strip() for t in os.environ.get("INCLUDE_TYPES", "official,early,breakout").split(",")
    if t.strip()
)
TRADES_FILENAME = "closed_trades.json"

# أنواع الإغلاق التي تُعتبر "نجاح" (وصلت لهدف واحد على الأقل / كل الأهداف)
WIN_CLOSED_REASONS = {"ALL_TP"}
# ملاحظة: لو عندك تعريف نجاح مختلف (مثلاً hit_tps > 0 كافٍ)، بدّل دالة is_win تحت.
LOSS_CLOSED_REASONS = {"SL"}


def fetch_closed_trades():
    if not GIST_TOKEN or not GIST_ID:
        print("خطأ: لازم تضبط GIST_TOKEN و GIST_ID كمتغيرات بيئة.")
        sys.exit(1)

    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {
        "Authorization": f"token {GIST_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    gist = resp.json()

    files = gist.get("files", {})
    if TRADES_FILENAME not in files:
        print(f"خطأ: الملف {TRADES_FILENAME} غير موجود بالـ Gist.")
        sys.exit(1)

    content = files[TRADES_FILENAME].get("content", "")
    # لو الملف كبير، GitHub أحيانًا يرجّع truncated=True مع raw_url لجلب المحتوى كاملًا
    if files[TRADES_FILENAME].get("truncated"):
        raw_url = files[TRADES_FILENAME]["raw_url"]
        raw_resp = requests.get(raw_url, headers=headers, timeout=30)
        raw_resp.raise_for_status()
        content = raw_resp.text

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        print("خطأ: تعذّر قراءة closed_trades.json كـ JSON صالح.")
        sys.exit(1)

    if isinstance(data, dict):
        # بعض التخزينات تحفظه كـ {"trades": [...]}
        data = data.get("trades", [])
    return data


def is_win(trade):
    reason = trade.get("closed_reason", "")
    if reason in WIN_CLOSED_REASONS:
        return True
    if reason in LOSS_CLOSED_REASONS:
        return False
    # fallback: لو ما فيه closed_reason واضح، استخدم hit_tps مقابل tps
    hit = trade.get("hit_tps")
    tps = trade.get("tps")
    if isinstance(hit, list) and isinstance(tps, list) and len(tps) > 0:
        return len(hit) >= len(tps)
    if isinstance(hit, int) and isinstance(tps, list):
        return hit >= len(tps) and len(tps) > 0
    return None  # غير محسوم، يُستبعد من الإحصاء


def main():
    trades = fetch_closed_trades()

    per_symbol = defaultdict(lambda: {"wins": 0, "losses": 0, "undecided": 0, "pnl_sum": 0.0, "pnl_count": 0})

    for t in trades:
        if INCLUDE_TYPES and t.get("type") not in INCLUDE_TYPES:
            continue
        symbol = t.get("symbol", "UNKNOWN")
        result = is_win(t)
        stats = per_symbol[symbol]

        if result is True:
            stats["wins"] += 1
        elif result is False:
            stats["losses"] += 1
        else:
            stats["undecided"] += 1

        # حقل العائد قد يكون باسم مختلف حسب النسخة، نجرّب عدة أسماء شائعة
        pnl = t.get("pnl_pct")
        if pnl is None:
            pnl = t.get("net_pnl_pct")
        if pnl is None:
            pnl = t.get("profit_pct")
        if isinstance(pnl, (int, float)):
            stats["pnl_sum"] += pnl
            stats["pnl_count"] += 1

    ranking = []
    for symbol, s in per_symbol.items():
        decided = s["wins"] + s["losses"]
        if decided == 0:
            continue
        win_rate = (s["wins"] / decided) * 100
        avg_pnl = (s["pnl_sum"] / s["pnl_count"]) if s["pnl_count"] > 0 else None
        ranking.append({
            "symbol": symbol,
            "total": decided,
            "wins": s["wins"],
            "losses": s["losses"],
            "win_rate": win_rate,
            "avg_pnl": avg_pnl,
        })

    # الترتيب الرئيسي: من الأعلى نجاحًا للأقل
    ranking.sort(key=lambda r: (r["win_rate"], r["total"]), reverse=True)

    always_right = [r for r in ranking if r["win_rate"] == 100.0 and r["total"] >= MIN_TRADES]
    always_wrong = [r for r in ranking if r["win_rate"] == 0.0 and r["total"] >= MIN_TRADES]

    def fmt_pnl(v):
        return f"{v:+.2f}%" if v is not None else "—"

    print("=" * 60)
    print(f"تحليل دقة البوت حسب العملة (الحد الأدنى للصفقات: {MIN_TRADES})")
    print(f"الأنواع المشمولة: {', '.join(sorted(INCLUDE_TYPES))}")
    print("=" * 60)

    print(f"\n✅ عملات ينجح البوت في تحليلها دائمًا ({len(always_right)} عملة):")
    if not always_right:
        print("  (لا توجد عملة وصلت لعتبة الحد الأدنى بنجاح 100%)")
    for r in always_right:
        print(f"  {r['symbol']:<12} | {r['total']} صفقة | نجاح 100% | متوسط عائد {fmt_pnl(r['avg_pnl'])}")

    print(f"\n❌ عملات يُخطئ البوت في تحليلها دائمًا ({len(always_wrong)} عملة):")
    if not always_wrong:
        print("  (لا توجد عملة وصلت لعتبة الحد الأدنى بفشل 100%)")
    for r in always_wrong:
        print(f"  {r['symbol']:<12} | {r['total']} صفقة | نجاح 0% | متوسط عائد {fmt_pnl(r['avg_pnl'])}")

    print(f"\n📊 الترتيب الكامل لكل العملات (من الأفضل للأسوأ):")
    print(f"  {'العملة':<12}{'صفقات':>8}{'نجاح':>6}{'خسارة':>8}{'نسبة':>9}{'متوسط عائد':>14}")
    for r in ranking:
        print(f"  {r['symbol']:<12}{r['total']:>8}{r['wins']:>6}{r['losses']:>8}{r['win_rate']:>8.1f}%{fmt_pnl(r['avg_pnl']):>14}")


if __name__ == "__main__":
    main()
