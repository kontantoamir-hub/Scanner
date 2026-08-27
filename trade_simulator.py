#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
محاكي أرباح البوت (Trade Simulator)
------------------------------------
يحسب: لو دخلت بمبلغ ثابت (مثلاً 50$) في كل صفقة أغلقها البوت خلال آخر N يوم،
كم كانت النتيجة الإجمالية (ربح/خسارة)، مع سرد الصفقات الرابحة والخاسرة.

البيانات تُسحب تلقائيًا من ملف closed_trades.json داخل الـ Gist بتاعك.

الاستخدام:
    python trade_simulator.py --days 10 --amount 50

الإعداد المطلوب مرة واحدة فقط:
    عدّل GIST_RAW_URL بالأسفل ليشير إلى الرابط الخام (Raw) لملف closed_trades.json
    في الـ Gist بتاعك. مثال على شكل الرابط:
    https://gist.githubusercontent.com/<username>/<gist_id>/raw/closed_trades.json

    ملاحظة: روابط raw.githubusercontent.com للـ Gist أحيانًا تُخزَّن مؤقتًا (cache).
    إذا لاحظت أن البيانات قديمة، استخدم رابط الـ API بدلاً منه (انظر التعليق تحت المتغير).
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen
from urllib.error import URLError

# =========================================================
# إعداد: ضع هنا رابط الـ Gist الخام لملف closed_trades.json
# =========================================================
GIST_RAW_URL = "PASTE_YOUR_GIST_RAW_URL_HERE"

# بديل أدق (يتجاوز الكاش): استخدم الـ Gist ID عبر GitHub API
# GIST_API_URL = "https://api.github.com/gists/<GIST_ID>"
# GIST_FILENAME = "closed_trades.json"


def fetch_trades():
    """يجلب قائمة الصفقات المغلقة من الـ Gist."""
    if "PASTE_YOUR_GIST_RAW_URL_HERE" in GIST_RAW_URL:
        print("⚠️  لم تقم بضبط GIST_RAW_URL في أعلى الملف بعد.")
        print("افتح trade_simulator.py وضع رابط raw لملف closed_trades.json من الـ Gist بتاعك.")
        sys.exit(1)
    try:
        with urlopen(GIST_RAW_URL, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except URLError as e:
        print(f"❌ فشل الاتصال بالـ Gist: {e}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"❌ الملف الناتج ليس JSON صالح: {e}")
        sys.exit(1)

    # قد يكون الملف قائمة مباشرة أو dict فيه مفتاح trades
    if isinstance(data, dict):
        for key in ("trades", "closed_trades", "data"):
            if key in data:
                return data[key]
        # لو dict لكن بدون مفتاح معروف، افترض أن القيم نفسها الصفقات
        return list(data.values())
    return data


def parse_close_time(trade):
    """يحاول استخراج تاريخ إغلاق الصفقة من أسماء حقول مختلفة محتملة."""
    for key in ("closed_at", "close_time", "closedAt", "close_date", "timestamp"):
        if key in trade and trade[key]:
            raw = trade[key]
            try:
                if isinstance(raw, (int, float)):
                    return datetime.fromtimestamp(raw, tz=timezone.utc)
                # يدعم صيغ ISO المختلفة بما فيها Z
                return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
    return None


def get_pnl_pct(trade):
    """يحاول استخراج نسبة الربح/الخسارة من أسماء حقول مختلفة محتملة."""
    for key in ("pnl_pct", "profit_pct", "pnl_percent", "change_pct", "result_pct"):
        if key in trade and trade[key] is not None:
            try:
                return float(trade[key])
            except (ValueError, TypeError):
                continue
    # كحل أخير: احسبها من سعر الدخول والخروج لو متوفرين
    entry = trade.get("entry_price") or trade.get("entry")
    exitp = trade.get("exit_price") or trade.get("close_price")
    if entry and exitp:
        try:
            return (float(exitp) - float(entry)) / float(entry) * 100
        except (ValueError, TypeError, ZeroDivisionError):
            return None
    return None


def get_symbol(trade):
    return trade.get("symbol") or trade.get("pair") or trade.get("coin") or "غير معروف"


def get_reason(trade):
    return trade.get("close_reason") or trade.get("reason") or trade.get("status") or "-"


def main():
    parser = argparse.ArgumentParser(description="محاكي أرباح البوت خلال فترة معينة")
    parser.add_argument("--days", type=int, required=True, help="عدد الأيام الماضية للحساب (مثال: 10)")
    parser.add_argument("--amount", type=float, required=True, help="المبلغ الثابت بالدولار لكل صفقة (مثال: 50)")
    args = parser.parse_args()

    trades = fetch_trades()
    if not trades:
        print("لا توجد صفقات مغلقة في الملف.")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    winners = []
    losers = []
    skipped = 0

    for t in trades:
        close_time = parse_close_time(t)
        if close_time is None:
            skipped += 1
            continue
        if close_time.tzinfo is None:
            close_time = close_time.replace(tzinfo=timezone.utc)
        if close_time < cutoff:
            continue

        pnl_pct = get_pnl_pct(t)
        if pnl_pct is None:
            skipped += 1
            continue

        profit_usd = args.amount * (pnl_pct / 100)
        entry = {
            "symbol": get_symbol(t),
            "pnl_pct": pnl_pct,
            "profit_usd": profit_usd,
            "reason": get_reason(t),
            "close_time": close_time,
        }
        if pnl_pct >= 0:
            winners.append(entry)
        else:
            losers.append(entry)

    total_trades = len(winners) + len(losers)
    total_profit = sum(w["profit_usd"] for w in winners) + sum(l["profit_usd"] for l in losers)
    win_rate = (len(winners) / total_trades * 100) if total_trades else 0
    invested = total_trades * args.amount

    print("=" * 50)
    print(f"📊 نتيجة محاكاة دخول {args.amount:.2f}$ في كل صفقة خلال آخر {args.days} يوم")
    print("=" * 50)

    if winners:
        print(f"\n✅ الصفقات الرابحة ({len(winners)}):")
        for w in sorted(winners, key=lambda x: x["pnl_pct"], reverse=True):
            print(f"  {w['symbol']:<12} +{w['pnl_pct']:.2f}%  →  +{w['profit_usd']:.2f}$   ({w['reason']})")

    if losers:
        print(f"\n❌ الصفقات الخاسرة ({len(losers)}):")
        for l in sorted(losers, key=lambda x: x["pnl_pct"]):
            print(f"  {l['symbol']:<12} {l['pnl_pct']:.2f}%  →  {l['profit_usd']:.2f}$   ({l['reason']})")

    print("\n" + "-" * 50)
    print(f"عدد الصفقات المحسوبة: {total_trades}  (تم تجاهل {skipped} بسبب بيانات ناقصة)")
    print(f"نسبة الربح (Win Rate): {win_rate:.1f}%")
    print(f"إجمالي رأس المال المفترض دخوله: {invested:.2f}$")
    print(f"النتيجة الصافية: {'+' if total_profit >= 0 else ''}{total_profit:.2f}$")
    if invested:
        print(f"نسبة العائد على رأس المال: {(total_profit/invested*100):+.2f}%")
    print("=" * 50)


if __name__ == "__main__":
    main()
