"""
paper_trading.py — محفظة وهمية حقيقية (Paper Trading)
========================================================
منفصل بالكامل عن scanner.py: Gist خاص به + بوت Telegram خاص به، ولا يخزّن أي
شيء في Gist السكانر الأصلي. الملف الوحيد الذي يربطه بالسكانر هو استدعاء
الدالة run_cycle() من داخل scanner.py عند كل دورة فحص (لأن الإشارات الأربعة
—رسمية/مبكرة/انفجار/تجريبية— تُولَّد هناك فقط ولا يوجد مكان آخر يلتقطها).

المنطق:
- رأس مال وهمي ثابت = PAPER_CAPITAL (افتراضيًا 400$)
- حجم كل صفقة ثابت = PAPER_CAPITAL / PAPER_MAX_POSITIONS (افتراضيًا 400/8 = 50$)
- حد أقصى PAPER_MAX_POSITIONS صفقة مفتوحة في نفس الوقت (افتراضيًا 8)
- لو الرصيد المتاح < حجم الصفقة، أو عدد الصفقات المفتوحة وصل الحد الأقصى
  → الإشارة تُتفوّت بدون تنفيذ وهمي (نفس منطق التداول الحقيقي تمامًا)
- يدخل في كل الأنواع الأربعة (رسمية/مبكرة/انفجار/تجريبية) بدون استثناء
- الرصيد يتحرك فعليًا: يُخصم عند الفتح ويُعاد (+ الربح/الخسارة الصافية بعد
  عمولة التداول) عند الإغلاق — وليس مجرد تقرير نهائي
"""

import os
import time
import json
import requests

# ---------------- إعدادات عامة ----------------
PAPER_CAPITAL = float(os.environ.get("PAPER_CAPITAL", "400"))
PAPER_MAX_POSITIONS = int(os.environ.get("PAPER_MAX_POSITIONS", "8"))
PAPER_POSITION_SIZE = round(PAPER_CAPITAL / PAPER_MAX_POSITIONS, 8)
PAPER_TRADING_FEE_PCT = float(os.environ.get("PAPER_TRADING_FEE_PCT", "0.2"))  # عمولة الدخول+الخروج مجتمعة
PAPER_TIME_STOP_HOURS = float(os.environ.get("PAPER_TIME_STOP_HOURS", "96"))

# ---------------- بوت Telegram الخاص بالمحفظة الوهمية (منفصل كليًا عن بوت السكانر) ----------------
# يمكن تجاوزهما عبر متغيرات بيئة PAPER_TELEGRAM_TOKEN / PAPER_TELEGRAM_CHAT_ID (GitHub Secrets)،
# والقيم هنا هي القيم التي أُنشئ بها البوت الجديد عبر BotFather.
PAPER_TELEGRAM_TOKEN = os.environ.get("PAPER_TELEGRAM_TOKEN", "")
PAPER_TELEGRAM_CHAT_ID = os.environ.get("PAPER_TELEGRAM_CHAT_ID", "")

# ---------------- Gist الخاص بالمحفظة الوهمية (منفصل كليًا عن Gist السكانر) ----------------
# GIST_TOKEN نفسه المستخدم في السكانر (توكن GitHub الشخصي بصلاحية gist) يُعاد استخدامه هنا
# تلقائيًا لأنه ينتمي لنفس الحساب ويملك صلاحية الكتابة على أي Gist خاص به — لا داعي لتوكن جديد،
# فقط PAPER_GIST_ID مختلف. يمكن التجاوز عبر PAPER_GIST_TOKEN لو احتجت توكن مختلف لاحقًا.
PAPER_GIST_TOKEN = os.environ.get("PAPER_GIST_TOKEN") or os.environ.get("GIST_TOKEN")
PAPER_GIST_ID = os.environ.get("PAPER_GIST_ID", "af82b35a4fde92f671d596bc6c18f4f2")

BALANCE_FILE = "paper_balance.json"
POSITIONS_FILE = "paper_positions.json"
CLOSED_FILE = "paper_closed_trades.json"          # السجل النشط: أحدث الصفقات فقط (قراءة سريعة)
ACTIVE_HISTORY_SIZE = 150  # عدد الصفقات المغلقة بالسجل النشط قبل ترحيل الأقدم للأرشيف (مثل السكانر الأصلي)

# ---------------- أرشيف الصفقات المغلقة (نفس منطق scanner.py بالضبط) ----------------
# بدل حذف الصفقات الأقدم من ACTIVE_HISTORY_SIZE نهائيًا، تُرحَّل لملفات أرشيف مرقّمة
# داخل Gist أرشيف مخصص (يبدأ بنفس الـGist الرئيسي، وينتقل تلقائيًا لـGist أرشيف جديد
# عند الاقتراب من حد GitHub الفعلي 300 ملف/Gist) — لا يُفقد أي شيء أبدًا.
ARCHIVE_PREFIX = "paper_closed_trades_archive_"    # بادئة ملفات الأرشيف المرقّمة
ARCHIVE_CHUNK_SIZE = 150                           # حد أقصى للصفقات بكل ملف أرشيف
ARCHIVE_CHAIN_FILE = "paper_archive_gists_chain.json"  # بالـGist الرئيسي: سلسلة معرّفات Gists الأرشيف (الأخير = النشط للكتابة)
ARCHIVE_INDEX_FILE = "paper_archive_index.json"    # فهرس صغير: أي Gist أرشيف آخر تحديث له
MAX_ARCHIVE_FILES_BEFORE_ROTATE = 280              # هامش أمان قبل حد GitHub (300 ملف/Gist)


class GistFetchError(Exception):
    pass


# ---------------- Gist helpers (نسخة مستقلة، تستهدف PAPER_GIST_ID فقط) ----------------

def _gist_headers():
    return {"Authorization": f"token {PAPER_GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def _gist_get_all_files():
    if not PAPER_GIST_TOKEN or not PAPER_GIST_ID:
        print("⚠️ [محفظة وهمية] PAPER_GIST_TOKEN أو PAPER_GIST_ID غير موجودين — تخطي هذه الدورة.")
        return {}
    try:
        r = requests.get(f"https://api.github.com/gists/{PAPER_GIST_ID}", headers=_gist_headers(), timeout=15)
        r.raise_for_status()
        return r.json().get("files", {})
    except Exception as e:
        raise GistFetchError(f"تعذّر قراءة ملفات Gist المحفظة الوهمية: {e}") from e


def _gist_get_file(filename, gist_files):
    f = gist_files.get(filename)
    return f["content"] if f else None


def _gist_get_all_files_for(gist_id):
    """مثل _gist_get_all_files لكن لأي معرف Gist — يُستخدم لقراءة ملفات Gist أرشيف
    منفصل لو كان مختلفًا عن الـGist الرئيسي (PAPER_GIST_ID)."""
    if not PAPER_GIST_TOKEN or not gist_id:
        return {}
    try:
        r = requests.get(f"https://api.github.com/gists/{gist_id}", headers=_gist_headers(), timeout=15)
        r.raise_for_status()
        return r.json().get("files", {})
    except Exception as e:
        raise GistFetchError(f"تعذّر قراءة ملفات Gist أرشيف المحفظة الوهمية {gist_id}: {e}") from e


def _create_new_gist(description="Paper Trading - أرشيف صفقات وهمية إضافي"):
    """ينشئ Gist خاص جديد (يبدأ بملف README بسيط) ويرجع معرفه — يُستخدم عند تدوير الأرشيف."""
    payload = {
        "description": description,
        "public": False,
        "files": {"README.md": {"content": "أرشيف صفقات مغلقة للمحفظة الوهمية — يُدار تلقائيًا، لا تعدّل يدويًا."}},
    }
    r = requests.post("https://api.github.com/gists", headers=_gist_headers(), json=payload, timeout=20)
    r.raise_for_status()
    return r.json()["id"]


def get_active_archive_gist(main_gist_files):
    """يرجع (معرف Gist الأرشيف النشط حاليًا للكتابة، سلسلة الـGists، ملفاته الحالية).
    ينشئ Gist أرشيف جديد تلقائيًا لو عدد ملفات الأرشيف بالنشط الحالي يقارب حد GitHub
    الفعلي (300 ملف/Gist) — بنفس منطق get_active_archive_gist في scanner.py."""
    try:
        chain_raw = _gist_get_file(ARCHIVE_CHAIN_FILE, main_gist_files)
        chain = json.loads(chain_raw) if chain_raw else []
    except Exception:
        chain = []

    if not chain:
        new_id = _create_new_gist()
        return new_id, [new_id], {}

    active_id = chain[-1]
    active_files = main_gist_files if active_id == PAPER_GIST_ID else _gist_get_all_files_for(active_id)

    archive_count = sum(1 for fn in active_files if fn.startswith(ARCHIVE_PREFIX))
    if archive_count >= MAX_ARCHIVE_FILES_BEFORE_ROTATE:
        new_id = _create_new_gist()
        chain.append(new_id)
        return new_id, chain, {}

    return active_id, chain, active_files


def archive_overflow(overflow_trades, main_gist_files):
    """يوزّع الصفقات القديمة الفائضة على ملفات أرشيف مرقّمة داخل Gist أرشيف نشط —
    نفس منطق archive_overflow في scanner.py بالضبط. يرجع: (معرف Gist الأرشيف
    المستهدف, ملفات الأرشيف المطلوب كتابتها, ملفات إضافية للـGist الرئيسي
    [سلسلة الـGists + الفهرس] إن تغيّرت)."""
    main_updates = {}
    if not overflow_trades:
        return None, {}, main_updates

    target_gist_id, chain, archive_files = get_active_archive_gist(main_gist_files)

    try:
        old_chain_raw = _gist_get_file(ARCHIVE_CHAIN_FILE, main_gist_files)
        old_chain = json.loads(old_chain_raw) if old_chain_raw else []
    except Exception:
        old_chain = []
    if chain != old_chain:
        main_updates[ARCHIVE_CHAIN_FILE] = json.dumps(chain, ensure_ascii=False)

    archive_names = sorted(fn for fn in archive_files if fn.startswith(ARCHIVE_PREFIX))
    if archive_names:
        last_name = archive_names[-1]
        idx = int(last_name[len(ARCHIVE_PREFIX):].replace(".json", ""))
        try:
            last_content = json.loads(archive_files[last_name].get("content") or "[]")
        except Exception:
            last_content = []
    else:
        idx = 1
        last_content = []

    files_to_write = {}
    remaining = list(overflow_trades)

    space = ARCHIVE_CHUNK_SIZE - len(last_content)
    if space > 0 and remaining:
        last_content.extend(remaining[:space])
        remaining = remaining[space:]
        files_to_write[f"{ARCHIVE_PREFIX}{idx:04d}.json"] = json.dumps(
            last_content, ensure_ascii=False, separators=(",", ":")
        )

    while remaining:
        idx += 1
        chunk = remaining[:ARCHIVE_CHUNK_SIZE]
        remaining = remaining[ARCHIVE_CHUNK_SIZE:]
        files_to_write[f"{ARCHIVE_PREFIX}{idx:04d}.json"] = json.dumps(
            chunk, ensure_ascii=False, separators=(",", ":")
        )

    try:
        index_raw = _gist_get_file(ARCHIVE_INDEX_FILE, main_gist_files)
        index = json.loads(index_raw) if index_raw else {}
    except Exception:
        index = {}
    index[target_gist_id] = {
        "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "archive_files_count": len(archive_names) + len(files_to_write)
        - (1 if space > 0 and overflow_trades else 0),
    }
    main_updates[ARCHIVE_INDEX_FILE] = json.dumps(index, ensure_ascii=False)

    return target_gist_id, files_to_write, main_updates


def _gist_patch_files(files_dict, gist_id=None):
    """يحفظ عدة ملفات دفعة واحدة داخل Gist معيّن (افتراضيًا PAPER_GIST_ID، أو أي
    Gist أرشيف آخر لو مُرِّر gist_id صراحة — يُستخدم عند تدوير الأرشيف)."""
    target_id = gist_id or PAPER_GIST_ID
    if not PAPER_GIST_TOKEN or not target_id:
        print("⚠️ [محفظة وهمية] لا يمكن الحفظ — التوكن أو معرف الـGist غير موجودين.")
        return False
    payload = {"files": {fn: {"content": content} for fn, content in files_dict.items()}}
    last_err = None
    for attempt in range(1, 4):
        try:
            r = requests.patch(
                f"https://api.github.com/gists/{target_id}",
                headers=_gist_headers(), json=payload, timeout=20,
            )
            if r.ok:
                return True
            if r.status_code == 429:
                time.sleep(5 * attempt)
                continue
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            last_err = str(e)
        if attempt < 3:
            time.sleep(2 * attempt)
    print(f"❌ [محفظة وهمية] فشل الحفظ في Gist ({target_id}) نهائيًا: {last_err}")
    return False


# ---------------- تحميل/حفظ الحالة ----------------

def load_balance(gist_files):
    content = _gist_get_file(BALANCE_FILE, gist_files)
    if content:
        try:
            data = json.loads(content)
            if data:
                return data
        except Exception:
            pass
    # أول تشغيل (أو ملف فارغ {}) → تهيئة برأس المال الكامل
    return {
        "initial_capital": PAPER_CAPITAL,
        "available": PAPER_CAPITAL,
        "realized_pnl_usd": 0.0,
        "wins": 0,
        "losses": 0,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def load_positions(gist_files):
    content = _gist_get_file(POSITIONS_FILE, gist_files)
    if not content:
        return []
    try:
        return json.loads(content) or []
    except Exception:
        print("⚠️ [محفظة وهمية] paper_positions.json تالف — التعامل معه كأنه فارغ.")
        return []


def _save_state(balance, positions, closed_now, gist_files):
    files = {
        BALANCE_FILE: json.dumps(balance, ensure_ascii=False, separators=(",", ":")),
        POSITIONS_FILE: json.dumps(positions, ensure_ascii=False, separators=(",", ":")),
    }
    if closed_now:
        try:
            history = json.loads(_gist_get_file(CLOSED_FILE, gist_files) or "[]")
        except Exception:
            history = []
        history.extend(closed_now)

        # لو تجاوز السجل النشط الحد، تُرحَّل أقدم الصفقات لملفات الأرشيف بدل حذفها
        # نهائيًا — بنفس منطق save_all_state في scanner.py بالضبط. لا يُفقد أي شيء،
        # والسجل النشط (CLOSED_FILE) يبقى دائمًا صغيرًا وسريع القراءة.
        if len(history) > ACTIVE_HISTORY_SIZE:
            overflow = history[:-ACTIVE_HISTORY_SIZE]
            history = history[-ACTIVE_HISTORY_SIZE:]
            archive_gist_id, archive_files, main_updates = archive_overflow(overflow, gist_files)
            files.update(main_updates)  # سلسلة الـGists + الفهرس، تُحفظ بالـGist الرئيسي
            if archive_gist_id and archive_files:
                if archive_gist_id == PAPER_GIST_ID:
                    files.update(archive_files)  # نفس الـGist الرئيسي، تُدمج بحفظة واحدة
                else:
                    archived_ok = _gist_patch_files(archive_files, gist_id=archive_gist_id)
                    if not archived_ok:
                        print(f"❌ [محفظة وهمية] فشل حفظ ملفات الأرشيف بـGist منفصل ({archive_gist_id})")

        files[CLOSED_FILE] = json.dumps(history, ensure_ascii=False, separators=(",", ":"))
    _gist_patch_files(files)


def load_full_closed_history():
    """يجمع السجل النشط (paper_closed_trades.json) + كل ملفات الأرشيف (سواء داخل
    نفس الـGist الرئيسي أو موزّعة على Gists أرشيف منفصلة عبر paper_archive_gists_chain.json)
    في لقطة واحدة كاملة — لأي تقرير/سكربت مستقبلي على صفقات المحفظة الوهمية، حتى
    لا يكتفي بالسجل النشط الأخير فقط (نفس القاعدة المتبعة مع صفقات البوت الحقيقي)."""
    try:
        main_files = _gist_get_all_files()
    except GistFetchError as e:
        raise SystemExit(f"❌ [محفظة وهمية] تعذّر جلب الـGist الرئيسي: {e}")

    all_trades = []
    try:
        chain_raw = _gist_get_file(ARCHIVE_CHAIN_FILE, main_files)
        chain = json.loads(chain_raw) if chain_raw else []
    except Exception:
        chain = []

    seen_gist_ids = set()
    for gist_id in chain:
        if gist_id in seen_gist_ids:
            continue
        seen_gist_ids.add(gist_id)
        try:
            files = main_files if gist_id == PAPER_GIST_ID else _gist_get_all_files_for(gist_id)
        except Exception as e:
            print(f"⚠️ [محفظة وهمية] تعذّر جلب Gist الأرشيف {gist_id} ضمن السلسلة: {e} — قد تكون البيانات ناقصة.")
            continue
        for name in sorted(fn for fn in files if fn.startswith(ARCHIVE_PREFIX)):
            try:
                all_trades.extend(json.loads(files[name].get("content") or "[]"))
            except Exception:
                pass

    if PAPER_GIST_ID not in seen_gist_ids:
        for name in sorted(fn for fn in main_files if fn.startswith(ARCHIVE_PREFIX)):
            try:
                all_trades.extend(json.loads(main_files[name].get("content") or "[]"))
            except Exception:
                pass

    try:
        active = json.loads(_gist_get_file(CLOSED_FILE, main_files) or "[]")
    except Exception:
        active = []
    all_trades.extend(active)
    return all_trades


# ---------------- Telegram (بوت منفصل خاص بالمحفظة الوهمية) ----------------

def send_telegram(text, retries=2):
    if not PAPER_TELEGRAM_TOKEN or not PAPER_TELEGRAM_CHAT_ID:
        print("⚠️ [محفظة وهمية] PAPER_TELEGRAM_TOKEN/CHAT_ID غير موجودين — تخطي الإرسال.")
        return None
    url = f"https://api.telegram.org/bot{PAPER_TELEGRAM_TOKEN}/sendMessage"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, data={"chat_id": PAPER_TELEGRAM_CHAT_ID, "text": text}, timeout=15)
            if resp.ok:
                return resp.json().get("result", {}).get("message_id")
        except Exception as e:
            print(f"[محفظة وهمية] خطأ إرسال تيليجرام (محاولة {attempt}): {e}")
        if attempt < retries:
            time.sleep(2)
    return None


def _escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _edit_telegram_strike(message_id, original_text, result_text):
    if not PAPER_TELEGRAM_TOKEN or not PAPER_TELEGRAM_CHAT_ID or not message_id:
        return
    new_text = f"<s>{_escape_html(original_text)}</s>\n\n{_escape_html(result_text)}"
    try:
        requests.post(
            f"https://api.telegram.org/bot{PAPER_TELEGRAM_TOKEN}/editMessageText",
            data={"chat_id": PAPER_TELEGRAM_CHAT_ID, "message_id": message_id,
                  "text": new_text, "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception as e:
        print(f"[محفظة وهمية] خطأ تعديل رسالة تيليجرام: {e}")


# ---------------- أدوات مساعدة ----------------

def _hours_since(opened_at_str):
    try:
        t = time.strptime(opened_at_str, "%Y-%m-%d %H:%M:%S")
        return (time.time() - time.mktime(t)) / 3600
    except Exception:
        return 0


def _parse_opened_epoch_ms(opened_at_str):
    try:
        t = time.strptime(opened_at_str, "%Y-%m-%d %H:%M:%S")
        return int(time.mktime(t) * 1000)
    except Exception:
        return None


def _format_duration(hours):
    total_minutes = round(hours * 60)
    days, rem = divmod(total_minutes, 24 * 60)
    hrs, minutes = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} يوم" if days > 1 else "يوم")
    if hrs:
        parts.append(f"{hrs} ساعة")
    if not parts:
        parts.append(f"{minutes} دقيقة")
    return " و".join(parts)


TYPE_LABELS = {
    "official": "🟢 رسمية",
    "early": "🟡 مبكرة",
    "breakout": "🚀 انفجار",
    "experimental": "🧪 تجريبية",
}


def _normalize_signal(r, sig_type):
    """يستخرج entry/sl/tps/score من قاموس الإشارة r حسب نوعها — نفس الحقول التي
    يستخدمها scanner.py في open_new_*_positions."""
    if sig_type == "official":
        entry, sl, tps, score = r.get("entry"), r.get("sl"), r.get("tps"), r.get("score")
    elif sig_type == "early":
        entry, sl, tps, score = r.get("early_entry"), r.get("early_sl"), r.get("early_tps"), r.get("score")
    elif sig_type == "breakout":
        entry, sl, tps, score = r.get("breakout_entry"), r.get("breakout_sl"), r.get("breakout_tps"), r.get("breakout_score")
    elif sig_type == "experimental":
        entry, sl, tps, score = r.get("experimental_entry"), r.get("experimental_sl"), r.get("experimental_tps"), r.get("experimental_score")
    else:
        return None
    if entry is None or sl is None or not tps:
        return None
    return {"symbol": r["symbol"], "entry": entry, "sl": sl, "tps": tps, "score": score, "type": sig_type}


# ---------------- رسائل Telegram ----------------

def _format_open(pos):
    label = TYPE_LABELS.get(pos["type"], pos["type"])
    tp1 = pos["tps"][0]
    pct_target = (tp1 - pos["entry"]) / pos["entry"] * 100
    return (
        f"💼 دخول وهمي — {label}\n"
        f"{pos['symbol'].replace('USDT', '/USDT')}\n"
        f"الدخول: {pos['entry']:.6g}\n"
        f"SL: {pos['sl']:.6g}\n"
        f"TP1: {tp1:.6g} (+{pct_target:.2f}%)\n"
        f"حجم الصفقة: ${pos['size_usd']:.2f}"
    )


def _format_close(pos, exit_price, reason, pnl_usd, net_pct, balance, open_count):
    label = TYPE_LABELS.get(pos["type"], pos["type"])
    icon = "✅" if pnl_usd >= 0 else "❌"
    reason_ar = {"TP1": "تحقق الهدف TP1", "SL": "ضرب وقف الخسارة", "EXPIRED": "انتهت صلاحية المراقبة (سقف زمني)"}[reason]
    duration = _format_duration(_hours_since(pos["opened_at"]))
    return (
        f"{icon} إغلاق وهمي — {label} — {reason_ar}\n"
        f"{pos['symbol'].replace('USDT', '/USDT')}\n"
        f"الدخول: {pos['entry']:.6g} | الخروج: {exit_price:.6g}\n"
        f"النتيجة الصافية: {net_pct:+.2f}% → {pnl_usd:+.2f}$\n"
        f"المدة: {duration}\n"
        f"الرصيد المتاح الآن: ${balance['available']:.2f} | صفقات مفتوحة: {open_count}"
    )


# ---------------- إغلاق الصفقات (TP1 / SL / سقف زمني) ----------------

def _check_positions(positions, balance, price_map, monitor_map):
    still_open, closed_now = [], []
    monitor_map = monitor_map or {}

    for pos in positions:
        symbol = pos["symbol"]
        price = price_map.get(symbol)
        candles = monitor_map.get(symbol) or []
        if price is None and candles:
            try:
                price = float(candles[-1][4])
            except Exception:
                price = None
        if price is None:
            still_open.append(pos)
            continue

        opened_ms = _parse_opened_epoch_ms(pos.get("opened_at")) or 0
        try:
            cursor_ms = int(pos.get("monitor_cursor_ms", opened_ms))
        except (TypeError, ValueError):
            cursor_ms = opened_ms

        events = []
        for k in candles:
            try:
                candle_open_ms = int(k[0])
                if candle_open_ms <= cursor_ms:
                    continue
                events.append((candle_open_ms, float(k[2]), float(k[3])))  # open_ms, high, low
            except Exception:
                continue

        tp = pos["tps"][0] if pos.get("tps") else None
        closed, exit_price, reason, close_ms = False, None, None, None

        for candle_open_ms, high, low in events:
            if low <= pos["sl"]:
                exit_price, reason, close_ms, closed = pos["sl"], "SL", candle_open_ms, True
                break
            if tp is not None and high >= tp:
                exit_price, reason, close_ms, closed = tp, "TP1", candle_open_ms, True
                break

        if not closed and events:
            pos["monitor_cursor_ms"] = events[-1][0]

        if not closed:
            hours_open = _hours_since(pos["opened_at"])
            if hours_open >= PAPER_TIME_STOP_HOURS:
                exit_price, reason, closed = price, "EXPIRED", True
                close_ms = None

        if not closed:
            still_open.append(pos)
            continue

        pct_change = (exit_price - pos["entry"]) / pos["entry"] * 100
        net_pct = pct_change - PAPER_TRADING_FEE_PCT
        pnl_usd = round(pos["size_usd"] * net_pct / 100, 8)

        balance["available"] = round(balance["available"] + pos["size_usd"] + pnl_usd, 8)
        balance["realized_pnl_usd"] = round(balance.get("realized_pnl_usd", 0.0) + pnl_usd, 8)
        if pnl_usd >= 0:
            balance["wins"] = balance.get("wins", 0) + 1
        else:
            balance["losses"] = balance.get("losses", 0) + 1
        balance["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

        pos["closed_reason"] = reason
        pos["closed_at"] = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(close_ms / 1000))
            if close_ms else time.strftime("%Y-%m-%d %H:%M:%S")
        )
        pos["exit_price"] = exit_price
        pos["pnl_pct"] = round(net_pct, 4)
        pos["pnl_usd"] = pnl_usd

        remaining_open = len(still_open) + (len(positions) - positions.index(pos) - 1)  # تقدير سريع لعرضه بالرسالة
        result_text = _format_close(pos, exit_price, reason, pnl_usd, net_pct, balance,
                                     open_count=max(0, sum(1 for p in positions if p not in closed_now) - 1))
        send_telegram(result_text)
        _edit_telegram_strike(pos.get("alert_message_id"), pos.get("alert_text", ""), result_text)

        closed_now.append(pos)

    return still_open, closed_now


# ---------------- فتح صفقات جديدة ----------------

def _open_new(positions, balance, fresh, fresh_early, fresh_breakout, fresh_experimental):
    candidates = []
    for r in fresh:
        candidates.append((r, "official"))
    for r in fresh_early:
        candidates.append((r, "early"))
    for r in fresh_breakout:
        candidates.append((r, "breakout"))
    for r in fresh_experimental:
        candidates.append((r, "experimental"))

    for r, sig_type in candidates:
        sig = _normalize_signal(r, sig_type)
        if sig is None:
            continue

        if len(positions) >= PAPER_MAX_POSITIONS:
            print(f"[محفظة وهمية] الحد الأقصى للصفقات المفتوحة ({PAPER_MAX_POSITIONS}) — "
                  f"تفويت {sig['symbol']} ({sig_type})")
            continue
        if balance["available"] < PAPER_POSITION_SIZE - 1e-9:
            print(f"[محفظة وهمية] رصيد غير كافٍ (${balance['available']:.2f}) — "
                  f"تفويت {sig['symbol']} ({sig_type})")
            continue

        balance["available"] = round(balance["available"] - PAPER_POSITION_SIZE, 8)
        pos = {
            "symbol": sig["symbol"],
            "entry": sig["entry"],
            "sl": sig["sl"],
            "tps": sig["tps"],
            "score": sig["score"],
            "type": sig_type,
            "size_usd": PAPER_POSITION_SIZE,
            "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        alert_text = _format_open(pos)
        pos["alert_message_id"] = send_telegram(alert_text)
        pos["alert_text"] = alert_text
        positions.append(pos)
        time.sleep(1)  # نفس احتياط حد تيليجرام المستخدم في السكانر الأصلي


# ---------------- نقطة الدخول: تُستدعى مرتين من scanner.py لكل دورة ----------------
# (1) load_context() + get_open_symbols() مبكرًا، قبل بناء خريطة شموع المراقبة في
#     scanner.py، عشان رموز صفقات المحفظة الوهمية المفتوحة تنضم لنفس الخريطة
#     المستخدمة للصفقات الحقيقية — بدل ما تعتمد لاحقًا على آخر سعر ticker فقط
#     وتفوّت TP/SL حصل وارتد بين تشغيلتين متباعدتين (نفس المشكلة التي صُممت
#     خريطة المراقبة أصلاً لتفاديها للصفقات الحقيقية).
# (2) run_cycle(gist_files, ...) لاحقًا، بنفس gist_files المُجلب في load_context()
#     — بدون إعادة جلب نفس الـGist مرة ثانية.

def load_context():
    """يجلب ملفات Gist المحفظة الوهمية مرة واحدة فقط لكل دورة. الناتج يُعاد
    استخدامه في run_cycle لاحقًا. يرجع None بهدوء عند أي فشل جلب — لا يوقف
    تشغيل السكانر الحقيقي أبدًا."""
    try:
        return _gist_get_all_files()
    except GistFetchError as e:
        print(f"❌ [محفظة وهمية] فشل جلب Gist ({e}) — تخطي دورة المحفظة الوهمية هذه المرة.")
        return None


def get_open_symbols(gist_files):
    """يرجع الصفقات المفتوحة حاليًا بالمحفظة الوهمية (لضمها لخريطة شموع المراقبة
    قبل بنائها في scanner.py). قائمة فارغة بهدوء لو gist_files غير متاح."""
    if gist_files is None:
        return []
    try:
        return load_positions(gist_files)
    except Exception as e:
        print(f"⚠️ [محفظة وهمية] تعذّر قراءة صفقاتها المفتوحة لضمها لخريطة المراقبة: {e}")
        return []


def run_cycle(gist_files, price_map, monitor_map, fresh, fresh_early, fresh_breakout, fresh_experimental):
    """
    يُستدعى مرة واحدة من داخل scanner.py في main()، بعد حساب fresh/fresh_early/
    fresh_breakout/fresh_experimental وبعد أن يصبح price_map وmonitor_map جاهزين.
    مستقل بالكامل عن حالة السكانر الحقيقي (Gist وبوت تيليجرام مختلفان تمامًا).
    gist_files: نفس القاموس المُجلب مسبقًا عبر load_context() في بداية main() —
    وليس جلبًا جديدًا، لتوفير طلب API مكرر على نفس الـGist كل دورة.
    """
    if gist_files is None:
        return

    balance = load_balance(gist_files)
    positions = load_positions(gist_files)

    positions, closed_now = _check_positions(positions, balance, price_map, monitor_map)
    _open_new(positions, balance, fresh, fresh_early, fresh_breakout, fresh_experimental)

    print(f"💼 [محفظة وهمية] رصيد متاح: ${balance['available']:.2f} | صفقات مفتوحة: {len(positions)} "
          f"| أُغلقت الآن: {len(closed_now)} | ربح/خسارة متراكم: {balance.get('realized_pnl_usd', 0):+.2f}$")

    _save_state(balance, positions, closed_now, gist_files)
