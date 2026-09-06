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

FEE_PCT_PER_SIDE = float(os.environ.get("TRADE_FEE_PCT", "0.1"))
MAX_CONCURRENT = int(os.environ.get("TRADE_MAX_CONCURRENT", "5"))

TYPE_LABELS = {"official": "رسمية", "early": "مبكرة", "breakout": "انفجار", "experimental": "تجريبية"}

TP_TARGET_INDEX = {"tp1": 0, "tp2": 1, "tp3": 2}

ACTIVE_FILENAME = "closed_trades.json"
ARCHIVE_PREFIX = "closed_trades_archive_"
MAX_ARCHIVE_LOOKUP = 500


def _archive_url_for(index):
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
    if not GIST_RAW_URL:
        raise SystemExit("❌ GIST_RAW_URL غير موجود بالأسرار (secrets).")

    all_trades = []
    archives_found = 0
    archive_fetch_error = False

    all_trades.extend(_fetch_json_url(GIST_RAW_URL))

    idx = 1
    while idx <= MAX_ARCHIVE_LOOKUP:
        url = _archive_url_for(idx)
        try:
            chunk = _fetch_json_url(url)
            all_trades.extend(chunk)
            archives_found += 1
        except urllib.error.HTTPError as e:
            if e.code == 404:
                break
            print(f"⚠️ خطأ HTTP غير متوقع عند جلب ملف الأرشيف رقم {idx:04d} ({e.code}) — التوقف هنا.")
            archive_fetch_error = True
            break
        except Exception as e:
            print(f"⚠️ خطأ غير متوقع عند جلب ملف الأرشيف رقم {idx:04d} ({e}) — التوقف هنا.")
            archive_fetch_error = True
            break
        idx += 1

    return all_trades, archives_found, archive_fetch_error


def parse_dt(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def trade_return_pct(t):
    entry, exit_price = t.get("entry"), t.get("exit_price")
    if not entry or not exit_price:
        return None
    gross_pct = (exit_price - entry) / entry * 100
    net_pct = gross_pct - (2 * FEE_PCT_PER_SIDE)
    return net_pct


def trade_return_pct_target(t, tp_index=0):
    entry = t.get("entry")
    tps = t.get("tps") or []
    hit_tps = t.get("hit_tps") or []
    if entry and len(tps) > tp_index and tp_index in hit_tps:
        gross_pct = (tps[tp_index] - entry) / entry * 100
        return gross_pct - (2 * FEE_PCT_PER_SIDE)
    return trade_return_pct(t)


def simulate(trades, days, capital, max_concurrent, trade_type="all", archive_fetch_error=False, tp_target="tp1",
             debug=False):
    tp_index = TP_TARGET_INDEX.get(tp_target, 0)
    cutoff = dt.datetime.now() - dt.timedelta(days=days)
    slot_amount = capital / max_concurrent

    # عدادات تشخيص: كم صفقة استُبعدت بكل مرحلة، عشان نعرف بالضبط وين تضيع صفقات النوع المطلوب
    total_seen = 0
    excluded_no_dates = 0
    excluded_wrong_type = 0
    excluded_bad_date_format = 0
    excluded_before_cutoff = 0
    type_seen_total = 0  # صفقات من نفس trade_type بغض النظر عن أي فلتر ثاني

    window = []
    for t in trades:
        total_seen += 1
        is_target_type = (trade_type == "all" or t.get("type", "official") == trade_type)
        if is_target_type:
            type_seen_total += 1

        if not t.get("opened_at") or not t.get("closed_at"):
            if is_target_type:
                excluded_no_dates += 1
            continue
        if trade_type != "all" and t.get("type", "official") != trade_type:
            excluded_wrong_type += 1
            continue
        try:
            opened_at = parse_dt(t["opened_at"])
            closed_at = parse_dt(t["closed_at"])
        except Exception:
            if is_target_type:
                excluded_bad_date_format += 1
            continue
        if opened_at >= cutoff:
            window.append({**t, "_opened_at": opened_at, "_closed_at": closed_at})
        else:
            if is_target_type:
                excluded_before_cutoff += 1

    if debug:
        print(f"[تشخيص] إجمالي الصفقات بالسجل: {total_seen}")
        print(f"[تشخيص] صفقات من النوع المطلوب ({trade_type}) قبل أي استبعاد: {type_seen_total}")
        print(f"[تشخيص] من نفس النوع، استُبعدت لعدم اكتمال opened_at/closed_at: {excluded_no_dates}")
        print(f"[تشخيص] من نفس النوع، استُبعدت بسبب صيغة تاريخ غير صالحة: {excluded_bad_date_format}")
        print(f"[تشخيص] من نفس النوع، استُبعدت لأنها أقدم من الفترة المطلوبة (cutoff): {excluded_before_cutoff}")
        print(f"[تشخيص] صفقات دخلت نافذة الفترة (window) قبل فلترة التزامن: {len(window)}")

    window.sort(key=lambda t: t["_opened_at"])

    incomplete_warning = archive_fetch_error

    open_slots = []
    taken, skipped = [], 0

    for t in window:
        open_slots = [c for c in open_slots if c > t["_opened_at"]]
        if len(open_slots) < max_concurrent:
            open_slots.append(t["_closed_at"])
            taken.append(t)
        else:
            skipped += 1

    if debug:
        print(f"[تشخيص] صفقات فعليًا أُخذت (بعد حدود التزامن/رأس المال): {len(taken)}")
        print(f"[تشخيص] صفقات تُجوهلت لعدم توفر شريحة فارغة: {skipped}")

    by_type = {}
    total_profit = 0.0
    wins = losses = 0

    for t in taken:
        pct = trade_return_pct_target(t, tp_index)
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
        "tp_target": tp_target,
        "total_profit": round(total_profit, 2),
        "final_balance": round(capital + total_profit, 2),
        "by_type": by_type,
        "incomplete_warning": incomplete_warning,
    }


def format_message(days, res, archives_found=0):
    type_label = "الكل" if res["trade_type"] == "all" else TYPE_LABELS.get(res["trade_type"], res["trade_type"])
    tp_label = res.get("tp_target", "tp1").upper()
    lines = [
        f"💰 محاكاة أرباح آخر {days} يوم — نوع الصفقات: {type_label} — هدف الخروج: {tp_label} — رأس مال {res['capital']:.0f}$ "
        f"({res['max_concurrent']} صفقات متزامنة كحد أقصى، {res['slot_amount']:.0f}$ لكل شريحة)"
    ]
    if archives_found:
        lines.append(f"📦 تم دمج {archives_found} ملف أرشيف مع السجل النشط لتغطية الفترة كاملة")

    if res["incomplete_warning"]:
        lines.append("⚠️ تنبيه: صار خطأ فعلي أثناء جلب أحد ملفات الأرشيف (راجع سجل التشغيل/logs) — قد لا تكون البيانات كاملة.")

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

    note = (
        "(محاكاة واقعية: رأس المال مقسوم على شرائح متزامنة، والإشارات الزائدة عند امتلاء الشرائح تُتجاهل، "
        f"بعد خصم رسوم تداول تقديرية 0.1% لكل جهة — أي صفقة وصلت فعليًا لهدف {tp_label} في أي وقت تُحسب "
        "بربح ذلك الهدف مباشرة (بغض النظر عمّا حصل بعده)، وما لم تصله يُحتسب عائدها الفعلي المسجّل عند الإغلاق)"
    )
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
    parser.add_argument("--type", type=str, default="all",
                         choices=["all", "official", "early", "breakout", "experimental"])
    parser.add_argument("--tp", type=str, default="tp1", choices=["tp1", "tp2", "tp3"],
                         help="الهدف الذي يُحتسب الخروج عنده إذا تحقق فعليًا (افتراضي: tp1)")
    parser.add_argument("--debug", action="store_true", help="طباعة تفاصيل تشخيصية عن سبب استبعاد الصفقات")
    args = parser.parse_args()

    trades, archives_found, archive_fetch_error = fetch_all_trades()
    res = simulate(trades, args.days, args.amount, MAX_CONCURRENT, args.type, archive_fetch_error, args.tp,
                   debug=args.debug)
    message = format_message(args.days, res, archives_found)

    print(message)
    send_telegram(message)


if __name__ == "__main__":
    main()
