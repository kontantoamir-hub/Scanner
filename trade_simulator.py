"""
محاكي أرباح — يحاكي التداول الواقعي: رأس مال إجمالي مقسوم على عدد صفقات متزامنة أقصى
(افتراضيًا 5). أي إشارة جديدة تجيك وكل الشرائح مشغولة تُتجاهل لحد ما تتحرر شريحة
(صفقة موجودة توصل هدفها أو وقف خسارتها)، تمامًا متل واقع التداول الفعلي بمبلغ محدود.

يجمع الصفقات من السجل النشط (closed_trades.json) + كل ملفات الأرشيف المرقّمة
(closed_trades_archive_0001.json, 0002.json, ...) بنفس الـGist، عشان فترات (--days)
أطول من عمر السجل النشط الحالي تُحتسب بشكل كامل وصحيح بدل ما تتوقف عند حدود السجل النشط.

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

# نفس التسمية المستخدمة بـscanner.py (archive_overflow) لملفات الأرشيف المرقّمة
ACTIVE_FILENAME = "closed_trades.json"
ARCHIVE_PREFIX = "closed_trades_archive_"
MAX_ARCHIVE_LOOKUP = 500  # سقف أمان لعدد ملفات الأرشيف المفحوصة (يفوق أي حجم واقعي متوقع)


def _archive_url_for(index):
    """يبني رابط ملف أرشيف رقم index بنفس نمط GIST_RAW_URL (استبدال اسم الملف النشط فقط)."""
    filename = f"{ARCHIVE_PREFIX}{index:04d}.json"
    if ACTIVE_FILENAME in GIST_RAW_URL:
        return GIST_RAW_URL.replace(ACTIVE_FILENAME, filename)
    raise SystemExit(
        f"❌ تعذّر بناء رابط الأرشيف تلقائيًا من GIST_RAW_URL "
        f"(الرابط لا يحتوي اسم الملف '{ACTIVE_FILENAME}' صراحة)."
    )


def _fetch_json_url(url):
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_all_trades():
    """
    يجمع كل الصفقات المغلقة: السجل النشط أولاً، ثم كل ملفات الأرشيف بالترتيب
    (0001 فصاعدًا) لحد ما يوصل لأول رقم غير موجود (404) فيتوقف — بهذا الشكل
    فترة --days الطويلة تغطي كل الصفقات المتوفرة فعليًا، مش بس السجل النشط.
    """
    if not GIST_RAW_URL:
        raise SystemExit("❌ GIST_RAW_URL غير موجود بالأسرار (secrets).")

    all_trades = []
    archives_found = 0

    # 1) السجل النشط
    all_trades.extend(_fetch_json_url(GIST_RAW_URL))

    # 2) ملفات الأرشيف بالترتيب، من الأقدم (0001) صعودًا لحد أول ملف غير موجود
    idx = 1
    while idx <= MAX_ARCHIVE_LOOKUP:
        url = _archive_url_for(idx)
        try:
            chunk = _fetch_json_url(url)
            all_trades.extend(chunk)
            archives_found += 1
        except urllib.error.HTTPError as e:
            if e.code == 404:
                break  # ما فيه أرشيف بهذا الرقم — وصلنا لنهاية الأرشيف
            raise
        idx += 1

    return all_trades, archives_found


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


def trade_return_pct_tp1(t):
    """
    نفس trade_return_pct، لكن للصفقات الرسمية: لو الهدف الأول (index 0) تحقق في أي وقت،
    نحتسب الخروج عنده مباشرة (تجاهل ما حصل بعده — رجوع لـSL أو استمرار لبقية الأهداف)،
    بدل انتظار الإغلاق الفعلي النهائي المسجّل بالبوت.
    """
    entry = t.get("entry")
    tps = t.get("tps") or []
    hit_tps = t.get("hit_tps") or []
    if t.get("type") == "official" and entry and tps and 0 in hit_tps:
        gross_pct = (tps[0] - entry) / entry * 100
        return gross_pct - (2 * FEE_PCT_PER_SIDE)
    return trade_return_pct(t)


def simulate(trades, days, capital, max_concurrent, trade_type="all"):
    cutoff = dt.datetime.now() - dt.timedelta(days=days)
    slot_amount = capital / max_concurrent

    # نأخذ الصفقات اللي فُتحت خلال الفترة المطلوبة (هذا وقت "اتخاذ القرار" الفعلي)
    # مع فلترة النوع إذا حُدد (رسمية فقط / مبكرة فقط) — الإشارات من نوع آخر تُتجاهل بالكامل
    # وما تنافس على الشرائح أصلًا (كأنك ما شفتها من الأساس)
    window = []
    for t in trades:
        if not t.get("opened_at") or not t.get("closed_at"):
            continue
        if trade_type != "all" and t.get("type", "official") != trade_type:
            continue
        try:
            opened_at = parse_dt(t["opened_at"])
            closed_at = parse_dt(t["closed_at"])
        except Exception:
            continue
        if opened_at >= cutoff:
            window.append({**t, "_opened_at": opened_at, "_closed_at": closed_at})

    window.sort(key=lambda t: t["_opened_at"])

    # مع دمج الأرشيف، تغطية البيانات صارت شبه مؤكدة طالما فيه أرشيف كافٍ — بس نبقي
    # نفس التحذير احتياطًا لأي حالة نادرة (أول صفقة بكل الملفات مجتمعة أحدث من الفترة المطلوبة)
    incomplete_warning = False
    if trades:
        oldest = min((parse_dt(t["closed_at"]) for t in trades if t.get("closed_at")), default=None)
        if oldest and oldest > cutoff:
            incomplete_warning = True

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
        pct = trade_return_pct_tp1(t)
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
        "trade_type": trade_type,
        "total_profit": round(total_profit, 2),
        "final_balance": round(capital + total_profit, 2),
        "by_type": by_type,
        "incomplete_warning": incomplete_warning,
    }


def format_message(days, res, archives_found=0):
    type_label = "الكل" if res["trade_type"] == "all" else TYPE_LABELS.get(res["trade_type"], res["trade_type"])
    lines = [
        f"💰 محاكاة أرباح آخر {days} يوم — نوع الصفقات: {type_label} — رأس مال {res['capital']:.0f}$ "
        f"({res['max_concurrent']} صفقات متزامنة كحد أقصى، {res['slot_amount']:.0f}$ لكل شريحة)"
    ]
    if archives_found:
        lines.append(f"📦 تم دمج {archives_found} ملف أرشيف مع السجل النشط لتغطية الفترة كاملة")

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
        lines.append("⚠️ ملاحظة: بعض الصفقات الأقدم قد تكون غير مشمولة (تحقق من اكتمال ملفات الأرشيف بالـGist).")

    note = "(محاكاة واقعية: رأس المال مقسوم على شرائح متزامنة، والإشارات الزائدة عند امتلاء الشرائح تُتجاهل، بعد خصم رسوم تداول تقديرية 0.1% لكل جهة"
    if res["trade_type"] in ("all", "official"):
        note += " — الصفقات الرسمية تُحسب بربح الهدف الأول فورًا إذا تحقق، بغض النظر عمّا حصل بعده"
    note += ")"
    lines.append(note)
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
    parser.add_argument("--type", type=str, default="all", choices=["all", "official", "early", "breakout"])
    args = parser.parse_args()

    trades, archives_found = fetch_all_trades()
    res = simulate(trades, args.days, args.amount, MAX_CONCURRENT, args.type)
    message = format_message(args.days, res, archives_found)

    print(message)
    send_telegram(message)


if __name__ == "__main__":
    main()
