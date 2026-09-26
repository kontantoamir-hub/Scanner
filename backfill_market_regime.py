"""
backfill_market_regime.py
==========================
يضيف حقل "market_regime" بأثر رجعي لكل صفقة (رسمية/مبكرة/انفجار/تجريبية) فُتحت
اعتبارًا من BACKFILL_FROM (افتراضيًا 2026-09-17) ولا تملك الحقل بعد.

المنطق مطابق تمامًا لـ compute_market_regime() في scanner.py: ADX(14) + ميل EMA50
(10 شموع) على BTCUSDT بنفس إطار السكانر (INTERVAL) — لضمان أن القيم المحسوبة هنا
متوافقة تمامًا مع القيم التي سيسجلها scanner.py مستقبلاً للصفقات الجديدة.

يمسح: الملف النشط (closed_trades.json) + كل ملفات الأرشيف عبر سلسلة archive_gists_chain.json،
ويحفظ فقط الملفات التي تغيّرت فعليًا (لا يعيد كتابة كل شيء).

تشغيل: GIST_TOKEN و GIST_ID لازم يكونوا معرّفين بنفس القيم المستخدمة في scanner.py.
    python3 backfill_market_regime.py            # تنفيذ فعلي
    python3 backfill_market_regime.py --dry-run   # عرض فقط بدون حفظ أي شيء
"""

import sys
import json
import datetime as dt

import scanner  # يعيد استخدام: ema, adx, compute_market_regime, fetch_klines,
                 # drop_unclosed_candle, GIST_* , _gist_get_all_files, _gist_get_all_files_for,
                 # _gist_get_file, _gist_patch_files, ARCHIVE_CHAIN_FILE, ARCHIVE_PREFIX,
                 # CLOSED_GIST_FILE, INTERVAL

BACKFILL_FROM = dt.datetime(2026, 9, 17, 0, 0, 0)  # عدّل هنا لو أردت نطاقًا مختلفًا
MIN_CANDLES = scanner.MARKET_REGIME_EMA_PERIOD + scanner.MARKET_REGIME_SLOPE_LOOKBACK + 1


def parse_opened_at(value):
    """opened_at مخزّن بصيغة '%Y-%m-%d %H:%M:%S' (وقت التشغيلة على GitHub Actions = UTC)."""
    if not value:
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def fetch_btc_history(start_dt, end_dt):
    """
    يجلب شموع BTCUSDT (نفس INTERVAL المستخدم بالسكانر) تغطي من (start_dt - هامش
    كافٍ لـ MIN_CANDLES شمعة) حتى end_dt، بصفحات متتالية لو تجاوز العدد حد 1000/طلب.
    يرجع قائمة مرتبة زمنيًا من tuples: (open_time_ms, close_time_ms, close, high, low)
    """
    interval_hours = {"15m": 0.25, "1h": 1, "4h": 4, "1d": 24}.get(scanner.INTERVAL, 1)
    margin_hours = MIN_CANDLES * interval_hours * 1.5 + 24  # هامش أمان إضافي
    fetch_start = start_dt - dt.timedelta(hours=margin_hours)

    start_ms = int(fetch_start.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    all_candles = []
    cursor = start_ms
    while cursor < end_ms:
        r = scanner._request_with_retry(
            f"{scanner.BASE_URL}/klines",
            params={
                "symbol": "BTCUSDT",
                "interval": scanner.INTERVAL,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        batch = r.json()
        if not batch:
            break
        for k in batch:
            all_candles.append((k[0], k[6], float(k[4]), float(k[2]), float(k[3])))
        if len(batch) < 1000:
            break
        cursor = batch[-1][6] + 1  # بعد close_time لآخر شمعة بالدفعة

    all_candles.sort(key=lambda c: c[0])
    return all_candles


def regime_at(candles, target_dt):
    """
    يبحث عن آخر شمعة أُغلقت في target_dt أو قبله، ويحسب market_regime من آخر
    MIN_CANDLES شمعة تنتهي عندها (نفس منطق fetch_market_regime لكن بأثر رجعي).
    """
    target_ms = int(target_dt.timestamp() * 1000)
    # آخر index تكون شمعته مغلقة (close_time <= target_ms)
    idx = None
    for i, (_, close_time, *_r) in enumerate(candles):
        if close_time <= target_ms:
            idx = i
        else:
            break
    if idx is None or idx + 1 < MIN_CANDLES:
        return None
    window = candles[idx + 1 - MIN_CANDLES: idx + 1]
    closes = [c[2] for c in window]
    highs = [c[3] for c in window]
    lows = [c[4] for c in window]
    return scanner.compute_market_regime(closes, highs, lows)


def backfill_trade_list(trades, candles, stats):
    """يعدّل trades في المكان (in place). يرجع True لو أي صفقة تغيّرت فعليًا."""
    changed = False
    for t in trades:
        if t.get("market_regime") is not None:
            continue
        opened = parse_opened_at(t.get("opened_at"))
        if opened is None or opened < BACKFILL_FROM:
            continue
        regime = regime_at(candles, opened)
        stats["checked"] += 1
        if regime is not None:
            t["market_regime"] = regime
            stats["filled"] += 1
            changed = True
        else:
            stats["skipped_no_data"] += 1
    return changed


def get_full_content(file_meta):
    """
    يرجع المحتوى الكامل لملف Gist. GitHub API يبتر content ويضع truncated=true
    لو الملف تجاوز حد معيّن (لاحظنا ذلك عمليًا مع ملفات أرشيف أكبر من ~50KB) —
    فبنجيب المحتوى الكامل من raw_url في هذه الحالة بدل الاكتفاء بـcontent المبتور
    (اللي كان بيسبب JSONDecodeError: Unterminated string).
    """
    if not file_meta:
        return None
    if file_meta.get("truncated") and file_meta.get("raw_url"):
        r = scanner.requests.get(file_meta["raw_url"], timeout=20)
        r.raise_for_status()
        return r.text
    return file_meta.get("content")


def load_json_file(files_dict, filename, default):
    meta = files_dict.get(filename)
    if meta is None:
        return default
    content = get_full_content(meta)
    if not content:
        return default
    return json.loads(content)


def main():
    dry_run = "--dry-run" in sys.argv
    print(f"🔎 Backfill market_regime — من {BACKFILL_FROM} حتى الآن | dry_run={dry_run}")

    now = dt.datetime.utcnow()
    print("📥 جلب تاريخ BTCUSDT ...")
    candles = fetch_btc_history(BACKFILL_FROM, now)
    print(f"✅ {len(candles)} شمعة BTCUSDT محمّلة ({scanner.INTERVAL})")
    if len(candles) < MIN_CANDLES:
        print("❌ بيانات BTCUSDT غير كافية لحساب market_regime — توقف.")
        return

    print("📥 جلب ملفات Gist الرئيسي ...")
    main_files = scanner._gist_get_all_files()

    stats = {"checked": 0, "filled": 0, "skipped_no_data": 0}
    files_to_save_main = {}
    archive_saves = {}  # gist_id -> {filename: content}

    # 1) السجل النشط
    active_trades = load_json_file(main_files, scanner.CLOSED_GIST_FILE, [])
    print(f"📋 السجل النشط: {len(active_trades)} صفقة")
    if backfill_trade_list(active_trades, candles, stats):
        files_to_save_main[scanner.CLOSED_GIST_FILE] = json.dumps(
            active_trades, ensure_ascii=False, separators=(",", ":")
        )

    # 2) سلسلة الأرشيف بالكامل
    chain = load_json_file(main_files, scanner.ARCHIVE_CHAIN_FILE, [])
    print(f"🔗 سلسلة الأرشيف: {len(chain)} Gist")

    for gist_id in chain:
        archive_files = main_files if gist_id == scanner.GIST_ID else scanner._gist_get_all_files_for(gist_id)
        archive_names = sorted(fn for fn in archive_files if fn.startswith(scanner.ARCHIVE_PREFIX))
        for fn in archive_names:
            trades = load_json_file(archive_files, fn, [])
            if backfill_trade_list(trades, candles, stats):
                archive_saves.setdefault(gist_id, {})[fn] = json.dumps(
                    trades, ensure_ascii=False, separators=(",", ":")
                )
        print(f"   • {gist_id}: {len(archive_names)} ملف أرشيف مفحوص")

    print(f"\n📊 النتيجة: فُحصت {stats['checked']} صفقة مؤهلة | "
          f"عُبّئت {stats['filled']} | تعذّر (بيانات ناقصة) {stats['skipped_no_data']}")

    if dry_run:
        print("🧪 dry-run: لن يُحفظ أي شيء.")
        return

    if files_to_save_main:
        print(f"💾 حفظ الملف النشط بالـGist الرئيسي ({scanner.GIST_ID}) ...")
        scanner._gist_patch_files(files_to_save_main)

    for gist_id, files in archive_saves.items():
        print(f"💾 حفظ {len(files)} ملف أرشيف بالـGist {gist_id} ...")
        scanner._gist_patch_files(files, gist_id=gist_id)

    print("✅ انتهى.")


if __name__ == "__main__":
    main()
