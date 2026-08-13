"""
اختبار رجعي (Backtesting) — يعيد تشغيل نفس منطق البوت الحي على بيانات تاريخية من Binance،
لمعرفة كيف كانت ستؤدي الاستراتيجية لو كانت تعمل فعلاً خلال تلك الفترة.

يقارن أيضًا الأداء مع الفلاتر الأربعة الجديدة (ADX / انحراف / مقاومة / OBV) مقابل بدونها،
للتحقق هل تحسّن جودة الإشارات فعلاً أم لا.

تعديلات عن النسخة الأصلية:
  1) خصم رسوم التداول (FEE_PCT لكل جهة) من نتيجة كل صفقة، لمطابقة الواقع.
  2) محاكاة أسباب إغلاق إضافية موجودة في البوت الحي وغائبة سابقًا عن الاختبار الرجعي:
     انعكاس الاتجاه (EMA9/21) والسقف الزمني (TIME_STOP_HOURS). بدون هذا، كانت الصفقات
     التي لا تصل لا لآخر TP ولا لـ SL تبقى "معلّقة" وتُستبعد بصمت من الإحصائيات، مما
     يجعل نسبة النجاح متفائلة بشكل غير واقعي.
  3) تقرير عدد الصفقات التي بقيت مفتوحة بلا حسم حتى نهاية فترة الاختبار (unresolved)،
     حتى تُعرف حدود موثوقية الأرقام المعروضة.
  4) توسيع العينة الافتراضية (365 يوم / 30 عملة بدل 180 يوم / 15 عملة) لتقليل أثر الضجيج
     الإحصائي على عملة أو فترة بعينها.
  5) اختبار عدة عتبات دخول (SCORE_THRESHOLDS) في نفس التشغيلة — بدل عتبة 2.5 الثابتة —
     لمعرفة هل تشديد شرط الدخول يحسّن EV، مع جدول مقارنة نهائي بين كل العتبات.

تشغيل يدوي فقط (لا يعمل بجدولة تلقائية):
    python backtest.py
"""

import os
import bisect
import time

from scanner import (
    compute_indicators, score_at,
    fetch_ticker24h,
    EXCLUDE_SUFFIX, EXCLUDE_SYMS, LIQUIDITY_FLOOR, HTF_MAP,
    BASE_URL, _request_with_retry, TIME_STOP_HOURS,
)

# ---------- إعدادات الاختبار الرجعي ----------
BACKTEST_INTERVAL = os.environ.get("BACKTEST_INTERVAL", "1h")
BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "365"))          # مدة الفترة المُختبَرة بالأيام (وُسّعت من 180)
BACKTEST_SYMBOL_COUNT = int(os.environ.get("BACKTEST_SYMBOL_COUNT", "30"))  # عدد العملات (وُسّع من 15)
WARMUP_CANDLES = 250  # عدد الشموع الأولى المستخدمة فقط لتهيئة المؤشرات (لا تُستخدم كإشارات)

# نسبة العمولة لكل جهة (%) — الافتراضي 0.1% يطابق Taker العادي على Binance Spot.
# رسوم الصفقة الكاملة (دخول + خروج) = FEE_PCT * 2
FEE_PCT = float(os.environ.get("FEE_PCT", "0.1"))

# عتبات درجة الدخول المراد اختبارها في نفس التشغيلة (تُطبَّق فقط على نسخة "مع الفلاتر"،
# لأنها أثبتت أداءً أفضل في الاختبارات السابقة). القيمة الأولى (2.5) هي الأساس الحالي في scanner.py.
SCORE_THRESHOLDS = [float(x) for x in os.environ.get("SCORE_THRESHOLDS", "2.5,3.0,3.5,4.0").split(",")]


# ---------------- جلب بيانات تاريخية طويلة (تتجاوز حد الـ1000 شمعة لكل طلب) ----------------
def fetch_klines_range(symbol, interval, start_ms, end_ms):
    out = []
    cursor = start_ms
    while cursor < end_ms:
        r = _request_with_retry(f"{BASE_URL}/klines", params={
            "symbol": symbol, "interval": interval,
            "startTime": cursor, "endTime": end_ms, "limit": 1000,
        })
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        last_close_time = batch[-1][6]
        if last_close_time <= cursor:
            break
        cursor = last_close_time + 1
        if len(batch) < 1000:
            break
        time.sleep(0.2)  # تفادي ضغط زائد على Binance عند الجلب الطويل
    return out


def pick_backtest_symbols(count):
    """يختار العملات حسب نفس فلتر السيولة/الاستبعاد المستخدم في الماسح الحي، مرتبة بالسيولة الحالية."""
    tickers = fetch_ticker24h()
    liquid = [
        t for t in tickers
        if t["symbol"].endswith("USDT")
        and not t["symbol"].endswith(EXCLUDE_SUFFIX)
        and t["symbol"] not in EXCLUDE_SYMS
        and float(t["quoteVolume"]) >= LIQUIDITY_FLOOR
    ]
    liquid.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    return [t["symbol"] for t in liquid[:count]]


# ---------------- محاكاة صفقة واحدة (دخول/خروج) ضمن بيانات تاريخية ----------------
def atr_value_at(ind, i, period=14):
    trs = []
    start = max(1, i - period + 1)
    for j in range(start, i + 1):
        prev_close = ind["closes"][j - 1]
        tr = max(ind["highs"][j] - ind["lows"][j],
                  abs(ind["highs"][j] - prev_close),
                  abs(ind["lows"][j] - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0


def htf_index_for(ltf_close_time, htf_close_times):
    """يجد آخر شمعة في الفريم الأعلى مُغلقة قبل أو عند وقت إغلاق شمعة الفريم الأدنى الحالية."""
    idx = bisect.bisect_right(htf_close_times, ltf_close_time) - 1
    return idx if idx >= 0 else None


def backtest_symbol(symbol, interval, klines, htf_klines, apply_extra_filters, score_threshold=2.5):
    ind = compute_indicators(klines)
    htf_ind = compute_indicators(htf_klines) if len(htf_klines) > 30 else None
    htf_close_times = [k[6] for k in htf_klines] if htf_ind else []

    n = len(ind["closes"])
    trades = []
    open_trade = None
    unresolved = 0

    for i in range(WARMUP_CANDLES, n):
        high, low, price = ind["highs"][i], ind["lows"][i], ind["closes"][i]

        # 1) تحديث الصفقة المفتوحة أولاً (بنفس ترتيب أولويات الفحص في البوت الحي)
        if open_trade:
            closed = False

            # أ) وقف الخسارة (يُفترض متحفظًا أنه يُفحص أولًا داخل نفس الشمعة)
            if low <= open_trade["sl"]:
                open_trade.update(exit_price=open_trade["sl"], exit_index=i, result="SL")
                trades.append(open_trade)
                open_trade = None
                closed = True

            # ب) الوصول لآخر هدف ربح (إغلاق كامل)
            elif high >= open_trade["tps"][-1]:
                open_trade.update(exit_price=open_trade["tps"][-1], exit_index=i, result="ALL_TP")
                trades.append(open_trade)
                open_trade = None
                closed = True

            # ج) انعكاس الاتجاه (EMA9/21) — نفس فحص trend_reversed في البوت الحي
            if not closed and open_trade:
                current_trend_up = ind["ema9"][i] > ind["ema21"][i]
                if current_trend_up != open_trade["trend_up"]:
                    open_trade.update(exit_price=price, exit_index=i, result="INVALIDATED")
                    trades.append(open_trade)
                    open_trade = None
                    closed = True

            # د) السقف الزمني — نفس TIME_STOP_HOURS في البوت الحي
            if not closed and open_trade:
                hours_open = (klines[i][6] - klines[open_trade["entry_index"]][6]) / 3600000
                if hours_open >= TIME_STOP_HOURS:
                    open_trade.update(exit_price=price, exit_index=i, result="EXPIRED")
                    trades.append(open_trade)
                    open_trade = None
                    closed = True

        if open_trade:
            continue  # صفقة واحدة مفتوحة بالتوازي لكل عملة، لتبسيط المحاكاة

        # 2) البحث عن إشارة جديدة
        r = score_at(i, ind, apply_extra_filters=apply_extra_filters)
        if not r:
            continue

        prev_r = score_at(i - 1, ind, apply_extra_filters=apply_extra_filters)
        persistent = bool(prev_r) and (prev_r["score"] > 0) == (r["score"] > 0) and abs(prev_r["score"]) >= 1.5

        final_score = r["score"]
        if htf_ind and abs(r["score"]) >= 1:
            htf_idx = htf_index_for(klines[i][6], htf_close_times)
            if htf_idx is not None and htf_idx >= 21:
                htf_up = htf_ind["ema9"][htf_idx] > htf_ind["ema21"][htf_idx]
                trend_dir = 1 if r["trend_up"] else -1
                htf_aligned = htf_up == r["trend_up"]
                final_score += (trend_dir * 0.5) if htf_aligned else (-trend_dir * 0.5)

        strong = (
            final_score >= score_threshold and r["vol_confirm"] and persistent
        )
        if apply_extra_filters:
            strong = strong and not r["ranging"] and not r["near_resistance"]

        # فلتر الحد الأدنى للتقلب (ATR%) يُحسب لحظيًا عند الشمعة i، وليس آخر شمعة في السلسلة كلها
        atrv = atr_value_at(ind, i)
        atr_pct_now = atrv / price * 100
        strong = strong and atr_pct_now >= 0.12

        if not strong:
            continue

        entry = price
        sl = entry - atrv * 1.5
        risk = entry - sl
        if risk <= 0:
            continue

        if final_score >= 4:
            tp_count = 4
        elif final_score >= 3:
            tp_count = 3
        elif final_score >= 2.5:
            tp_count = 2
        else:
            tp_count = 1
        tps = [entry + risk * k for k in range(1, tp_count + 1)]

        open_trade = {
            "symbol": symbol, "entry_index": i, "entry": entry,
            "sl": sl, "tps": tps, "score": final_score,
            "trend_up": r["trend_up"],
        }

    if open_trade:
        unresolved += 1  # صفقة بقيت مفتوحة حتى نهاية بيانات الاختبار — لم تُحسم ولا تُحتسب في الإحصائيات

    return trades, unresolved


# ---------------- تجميع الإحصائيات ----------------
def net_pct(t):
    """نسبة الربح/الخسارة الصافية للصفقة بعد خصم رسوم الدخول والخروج."""
    gross = (t["exit_price"] - t["entry"]) / t["entry"] * 100
    return gross - (FEE_PCT * 2)


def summarize(all_trades, label, unresolved=0):
    if not all_trades:
        print(f"\n=== {label}: لا توجد صفقات ===")
        if unresolved:
            print(f"(صفقات معلّقة لم تُحسم حتى نهاية الفترة: {unresolved})")
        return

    total = len(all_trades)
    pcts = [net_pct(t) for t in all_trades]

    # التصنيف الآن بناءً على الربح/الخسارة الصافي الفعلي، وليس فقط سبب الإغلاق —
    # لأن صفقة EXPIRED أو INVALIDATED قد تكون رابحة أو خاسرة بالصافي
    wins = [p for p in pcts if p > 0]
    losses = [p for p in pcts if p <= 0]

    win_rate = len(wins) / total * 100
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    ev = sum(pcts) / total

    by_result = {}
    for t in all_trades:
        by_result[t["result"]] = by_result.get(t["result"], 0) + 1

    best_i = max(range(total), key=lambda idx: pcts[idx])
    worst_i = min(range(total), key=lambda idx: pcts[idx])

    print(f"\n=== {label} ===")
    print(f"إجمالي الصفقات المغلقة: {total} | صفقات معلّقة (لم تُحسم حتى نهاية الفترة): {unresolved}")
    print("توزيع أسباب الإغلاق: " + ", ".join(f"{k}={v}" for k, v in by_result.items()))
    print(f"نسبة الصفقات الرابحة بعد الرسوم ({FEE_PCT*2:.2f}% ذهاب وإياب): {win_rate:.1f}%")
    print(f"متوسط الربح: +{avg_win:.2f}% | متوسط الخسارة: {avg_loss:.2f}%")
    print(f"القيمة المتوقعة لكل صفقة (EV صافي): {ev:+.3f}%")
    print(f"أفضل صفقة: {all_trades[best_i]['symbol'].replace('USDT','/USDT')} ({pcts[best_i]:+.2f}%)")
    print(f"أسوأ صفقة: {all_trades[worst_i]['symbol'].replace('USDT','/USDT')} ({pcts[worst_i]:+.2f}%)")

    per_symbol = {}
    for t, p in zip(all_trades, pcts):
        per_symbol.setdefault(t["symbol"], []).append(p)
    print("توزيع حسب العملة:")
    for sym, ps in sorted(per_symbol.items(), key=lambda x: -len(x[1])):
        w = sum(1 for p in ps if p > 0)
        avg = sum(ps) / len(ps)
        print(f"  {sym.replace('USDT','/USDT')}: {len(ps)} صفقة | نجاح {w}/{len(ps)} | EV متوسط {avg:+.2f}%")


# ---------------- التشغيل الرئيسي ----------------
def main():
    symbols = pick_backtest_symbols(BACKTEST_SYMBOL_COUNT)
    print(f"عملات الاختبار ({len(symbols)}): {', '.join(s.replace('USDT','/USDT') for s in symbols)}")
    print(f"رسوم مفترضة: {FEE_PCT:.3f}% لكل جهة ({FEE_PCT*2:.3f}% لكل صفقة كاملة)")
    print(f"عتبات الدرجة المُختبَرة (مع الفلاتر): {SCORE_THRESHOLDS}")

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - BACKTEST_DAYS * 86400 * 1000
    htf_interval = HTF_MAP.get(BACKTEST_INTERVAL)

    # جلب بيانات كل عملة مرة واحدة فقط، ثم إعادة استخدامها لكل الاختبارات (توفير طلبات API)
    symbol_data = []
    for symbol in symbols:
        print(f"\nجلب بيانات {symbol} ({BACKTEST_DAYS} يومًا، فريم {BACKTEST_INTERVAL})...")
        try:
            klines = fetch_klines_range(symbol, BACKTEST_INTERVAL, start_ms, end_ms)
            htf_klines = fetch_klines_range(symbol, htf_interval, start_ms, end_ms) if htf_interval else []
        except Exception as e:
            print(f"[تخطي {symbol}] فشل الجلب: {e}")
            continue

        if len(klines) < WARMUP_CANDLES + 30:
            print(f"[تخطي {symbol}] بيانات غير كافية ({len(klines)} شمعة)")
            continue

        symbol_data.append((symbol, klines, htf_klines))

    print("\n" + "=" * 50)
    print(f"نتائج الاختبار الرجعي — آخر {BACKTEST_DAYS} يومًا — فريم {BACKTEST_INTERVAL}")
    print("=" * 50)

    # 1) المقارنة الأساسية عند العتبة الافتراضية (2.5): مع الفلاتر مقابل بدونها
    base_threshold = SCORE_THRESHOLDS[0]
    trades_with_filters, unresolved_with = [], 0
    trades_without_filters, unresolved_without = [], 0
    for symbol, klines, htf_klines in symbol_data:
        t_with, u_with = backtest_symbol(symbol, BACKTEST_INTERVAL, klines, htf_klines, True, base_threshold)
        t_without, u_without = backtest_symbol(symbol, BACKTEST_INTERVAL, klines, htf_klines, False, base_threshold)
        trades_with_filters += t_with
        trades_without_filters += t_without
        unresolved_with += u_with
        unresolved_without += u_without

    summarize(trades_with_filters, f"مع الفلاتر الجديدة (ADX/انحراف/مقاومة/OBV) — عتبة {base_threshold}", unresolved_with)
    summarize(trades_without_filters, f"بدون الفلاتر الجديدة (المنطق القديم فقط) — عتبة {base_threshold}", unresolved_without)

    # 2) اختبار العتبات الإضافية (مع الفلاتر فقط، لأنها أثبتت أنها الأفضل)
    threshold_summaries = [(base_threshold, trades_with_filters, unresolved_with)]
    for threshold in SCORE_THRESHOLDS[1:]:
        trades_t, unresolved_t = [], 0
        for symbol, klines, htf_klines in symbol_data:
            t, u = backtest_symbol(symbol, BACKTEST_INTERVAL, klines, htf_klines, True, threshold)
            trades_t += t
            unresolved_t += u
        summarize(trades_t, f"مع الفلاتر — عتبة {threshold}", unresolved_t)
        threshold_summaries.append((threshold, trades_t, unresolved_t))

    # 3) جدول مقارنة نهائي يسهّل اختيار أفضل عتبة دفعة واحدة
    print("\n" + "=" * 50)
    print("جدول مقارنة العتبات (مع الفلاتر فقط)")
    print("=" * 50)
    print(f"{'العتبة':>8} | {'الصفقات':>8} | {'نسبة النجاح':>12} | {'EV صافي/صفقة':>14}")
    for threshold, trades_t, _ in threshold_summaries:
        if not trades_t:
            print(f"{threshold:>8} | {'0':>8} | {'-':>12} | {'-':>14}")
            continue
        pcts = [net_pct(t) for t in trades_t]
        win_rate = sum(1 for p in pcts if p > 0) / len(pcts) * 100
        ev = sum(pcts) / len(pcts)
        print(f"{threshold:>8} | {len(trades_t):>8} | {win_rate:>11.1f}% | {ev:>+13.3f}%")


if __name__ == "__main__":
    main()
