"""
محاكي أرباح — يحاكي التداول الواقعي: رأس مال إجمالي مقسوم على عدد صفقات متزامنة أقصى
(افتراضيًا 5). أي إشارة جديدة تجيك وكل الشرائح مشغولة تُتجاهل لحد ما تتحرر شريحة
(صفقة موجودة توصل هدفها أو وقف خسارتها)، تمامًا متل واقع التداول الفعلي بمبلغ محدود.

الاستخدام (نفس واجهة trade_simulator.yml — --amount هنا = رأس المال الإجمالي وليس لكل صفقة):
    python trade_simulator.py --days 10 --amount 400

عدد الصفقات المتزامنة قابل للتعديل عبر متغير بيئة اختياري TRADE_MAX_CONCURRENT
(افتراضي 5) بدون الحاجة لتعديل ملف الـworkflow.
"""

import os
import json
import argparse
import datetime as dt
import urllib.request
import urllib.parse
import urllib.error

GIST_RAW_URL = os.environ.get("GIST_RAW_URL")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# نفس نسبة الرسوم المستخدمة سابقًا بـbacktest.py (0.1% لكل جهة = دخول وخروج)
FEE_PCT_PER_SIDE = float(os.environ.get("TRADE_FEE_PCT", "0.1"))
MAX_CONCURRENT = int(os.environ.get("TRADE_MAX_CONCURRENT", "5"))

TYPE_LABELS = {"official": "رسمية", "early": "مبكرة", "breakout": "انفجار"}


def fetch_closed_trades():
    if not GIST_RAW_URL:
        raise SystemExit("❌ GIST_RAW_URL غير موجود بالأسرار (secrets).")
    with urllib.request.urlopen(GIST_RAW_URL, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


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


def simulate(trades, days, capital, max_concurrent):
    cutoff = dt.datetime.now() - dt.timedelta(days=days)
    slot_amount = capital / max_concurrent

    # نأخذ الصفقات اللي فُتحت خلال الفترة المطلوبة (هذا وقت "اتخاذ القرار" الفعلي)
    window = []
    for t in trades:
        if not t.get("opened_at") or not t.get("closed_at"):
            continue
        try:
            opened_at = parse_dt(t["opened_at"])
            closed_at = parse_dt(t["closed_at"])
        except Exception:
            continue
        if opened_at >= cutoff:
            window.append({**t, "_opened_at": opened_at, "_closed_at": closed_at})

    window.sort(key=lambda t: t["_opened_at"])

    incomplete_warning = False
    if trades:
        oldest = min((parse_dt(t["closed_at"]) for t in trades if t.get("closed_at")), default=None)
        if oldest and oldest > cutoff:
            incomplete_warning = True  # السجل النشط قد لا يغطي كامل الفترة المطلوبة (صفقات أقدم انتقلت للأرشيف)

    open_slots = []  # قائمة أوقات إغلاق الصفقات المشغولة حاليًا
    taken, skipped = [], 0

    for t in window:
        # حرّر أي شريحة انتهت صفقتها قبل لحظة فتح هذه الصفقة
        open_slots = [c for c in open_slots if c > t["_opened_at"]]
        if len(open_slots) < max_concurrent:
            open_slots.append(t["_closed_at"])
            taken.append(t)
        else:
            skipped += 1

    by_type = {}
    total_profit = 0.0
    wins = losses = 0

    for t in taken:
        pct = trade_return_pct(t)
        if pct is None:
            continue
        profit = slot_amount * pct / 100
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

    return {
        "n": n,
        "skipped": skipped,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "capital": capital,
        "slot_amount": round(slot_amount, 2),
        "max_concurrent": max_concurrent,
        "total_profit": round(total_profit, 2),
        "final_balance": round(capital + total_profit, 2),
        "by_type": by_type,
        "incomplete_warning": incomplete_warning,
    }


def format_message(days, res):
    lines = [
        f"💰 محاكاة أرباح آخر {days} يوم — رأس مال {res['capital']:.0f}$ "
        f"({res['max_concurrent']} صفقات متزامنة كحد أقصى، {res['slot_amount']:.0f}$ لكل شريحة)"
    ]
    if res["n"] == 0:
        lines.append("لا توجد صفقات دخلت خلال هذه الفترة (بحدود رأس المال والتزامن المحدد).")
        return "\n".join(lines)

    sign = "🟢" if res["total_profit"] >= 0 else "🔴"
    lines.append(f"عدد الصفقات المنفذة: {res['n']} (رابحة {res['wins']} / خاسرة {res['losses']} — نجاح {res['win_rate']}%)")
    if res["skipped"]:
        lines.append(f"⏭️ إشارات تم تجاهلها لعدم توفر شريحة فارغة: {res['skipped']}")
    lines.append(f"{sign} صافي الربح/الخسارة: {res['total_profit']:+.2f}$")
    lines.append(f"الرصيد: {res['capital']:.0f}$ ← {res['final_balance']:.2f}$")

    if res["by_type"]:
        lines.append("— حسب النوع —")
        for ttype, b in res["by_type"].items():
            label = TYPE_LABELS.get(ttype, ttype)
            lines.append(f"{label}: {b['count']} صفقة | {b['profit']:+.2f}$ | نجاح {round(b['wins']/(b['wins']+b['losses'])*100,1) if (b['wins']+b['losses']) else 0}%")

    if res["incomplete_warning"]:
        lines.append("⚠️ ملاحظة: بعض الصفقات الأقدم قد تكون انتقلت للأرشيف ولم تُحتسب هنا (السجل النشط محدود العدد).")

    lines.append("(محاكاة واقعية: رأس المال مقسوم على شرائح متزامنة، والإشارات الزائدة عند امتلاء الشرائح تُتجاهل، بعد خصم رسوم تداول تقديرية 0.1% لكل جهة)")
    return "\n".join(lines)


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID غير موجودين — سيتم الاكتفاء بالطباعة.")
        return
    try:
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode("utf-8")
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        print("فشل إرسال رسالة تيليجرام:", e.read().decode("utf-8", "ignore"))
    except Exception as e:
        print("خطأ إرسال تيليجرام:", e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--amount", type=float, default=400, help="رأس المال الإجمالي (وليس لكل صفقة)")
    args = parser.parse_args()

    trades = fetch_closed_trades()
    res = simulate(trades, args.days, args.amount, MAX_CONCURRENT)
    message = format_message(args.days, res)

    print(message)
    send_telegram(message)


if __name__ == "__main__":
    main()
