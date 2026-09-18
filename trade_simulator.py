"""
محاكي أرباح — يحاكي التداول الواقعي: رأس مال إجمالي مقسوم على عدد صفقات متزامنة أقصى
(افتراضيًا 5). أي إشارة جديدة تجيك وكل الشرائح مشغولة تُتجاهل لحد ما تتحرر شريحة
(صفقة موجودة توصل هدفها أو وقف خسارتها)، تمامًا متل واقع التداول الفعلي بمبلغ محدود.

يجمع الصفقات من السجل النشط (closed_trades.json) + كل ملفات الأرشيف المرقّمة
(closed_trades_archive_0001.json, 0002.json, ...) — سواء كانت داخل نفس الـGist الرئيسي،
أو موزّعة على Gists أرشيف منفصلة يتتبعها scanner.py عبر archive_gists_chain.json — عشان
فترات (--days) أطول من عمر السجل النشط الحالي، أو تمتد لأبعد من أول rotation للأرشيف،
تُحتسب بشكل كامل وصحيح.

تحديث (طريقة قراءة البيانات — إصلاح تجمّد التاريخ): كان هذا الملف يقرأ عبر رابط GIST_RAW_URL
ثابت (raw.githubusercontent.com/.../raw/<commit-sha>/closed_trades.json). المشكلة أن رابط
"Raw" المنسوخ من صفحة الـGist يتضمّن غالبًا SHA لتلك اللحظة بالضبط، وهذا النوع من الروابط
لا يتحدّث أبدًا حتى لو تغيّر محتوى الملف بالـGist لاحقًا — يبقى مجمّدًا على تلك اللقطة الزمنية
للأبد (وهذا سبب تجمّد أحدث closed_at على تاريخ معيّن رغم استمرار تسجيل صفقات جديدة فعليًا).
كمان هذه الطريقة لم تكن تتتبع archive_gists_chain.json إطلاقًا، فكانت تفترض أن كل الأرشيف
موجود بنفس الـGist وتُضيّع أي جزء منه انتقل لـGist أرشيف منفصل بعد rotation.
الحل: الملف الآن يقرأ عبر GitHub API مباشرة بمعرف الـGist (GIST_ID + GIST_TOKEN) ويتتبع سلسلة
الأرشيف بالضبط بنفس منطق load_closed_trades في trade_stats.py — نفس Secrets، ونفس السلوك
دائمًا يجيب أحدث نسخة فعلية من البيانات بدل نسخة مجمّدة.

مهم — معيار نافذة الفترة (--days):
الفلترة تتم على أساس تاريخ **الإغلاق** (closed_at) وليس تاريخ الدخول (opened_at).
السبب: نافذة "آخر N يوم" المطلوبة تجاوب على سؤال "شو صار برأس مالي بآخر N يوم؟" —
وهذا يتحدد بالصفقات التي *تحقق ربحها/خسارتها فعليًا* (أي أُغلقت) خلال هذه الفترة، بغض
النظر عن كونها فُتحت قبلها بفترة أطول (لو مدة الاحتفاظ المتوسطة بالصفقات أطول من N يوم،
فلترة حسب opened_at بدل closed_at تستبعد كل الصفقات القريبة وتظهر نتيجة "لا يوجد صفقات"
بشكل خاطئ حتى لو أُغلقت عشرات الصفقات فعليًا خلال الفترة). يمكن الرجوع للسلوك القديم
(الفلترة حسب opened_at) عبر --window-by opened لو احتجت ذلك لأي سبب.

نقطة بداية ثابتة (--since) بدل الرجوع N يوم للخلف من الآن:
لو عملت Reset كامل للبوت (بدء حساب من الصفر من لحظة معينة)، الرجوع "N يوم للخلف من الآن"
مو المنطق الصح لأنو ممكن يرجع لفترة قبل الـ Reset أصلاً. --since يحل هذا: تعطيه تاريخ/وقت
لحظة الـ Reset، ويصير هو نقطة البداية الثابتة بدل "الآن - days". فيه وضعين عبر --since-mode:
  - open   (افتراضي): من لحظة الـ Reset لحد الآن، بدون أي سقف زمني (--days تُتجاهل كنافذة).
  - capped: نافذة ثابتة = [لحظة الـ Reset, لحظة الـ Reset + --days يوم]، حتى لو تجاوز
            الوقت الحالي هذا السقف (يعني ما بتستمر بالتوسع بعد ما تخلص أيام الـ N).

المتغيرات المطلوبة (نفس Secrets المستخدمة في scanner.py وtrade_stats.py):
  GIST_TOKEN, GIST_ID
اختياري لإرسال النتيجة عبر تيليجرام بدل الطباعة فقط:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

الاستخدام (--amount هنا = رأس المال الإجمالي وليس لكل صفقة):
    python trade_simulator.py --days 10 --amount 400
    python trade_simulator.py --since "2026-09-17 22:00:00" --amount 400
    python trade_simulator.py --since "2026-09-17 22:00:00" --since-mode capped --days 10 --amount 400

عدد الصفقات المتزامنة قابل للتعديل عبر متغير بيئة اختياري TRADE_MAX_CONCURRENT
(افتراضي 5) بدون الحاجة لتعديل ملف الـworkflow.
"""

import os
import json
import argparse
import datetime as dt
import urllib.parse
import urllib.request
import urllib.error
import requests

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

FEE_PCT_PER_SIDE = float(os.environ.get("TRADE_FEE_PCT", "0.1"))
MAX_CONCURRENT = int(os.environ.get("TRADE_MAX_CONCURRENT", "5"))

TYPE_LABELS = {"official": "رسمية", "early": "مبكرة", "breakout": "انفجار", "experimental": "تجريبية"}

TP_TARGET_INDEX = {"tp1": 0, "tp2": 1, "tp3": 2}

ACTIVE_GIST_FILE = "closed_trades.json"           # السجل النشط: أحدث الصفقات فقط
ARCHIVE_PREFIX = "closed_trades_archive_"          # ملفات الأرشيف المرقّمة
ARCHIVE_CHAIN_FILE = "archive_gists_chain.json"    # قائمة معرّفات كل Gists الأرشيف عبر الوقت (نفس اسم الحقل بscanner.py)


def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def _fetch_gist_files(gist_id):
    """يجلب قاموس ملفات أي Gist (الرئيسي أو أي Gist أرشيف منفصل) عبر رقم معرّفه."""
    r = requests.get(f"https://api.github.com/gists/{gist_id}", headers=_gist_headers(), timeout=15)
    r.raise_for_status()
    return r.json().get("files", {})


def _read_json_file(file_entry, filename):
    """يقرأ محتوى ملف من استجابة Gist، مع التحقق من احتمال البتر (truncated) لملف كبير جدًا
    والجلب من raw_url في هذه الحالة بدل الاعتماد على content فقط."""
    if file_entry.get("truncated"):
        raw_url = file_entry.get("raw_url")
        try:
            rr = requests.get(raw_url, timeout=15)
            rr.raise_for_status()
            content = rr.text
        except Exception as e:
            print(f"⚠️ تعذّر جلب المحتوى الكامل غير المبتور لـ {filename}: {e}")
            content = file_entry.get("content", "[]")
    else:
        content = file_entry.get("content", "[]")
    try:
        return json.loads(content)
    except Exception as e:
        print(f"⚠️ تعذّر تحليل {filename}: {e}")
        return []


def _archive_trades_from_files(files):
    """يستخرج كل صفقات ملفات الأرشيف (closed_trades_archive_NNNN.json) من قاموس ملفات Gist واحد."""
    out = []
    archive_names = sorted(fn for fn in files if fn.startswith(ARCHIVE_PREFIX))
    for name in archive_names:
        out.extend(_read_json_file(files[name], name))
    return out, len(archive_names)


def fetch_all_trades():
    """يجمع السجل النشط (closed_trades.json بالـGist الرئيسي) مع كل ملفات الأرشيف — سواء
    كانت داخل نفس الـGist الرئيسي، أو موزّعة على Gists أرشيف منفصلة يتتبعها scanner.py عبر
    archive_gists_chain.json — بنفس المنطق تمامًا المستخدم في load_closed_trades داخل
    trade_stats.py، بدل الاعتماد على رابط raw ثابت قد يتجمّد على لقطة زمنية قديمة."""
    if not GIST_TOKEN or not GIST_ID:
        raise SystemExit("❌ GIST_TOKEN أو GIST_ID غير موجودين في متغيرات البيئة (secrets).")

    all_trades = []
    archives_found = 0
    archive_fetch_error = False

    try:
        main_files = _fetch_gist_files(GIST_ID)
    except Exception as e:
        raise SystemExit(f"❌ تعذّر جلب الـGist الرئيسي ({GIST_ID}): {e}")

    # اقرأ سلسلة الـGists الأرشيفية (لو موجودة) — نفس الملف الذي يكتبه scanner.py
    try:
        chain_raw = main_files.get(ARCHIVE_CHAIN_FILE, {}).get("content")
        chain = json.loads(chain_raw) if chain_raw else []
    except Exception as e:
        print(f"⚠️ تعذّر تحليل {ARCHIVE_CHAIN_FILE}: {e}")
        chain = []

    seen_gist_ids = set()

    # اجمع أرشيف كل Gist مذكور بالسلسلة (الأقدم أولًا بترتيب السلسلة نفسه، ثم النشط أخيرًا)
    for gist_id in chain:
        if gist_id in seen_gist_ids:
            continue
        seen_gist_ids.add(gist_id)
        try:
            files = main_files if gist_id == GIST_ID else _fetch_gist_files(gist_id)
        except Exception as e:
            print(f"⚠️ تعذّر جلب Gist الأرشيف {gist_id} ضمن السلسلة: {e} — قد تكون البيانات ناقصة.")
            archive_fetch_error = True
            continue
        trades, n_files = _archive_trades_from_files(files)
        all_trades.extend(trades)
        archives_found += n_files

    # احتياطًا: لو فيه ملفات أرشيف بالـGist الرئيسي نفسه ولم يكن مذكورًا بالسلسلة (مثلاً
    # حالة قديمة قبل إضافة السلسلة، أو سلسلة فارغة/تالفة) — لا نفقدها
    if GIST_ID not in seen_gist_ids:
        trades, n_files = _archive_trades_from_files(main_files)
        all_trades.extend(trades)
        archives_found += n_files

    # ثم السجل النشط (الأحدث)
    if ACTIVE_GIST_FILE in main_files:
        all_trades.extend(_read_json_file(main_files[ACTIVE_GIST_FILE], ACTIVE_GIST_FILE))

    return all_trades, archives_found, archive_fetch_error


def parse_dt(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def _print_data_range_diagnostics(trades):
    """يطبع أقدم/أحدث تاريخ دخول وإغلاق موجود بكامل السجل (بدون أي فلترة نوع/فترة)،
    عشان تكتشف فورًا لو صار انقطاع بتحديث البيانات (السكانر توقف / تعطّل) بدل ما تفسّرها
    غلط كمشكلة بمنطق المحاكي."""
    opened_dates, closed_dates = [], []
    for t in trades:
        if t.get("opened_at"):
            try:
                opened_dates.append(parse_dt(t["opened_at"]))
            except Exception:
                pass
        if t.get("closed_at"):
            try:
                closed_dates.append(parse_dt(t["closed_at"]))
            except Exception:
                pass
    now = dt.datetime.now()
    print("[تشخيص] نطاق تواريخ السجل الكامل (قبل أي فلترة):")
    if opened_dates:
        oldest, newest = min(opened_dates), max(opened_dates)
        print(f"  opened_at: من {oldest} إلى {newest} (أحدث دخول قبل {(now - newest).days} يوم)")
    else:
        print("  opened_at: لا توجد تواريخ صالحة إطلاقًا")
    if closed_dates:
        oldest, newest = min(closed_dates), max(closed_dates)
        print(f"  closed_at: من {oldest} إلى {newest} (أحدث إغلاق قبل {(now - newest).days} يوم)")
        if (now - newest).days >= 3:
            print(f"  ⚠️ تنبيه: أحدث صفقة مسجّلة عمرها {(now - newest).days} يوم — يرجّح توقف السكانر عن "
                  f"إضافة صفقات جديدة، وهذا سبب مختلف تمامًا عن أي خطأ بمنطق المحاكي نفسه.")
    else:
        print("  closed_at: لا توجد تواريخ صالحة إطلاقًا")


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
             debug=False, window_by="closed", since=None, since_mode="open"):
    tp_index = TP_TARGET_INDEX.get(tp_target, 0)
    now = dt.datetime.now()

    end_cap = None
    if since is not None:
        cutoff = since
        if since_mode == "capped":
            end_cap = since + dt.timedelta(days=days)
        # since_mode == "open" -> بدون سقف، لحد الآن
    else:
        cutoff = now - dt.timedelta(days=days)

    slot_amount = capital / max_concurrent

    if debug:
        _print_data_range_diagnostics(trades)
        if since is not None:
            print(f"[تشخيص] نقطة بداية ثابتة (--since): {cutoff}  |  وضع: {since_mode}"
                  + (f"  |  سقف النافذة: {end_cap}" if end_cap else "  |  بدون سقف (حتى الآن)"))
        else:
            print(f"[تشخيص] معيار نافذة الفترة المستخدم: {window_by} "
                  f"({'تاريخ الإغلاق' if window_by == 'closed' else 'تاريخ الدخول'}) | cutoff: {cutoff}")

    # عدادات تشخيص: كم صفقة استُبعدت بكل مرحلة، عشان نعرف بالضبط وين تضيع صفقات النوع المطلوب
    total_seen = 0
    excluded_no_dates = 0
    excluded_wrong_type = 0
    excluded_bad_date_format = 0
    excluded_before_cutoff = 0
    excluded_after_cap = 0
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

        window_ref = closed_at if window_by == "closed" else opened_at
        if window_ref < cutoff:
            if is_target_type:
                excluded_before_cutoff += 1
            continue
        if end_cap is not None and window_ref > end_cap:
            if is_target_type:
                excluded_after_cap += 1
            continue

        window.append({**t, "_opened_at": opened_at, "_closed_at": closed_at})

    if debug:
        print(f"[تشخيص] إجمالي الصفقات بالسجل: {total_seen}")
        print(f"[تشخيص] صفقات من النوع المطلوب ({trade_type}) قبل أي استبعاد: {type_seen_total}")
        print(f"[تشخيص] من نفس النوع، استُبعدت لعدم اكتمال opened_at/closed_at: {excluded_no_dates}")
        print(f"[تشخيص] من نفس النوع، استُبعدت بسبب صيغة تاريخ غير صالحة: {excluded_bad_date_format}")
        print(f"[تشخيص] من نفس النوع، استُبعدت لأنها أقدم من نقطة البداية (cutoff): {excluded_before_cutoff}")
        if end_cap is not None:
            print(f"[تشخيص] من نفس النوع، استُبعدت لأنها بعد سقف النافذة (end_cap): {excluded_after_cap}")
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
        "since": since,
        "since_mode": since_mode,
        "end_cap": end_cap,
        "days": days,
    }


def format_message(days, res, archives_found=0):
    type_label = "الكل" if res["trade_type"] == "all" else TYPE_LABELS.get(res["trade_type"], res["trade_type"])
    tp_label = res.get("tp_target", "tp1").upper()

    if res.get("since") is not None:
        since = res["since"]
        if res.get("end_cap") is not None:
            period_label = f"من {since:%Y-%m-%d %H:%M} إلى {res['end_cap']:%Y-%m-%d %H:%M} (سقف {days} يوم بعد الـ Reset)"
        else:
            period_label = f"منذ الـ Reset ({since:%Y-%m-%d %H:%M}) وحتى الآن"
    else:
        period_label = f"آخر {days} يوم"

    lines = [
        f"💰 محاكاة أرباح {period_label} — نوع الصفقات: {type_label} — هدف الخروج: {tp_label} — رأس مال {res['capital']:.0f}$ "
        f"({res['max_concurrent']} صفقات متزامنة كحد أقصى، {res['slot_amount']:.0f}$ لكل شريحة)"
    ]
    if archives_found:
        lines.append(f"📦 تم دمج {archives_found} ملف أرشيف مع السجل النشط لتغطية الفترة كاملة")

    if res["incomplete_warning"]:
        lines.append("⚠️ تنبيه: صار خطأ فعلي أثناء جلب أحد Gists الأرشيف (راجع سجل التشغيل/logs) — قد لا تكون البيانات كاملة.")

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
    parser.add_argument("--days", type=int, default=10,
                         help="بدون --since: عدد الأيام للرجوع للخلف من الآن. مع --since وWith --since-mode capped: "
                              "طول النافذة (أيام) بعد نقطة الـ Reset.")
    parser.add_argument("--amount", type=float, default=400, help="رأس المال الإجمالي (وليس لكل صفقة)")
    parser.add_argument("--type", type=str, default="all",
                         choices=["all", "official", "early", "breakout", "experimental"])
    parser.add_argument("--tp", type=str, default="tp1", choices=["tp1", "tp2", "tp3"],
                         help="الهدف الذي يُحتسب الخروج عنده إذا تحقق فعليًا (افتراضي: tp1)")
    parser.add_argument("--window-by", type=str, default="closed", choices=["closed", "opened"],
                         help="معيار نافذة الفترة --days: closed = حسب تاريخ الإغلاق (الافتراضي والأصحّ "
                              "لسؤال 'شو صار بمحفظتي بآخر N يوم')، opened = حسب تاريخ الدخول (السلوك القديم)")
    parser.add_argument("--since", type=str, default=None,
                         help="نقطة بداية ثابتة بصيغة 'YYYY-MM-DD HH:MM:SS' (مثلاً لحظة عمل Reset كامل للبوت)، "
                              "تحل محل حساب cutoff من --days. راجع --since-mode لتحديد هل فيه سقف زمني أو لا.")
    parser.add_argument("--since-mode", type=str, default="open", choices=["open", "capped"],
                         help="فقط مع --since. open (افتراضي) = من لحظة الـ Reset لحد الآن بدون سقف. "
                              "capped = نافذة ثابتة أقصاها --days يوم بعد لحظة الـ Reset، حتى لو تجاوزها الوقت الحالي.")
    parser.add_argument("--debug", action="store_true", help="طباعة تفاصيل تشخيصية عن سبب استبعاد الصفقات")
    args = parser.parse_args()

    since_dt = parse_dt(args.since) if args.since else None

    trades, archives_found, archive_fetch_error = fetch_all_trades()
    res = simulate(trades, args.days, args.amount, MAX_CONCURRENT, args.type, archive_fetch_error, args.tp,
                    debug=args.debug, window_by=args.window_by, since=since_dt, since_mode=args.since_mode)
    message = format_message(args.days, res, archives_found)

    print(message)
    send_telegram(message)


if __name__ == "__main__":
    main()
