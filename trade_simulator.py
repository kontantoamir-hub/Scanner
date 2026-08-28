"""
محاكي أرباح — يحسب كم كان الربح/الخسارة لو دخلت بمبلغ ثابت (مثلاً 50$) في كل صفقة
أغلقها البوت خلال آخر N يوم، بالاعتماد على سجل closed_trades.json المحفوظ في الـGist.

الاستخدام (نفس واجهة trade_simulator.yml):
    python trade_simulator.py --days 10 --amount 50
"""

import os
import argparse
import datetime as dt
import requests

GIST_RAW_URL = os.environ.get("GIST_RAW_URL")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# نفس نسبة الرسوم المستخدمة سابقًا بـbacktest.py (0.1% لكل جهة = دخول وخروج)
FEE_PCT_PER_SIDE = float(os.environ.get("TRADE_FEE_PCT", "0.1"))

TYPE_LABELS = {"official": "رسمية", "early": "مبكرة", "breakout": "انفجار"}


def fetch_closed_trades():
    if not GIST_RAW_URL:
        raise SystemExit("❌ GIST_RAW_URL غير موجود بالأسرار (secrets).")
    r = requests.get(GIST_RAW_URL, timeout=15)
    r.raise_for_status()
    return r.json()


def parse_dt(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def trade_return_pct(t):
    """العائد الصافي% لصفقة واحدة (شراء فوري فقط)، بعد خصم رسوم الدخول والخروج."""
    entry, exit_price = t.get("entry"), t.get("exit_price")
    if not entry or not exit_price:
        return None
    gross_pct = (exit_price - entry) / entry * 100
    net_pct = gross_pct - (2 * FEE_PCT_PER_SIDE)
    return net_pct


def simulate(trades, days, amount):
    cutoff = dt.datetime.now() - dt.timedelta(days=days)
    closed = []
    for t in trades:
        if not t.get("closed_at"):
            continue
        try:
            closed_at = parse_dt(t["closed_at"])
        except Exception:
            continue
        if closed_at >= cutoff:
            closed.append(t)

    incomplete_warning = False
    if trades:
        oldest = min((parse_dt(t["closed_at"]) for t in trades if t.get("closed_at")), default=None)
        if oldest and oldest > cutoff:
            incomplete_warning = True  # السجل النشط قد لا يغطي كامل الفترة المطلوبة (صفقات أقدم انتقلت للأرشيف)

    by_type = {}
    total_profit = 0.0
    wins = losses = 0

    for t in closed:
        pct = trade_return_pct(t)
        if pct is None:
            continue
        profit = amount * pct / 100
        total_profit += profit
        if pct > 0:
            wins += 1
        elif pct < 0:
            losses += 1

        ttype = t.get("type", "official")
        b = by_type.setdefault(ttype, {"count": 0, "profit": 0.0, "wins": 0, "losses": 0})
        b["count"] += 1
        b["profit"] += profit
        if pct > 0:
            b["wins"] += 1
        elif pct < 0:
            b["losses"] += 1

    n = wins + losses
    win_rate = round(wins / n * 100, 1) if n else 0.0
    invested = amount * n

    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "invested": invested,
        "total_profit": round(total_profit, 2),
        "by_type": by_type,
        "incomplete_warning": incomplete_warning,
    }


def format_message(days, amount, res):
    lines = [f"💰 محاكاة أرباح آخر {days} يوم (بدخول {amount:.0f}$ لكل صفقة)"]
    if res["n"] == 0:
        lines.append("لا توجد صفقات مغلقة خلال هذه الفترة.")
        return "\n".join(lines)

    sign = "🟢" if res["total_profit"] >= 0 else "🔴"
    lines.append(f"عدد الصفقات: {res['n']} (رابحة {res['wins']} / خاسرة {res['losses']} — نجاح {res['win_rate']}%)")
    lines.append(f"إجمالي رأس المال المستخدم: {res['invested']:.0f}$")
    lines.append(f"{sign} صافي الربح/الخسارة: {res['total_profit']:+.2f}$")

    if res["by_type"]:
        lines.append("— حسب النوع —")
        for ttype, b in res["by_type"].items():
            label = TYPE_LABELS.get(ttype, ttype)
            lines.append(f"{label}: {b['count']} صفقة | {b['profit']:+.2f}$ | نجاح {round(b['wins']/(b['wins']+b['losses'])*100,1) if (b['wins']+b['losses']) else 0}%")

    if res["incomplete_warning"]:
        lines.append("⚠️ ملاحظة: بعض الصفقات الأقدم قد تكون انتقلت للأرشيف ولم تُحتسب هنا (السجل النشط محدود العدد).")

    lines.append("(الأرقام تفترض دخول متتالٍ بمبلغ ثابت لكل صفقة، بعد خصم رسوم تداول تقديرية 0.1% لكل جهة)")
    return "\n".join(lines)


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID غير موجودين — سيتم الاكتفاء بالطباعة.")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=15,
        )
        if not r.ok:
            print("فشل إرسال رسالة تيليجرام:", r.text)
    except Exception as e:
        print("خطأ إرسال تيليجرام:", e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--amount", type=float, default=50)
    args = parser.parse_args()

    trades = fetch_closed_trades()
    res = simulate(trades, args.days, args.amount)
    message = format_message(args.days, args.amount, res)

    print(message)
    send_telegram(message)


if __name__ == "__main__":
    main()
