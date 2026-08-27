#!/usr/bin/env python3
"""
symbol_factors_report.py
--------------------------
يقرأ closed_trades.json من الـ Gist، ويعرض لمجموعتين من العملات
(عملات ناجحة دائمًا / عملات فاشلة دائمًا حسب symbol_accuracy.py):
  1) تفصيل كل صفقة على حدة مع كل الحقول التشخيصية (لمعرفة "شو رأى" البوت وقت الدخول)
  2) مقارنة مجمّعة: نسبة حضور كل عامل تشخيصي (score, htf_aligned, rsi_state,
     macd_bull, bb_state, vol_confirm, ranging, near_resistance, obv_confirm,
     accumulation, squeeze, extended, divergence, market_regime) بين
     مجموعة العملات الناجحة ومجموعة الفاشلة

تشغيل يدوي فقط.

المتغيرات البيئية:
  GIST_TOKEN     - نفس توكن الـ Gist
  GIST_ID        - نفس معرّف الـ Gist
  WINNING_SYMBOLS- عملات المجموعة الناجحة، مفصولة بفاصلة (مثال: HEIUSDT,BONKUSDT)
  LOSING_SYMBOLS - عملات المجموعة الفاشلة، مفصولة بفاصلة (مثال: ZROUSDT,ASTERUSDT)
"""

import os
import sys
import json
import requests
from collections import defaultdict

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")
TRADES_FILENAME = "closed_trades.json"

WINNING_SYMBOLS = [s.strip().upper() for s in os.environ.get("WINNING_SYMBOLS", "HEIUSDT,BONKUSDT").split(",") if s.strip()]
LOSING_SYMBOLS = [s.strip().upper() for s in os.environ.get("LOSING_SYMBOLS", "ZROUSDT,ASTERUSDT").split(",") if s.strip()]

# الحقول التشخيصية التي نقارنها (بولياني/نصي بحسب scanner.py)
DIAG_FIELDS = [
    "rsi_state", "macd_bull", "bb_state", "vol_confirm", "ranging",
    "near_resistance", "obv_confirm", "htf_aligned",
    "accumulation", "squeeze", "extended", "divergence",
    "market_regime", "source",
]


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
        data = data.get("trades", [])
    return data


def is_win(trade):
    reason = trade.get("closed_reason", "")
    if reason == "ALL_TP":
        return True
    if reason == "SL":
        return False
    hit = trade.get("hit_tps")
    tps = trade.get("tps")
    if isinstance(hit, list) and isinstance(tps, list) and len(tps) > 0:
        return len(hit) >= len(tps)
    if isinstance(hit, int) and isinstance(tps, list):
        return hit >= len(tps) and len(tps) > 0
    return None


def print_trade_detail(t):
    symbol = t.get("symbol", "?")
    ttype = t.get("type", "?")
    score = t.get("score", "?")
    reason = t.get("closed_reason", "?")
    result = is_win(t)
    result_label = "✅ ربح" if result is True else ("❌ خسارة" if result is False else "؟ غير محسوم")

    line = f"  [{symbol}] نوع={ttype} | درجة={score} | إغلاق={reason} | {result_label}"
    diag_parts = []
    for f in DIAG_FIELDS:
        if f in t and t[f] not in (None, ""):
            diag_parts.append(f"{f}={t[f]}")
    if diag_parts:
        line += "\n      " + " | ".join(diag_parts)
    print(line)


def aggregate_group(trades, symbols):
    group_trades = [t for t in trades if t.get("symbol", "").upper() in symbols]
    counts = defaultdict(lambda: defaultdict(int))
    total = len(group_trades)
    for t in group_trades:
        for f in DIAG_FIELDS:
            val = t.get(f)
            if val is None or val == "":
                continue
            counts[f][str(val)] += 1
    return group_trades, counts, total


def print_aggregate(name, counts, total):
    print(f"\n--- تجميع المجموعة: {name} (إجمالي {total} صفقة) ---")
    if total == 0:
        print("  لا توجد صفقات لهذه المجموعة.")
        return
    for field in DIAG_FIELDS:
        if field not in counts:
            continue
        values = counts[field]
        parts = [f"{v}={c} ({c/total*100:.0f}%)" for v, c in sorted(values.items(), key=lambda x: -x[1])]
        print(f"  {field:<16}: " + ", ".join(parts))


def main():
    trades = fetch_closed_trades()

    print("=" * 70)
    print("تفاصيل صفقات العملات الناجحة دائمًا:", ", ".join(WINNING_SYMBOLS))
    print("=" * 70)
    winning_trades, winning_counts, winning_total = aggregate_group(trades, WINNING_SYMBOLS)
    for t in winning_trades:
        print_trade_detail(t)

    print("\n" + "=" * 70)
    print("تفاصيل صفقات العملات الفاشلة دائمًا:", ", ".join(LOSING_SYMBOLS))
    print("=" * 70)
    losing_trades, losing_counts, losing_total = aggregate_group(trades, LOSING_SYMBOLS)
    for t in losing_trades:
        print_trade_detail(t)

    print("\n" + "=" * 70)
    print("مقارنة مجمّعة بين المجموعتين")
    print("=" * 70)
    print_aggregate("الناجحة", winning_counts, winning_total)
    print_aggregate("الفاشلة", losing_counts, losing_total)


if __name__ == "__main__":
    main()
