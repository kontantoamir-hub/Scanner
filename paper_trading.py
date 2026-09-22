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
PAPER_TELEGRAM_TOKEN = os.environ.get("PAPER_TELEGRAM_TOKEN", "8819201723:AAHR8TXjCQf03BRt3P9lO2lAfLZhDOUJ7Jg")
PAPER_TELEGRAM_CHAT_ID = os.environ.get("PAPER_TELEGRAM_CHAT_ID", "1721516963")

# ---------------- Gist الخاص بالمحفظة الوهمية (منفصل كليًا عن Gist السكانر) ----------------
# GIST_TOKEN نفسه المستخدم في السكانر (توكن GitHub الشخصي بصلاحية gist) يُعاد استخدامه هنا
# تلقائيًا لأنه ينتمي لنفس الحساب ويملك صلاحية الكتابة على أي Gist خاص به — لا داعي لتوكن جديد،
# فقط PAPER_GIST_ID مختلف. يمكن التجاوز عبر PAPER_GIST_TOKEN لو احتجت توكن مختلف لاحقًا.
PAPER_GIST_TOKEN = os.environ.get("PAPER_GIST_TOKEN") or os.environ.get("GIST_TOKEN")
PAPER_GIST_ID = os.environ.get("PAPER_GIST_ID", "af82b35a4fde92f671d596bc6c18f4f2")

BALANCE_FILE = "paper_balance.json"
POSITIONS_FILE = "paper_positions.json"
CLOSED_FILE = "paper_closed_trades.json"
ACTIVE_HISTORY_SIZE = 150  # عدد الصفقات المغلقة المحفوظة بالسجل النشط قبل الاقتصاص (مثل السكانر الأصلي)


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


def _gist_patch_files(files_dict):
    if not PAPER_GIST_TOKEN or not PAPER_GIST_ID:
        print("⚠️ [محفظة وهمية] لا يمكن الحفظ — التوكن أو معرف الـGist غير موجودين.")
        return False
    payload = {"files": {fn: {"content": content} for fn, content in files_dict.items()}}
    last_err = None
    for attempt in range(1, 4):
        try:
            r = requests.patch(
                f"https://api.github.com/gists/{PAPER_GIST_ID}",
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
    print(f"❌ [محفظة وهمية] فشل الحفظ في Gist نهائيًا: {last_err}")
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
        history = history[-ACTIVE_HISTORY_SIZE:]
        files[CLOSED_FILE] = json.dumps(history, ensure_ascii=False, separators=(",", ":"))
    _gist_patch_files(files)


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


# ---------------- نقطة الدخول الوحيدة التي يستدعيها scanner.py ----------------

def run_cycle(price_map, monitor_map, fresh, fresh_early, fresh_breakout, fresh_experimental):
    """
    يُستدعى مرة واحدة من داخل scanner.py في main()، بعد حساب fresh/fresh_early/
    fresh_breakout/fresh_experimental وبعد أن يصبح price_map وmonitor_map جاهزين.
    مستقل بالكامل عن حالة السكانر الحقيقي (Gist وبوت تيليجرام مختلفان تمامًا)،
    وأي فشل بجلب/حفظ Gist المحفظة الوهمية لا يوقف السكانر الحقيقي أبدًا.
    """
    try:
        gist_files = _gist_get_all_files()
    except GistFetchError as e:
        print(f"❌ [محفظة وهمية] فشل جلب Gist ({e}) — تخطي دورة المحفظة الوهمية هذه المرة.")
        return

    balance = load_balance(gist_files)
    positions = load_positions(gist_files)

    positions, closed_now = _check_positions(positions, balance, price_map, monitor_map)
    _open_new(positions, balance, fresh, fresh_early, fresh_breakout, fresh_experimental)

    print(f"💼 [محفظة وهمية] رصيد متاح: ${balance['available']:.2f} | صفقات مفتوحة: {len(positions)} "
          f"| أُغلقت الآن: {len(closed_now)} | ربح/خسارة متراكم: {balance.get('realized_pnl_usd', 0):+.2f}$")

    _save_state(balance, positions, closed_now, gist_files)
