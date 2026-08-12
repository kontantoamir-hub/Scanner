"""
تقرير أداء دوري (افتراضيًا كل 15 يومًا) — يقرأ سجل الصفقات المغلقة (closed_trades.json)
من نفس الـGist الذي يستخدمه scanner.py، ويرسل ملخصًا عبر تيليجرام يتضمن:
- عدد الصفقات الرابحة، اسم كل صفقة، ونسبة الربح، ومدة كل صفقة
- عدد الصفقات الخاسرة، اسم كل صفقة، ونسبة الخسارة، ومدة كل صفقة
- نسبة النجاح الإجمالية للفترة
"""

import os
import json
import time

from scanner import (
    _gist_get_file,
    CLOSED_GIST_FILE,
    send_telegram,
    format_duration,
)

REPORT_PERIOD_DAYS = int(os.environ.get("REPORT_PERIOD_DAYS", "15"))


def load_closed_trades():
    """يقرأ سجل الصفقات المغلقة كاملاً من الـGist."""
    content = _gist_get_file(CLOSED_GIST_FILE)
    if not content:
        return []
    try:
        return json.loads(content)
    except Exception as e:
        print(f"تعذّر تحليل سجل الصفقات المغلقة: {e}")
        return []


def within_period(trade, days):
    """يتحقق هل تاريخ إغلاق الصفقة يقع ضمن آخر N يوم."""
    closed_at = trade.get("closed_at")
    if not closed_at:
        return False
    try:
        closed_epoch = time.mktime(time.strptime(closed_at, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return False
    return (time.time() - closed_epoch) <= days * 86400


def trade_pct(trade):
    """نسبة الربح/الخسارة الفعلية بناءً على سعر الخروج المسجّل وقت الإغلاق."""
    entry = trade.get("entry")
    exit_price = trade.get("exit_price")
    if entry is None or exit_price is None:
        return None
    return (exit_price - entry) / entry * 100


def trade_duration_str(trade):
    """المدة الزمنية بين فتح الصفقة وإغلاقها، بصيغة مقروءة."""
    opened_at = trade.get("opened_at")
    closed_at = trade.get("closed_at")
    if not opened_at or not closed_at:
        return "غير معروفة"
    try:
        opened_epoch = time.mktime(time.strptime(opened_at, "%Y-%m-%d %H:%M:%S"))
        closed_epoch = time.mktime(time.strptime(closed_at, "%Y-%m-%d %H:%M:%S"))
        hours = (closed_epoch - opened_epoch) / 3600
        return format_duration(hours)
    except Exception:
        return "غير معروفة"


def format_report(trades, period_days):
    recent = [t for t in trades if within_period(t, period_days)]

    header = f"📊 ملخص الأداء (آخر {period_days} يومًا)"
    if not recent:
        return f"{header}\nلا توجد صفقات مغلقة خلال هذه الفترة."

    wins, losses = [], []
    for t in recent:
        pct = trade_pct(t)
        if pct is None:
            continue
        (wins if pct >= 0 else losses).append((t, pct))

    lines = [header, ""]

    lines.append(f"✅ الصفقات الرابحة: {len(wins)}")
    for t, pct in wins:
        name = t["symbol"].replace("USDT", "/USDT")
        dur = trade_duration_str(t)
        lines.append(f"  • {name} — +{pct:.2f}% — {dur}")

    lines.append("")
    lines.append(f"❌ الصفقات الخاسرة: {len(losses)}")
    for t, pct in losses:
        name = t["symbol"].replace("USDT", "/USDT")
        dur = trade_duration_str(t)
        lines.append(f"  • {name} — {pct:.2f}% — {dur}")

    total = len(wins) + len(losses)
    if total:
        win_rate = len(wins) / total * 100
        lines.append("")
        lines.append(f"نسبة النجاح الإجمالية: {win_rate:.1f}% ({len(wins)}/{total})")

    return "\n".join(lines)


def main():
    trades = load_closed_trades()
    report = format_report(trades, REPORT_PERIOD_DAYS)
    print(report)
    send_telegram(report)


if __name__ == "__main__":
    main()
