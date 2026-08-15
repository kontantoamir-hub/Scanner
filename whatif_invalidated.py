"""
whatif_invalidated.py — تحليل "ماذا لو" لصفقات INVALIDATED (انعكاس الاتجاه)

لكل صفقة أُغلقت بسبب "انعكس الاتجاه قبل تحقيق أي هدف"، يجلب هذا السكربت بيانات
السعر التاريخية من Binance بعد لحظة الإغلاق، ويحاكي: لو تركنا الصفقة مفتوحة فعلاً
(بنفس السقف الزمني TIME_STOP_HOURS المستخدم بالبوت)، هل كانت ستصل لاحقًا للهدف
الأول أم لضربة الستوب لوس الأصلي أم تنتهي صلاحيتها بدون أي منهما؟

النتيجة تجاوبك عمليًا: هل خروج "انعكاس الاتجاه" المبكر يحميك غالبًا (كان سيضرب
ستوب أصلاً) أم يفوّت عليك أرباح غالبًا (كان سيحقق هدف لاحقًا = whipsaw مؤقت)؟

هذا سكريبت تحليل للقراءة فقط — لا يعدّل أي شيء في closed_trades.json ولا بمنطق
البوت. يُفضّل تشغيله يدويًا (مو ضمن جدولة scan.yml) لأنه يستهلك طلبات إضافية
لـ Binance API بعدد الصفقات المحايدة.

المتغيرات المطلوبة: GIST_TOKEN, GIST_ID
اختياري: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (لإرسال الملخص)
"""

import os
import json
import time
import datetime as dt
import requests

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")
CLOSED_GIST_FILE = "closed_trades.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BASE_URL = "https://api.binance.com/api/v3"
TIME_STOP_HOURS = float(os.environ.get("TIME_STOP_HOURS", "96"))  # نفس القيمة الافتراضية في scanner.py

INTERVAL_HOURS = {
    "1m": 1 / 60, "3m": 3 / 60, "5m": 5 / 60, "15m": 0.25, "30m": 0.5,
    "1h": 1, "2h": 2, "4h": 4, "6h": 6, "8h": 8, "12h": 12,
    "1d": 24, "3d": 72, "1w": 168,
}


def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def load_closed_trades():
    if not GIST_TOKEN or not GIST_ID:
        raise SystemExit("❌ GIST_TOKEN أو GIST_ID غير موجودين في متغيرات البيئة.")
    r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=_gist_headers(), timeout=15)
    r.raise_for_status()
    files = r.json().get("files", {})
    if CLOSED_GIST_FILE not in files:
        return []
    return json.loads(files[CLOSED_GIST_FILE]["content"])


def fetch_klines_after(symbol, interval, start_ms, limit=500):
    """يجلب الشموع مع إعادة محاولة تلقائية، بنفس منطق _request_with_retry في scanner.py."""
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(
                f"{BASE_URL}/klines",
                params={"symbol": symbol, "interval": interval, "startTime": start_ms, "limit": limit},
                timeout=20,
            )
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.exceptions.HTTPError(f"status {r.status_code}")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise last_err


def simulate_trade(trade):
    """
    يرجع أحد: 'would_tp' (كان سيحقق الهدف التالي)، 'would_sl' (كان سيضرب الستوب
    الأصلي فعلاً)، 'undecided' (ولا واحد خلال الوقت المتبقي — كان سينتهي EXPIRED)،
    أو None لو تعذّر الجلب أو بيانات ناقصة.
    """
    symbol = trade.get("symbol")
    interval = trade.get("interval", "1h")
    sl = trade.get("sl")
    tps = trade.get("tps") or []
    hit_tps = trade.get("hit_tps") or []

    next_tp = None
    for i, tp in enumerate(tps):
        if i not in hit_tps:
            next_tp = tp
            break
    if next_tp is None or sl is None:
        print(f"[تخطي] {symbol}: بيانات ناقصة (tps أو sl فارغة)")
        return None

    try:
        opened_at = dt.datetime.strptime(trade["opened_at"], "%Y-%m-%d %H:%M:%S")
        closed_at = dt.datetime.strptime(trade["closed_at"], "%Y-%m-%d %H:%M:%S")
    except Exception as e:
        print(f"[تخطي] {symbol}: تعذّر قراءة التاريخ ({e})")
        return None

    elapsed_hours = (closed_at - opened_at).total_seconds() / 3600
    remaining_hours = TIME_STOP_HOURS - elapsed_hours
    if remaining_hours <= 0:
        return "undecided"  # كان أصلاً وصل السقف الزمني وقت الانعكاس

    candle_hours = INTERVAL_HOURS.get(interval, 1)
    needed_candles = min(1000, int(remaining_hours / candle_hours) + 2)

    start_ms = int(closed_at.timestamp() * 1000)
    try:
        klines = fetch_klines_after(symbol, interval, start_ms, limit=needed_candles)
    except Exception as e:
        print(f"[تخطي] {symbol}: فشل جلب البيانات من Binance ({e})")
        return None

    for k in klines:
        high, low = float(k[2]), float(k[3])
        hit_sl = low <= sl
        hit_tp = high >= next_tp
        if hit_sl and hit_tp:
            # نفس الشمعة لمست الاثنين — لا نعرف الترتيب الفعلي، نفترض الأسوأ احترازيًا
            return "would_sl"
        if hit_sl:
            return "would_sl"
        if hit_tp:
            return "would_tp"

    return "undecided"


def build_report(results):
    total = len(results)
    if total == 0:
        return "لا توجد صفقات INVALIDATED كافية للتحليل بعد."

    would_tp = sum(1 for r in results if r == "would_tp")
    would_sl = sum(1 for r in results if r == "would_sl")
    undecided = sum(1 for r in results if r == "undecided")

    lines = [
        "🔎 تحليل \"ماذا لو\" لصفقات انعكاس الاتجاه (INVALIDATED)",
        f"الإجمالي المُحلَّل: {total} صفقة",
        f"🟢 كانت ستحقق هدفًا لو تُركت (whipsaw / خروج مبكر أضاع ربحًا): {would_tp} ({would_tp/total*100:.0f}%)",
        f"🔴 كانت ستضرب الستوب الأصلي فعلاً (الخروج المبكر كان صحيحًا): {would_sl} ({would_sl/total*100:.0f}%)",
        f"⚪ لا هذا ولا ذاك خلال الوقت المتبقي (كانت ستنتهي EXPIRED): {undecided} ({undecided/total*100:.0f}%)",
        "",
    ]

    if would_tp > would_sl:
        lines.append("📌 الخلاصة: أغلب حالات انعكاس الاتجاه كانت whipsaw مؤقت — يستاهل تفكر بتخفيف حساسية الإغلاق المبكر.")
    elif would_sl > would_tp:
        lines.append("📌 الخلاصة: أغلب حالات انعكاس الاتجاه كانت انعكاسًا حقيقيًا — الخروج المبكر الحالي يحمي رأس المال فعلاً، أفضل تركه كما هو.")
    else:
        lines.append("📌 الخلاصة: النتائج متقاربة — لا يوجد اتجاه واضح بعد، يُفضّل جمع بيانات أكثر قبل تغيير المنطق.")

    return "\n".join(lines)


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:3800]}, timeout=15)
    except Exception as e:
        print("تعذّر إرسال التقرير عبر تيليجرام:", e)


def main():
    trades = load_closed_trades()
    invalidated = [t for t in trades if t.get("closed_reason") == "INVALIDATED"]

    print(f"عدد صفقات INVALIDATED الموجودة: {len(invalidated)}")

    results = []
    per_trade_lines = []
    for t in invalidated:
        res = simulate_trade(t)
        if res is None:
            continue
        results.append(res)
        label = {"would_tp": "🟢 كان سيربح", "would_sl": "🔴 كان سيخسر", "undecided": "⚪ غير محسوم"}[res]
        per_trade_lines.append(f"{t.get('symbol','?')} | أُغلقت في {t.get('closed_at','?')} | {label}")
        time.sleep(0.3)  # تخفيف الضغط على Binance API

    report = build_report(results)
    print("\n" + report)
    print("\n— تفصيل كل صفقة —")
    for line in per_trade_lines:
        print(line)

    send_telegram(report)


if __name__ == "__main__":
    main()
