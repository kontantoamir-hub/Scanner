"""
اختبار رجعي (Backtesting) — يعيد تشغيل نفس منطق البوت الحي على بيانات تاريخية من Binance،
لمعرفة كيف كانت ستؤدي الاستراتيجية لو كانت تعمل فعلاً خلال تلك الفترة.

يقارن أيضًا الأداء مع الفلاتر الأربعة الجديدة (ADX / انحراف / مقاومة / OBV) مقابل بدونها،
للتحقق هل تحسّن جودة الإشارات فعلاً أم لا.

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
    BASE_URL, _request_with_retry,
)

# ---------- إعدادات الاختبار الرجعي ----------
BACKTEST_INTERVAL = os.environ.get("BACKTEST_INTERVAL", "1h")
BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "180"))       # مدة الفترة المُختبَرة بالأيام
BACKTEST_SYMBOL_COUNT = int(os.environ.get("BACKTEST_SYMBOL_COUNT", "15"))  # عدد العملات
WARMUP_CANDLES = 250  # عدد الشموع الأولى المستخدمة فقط لتهيئة المؤشرات (لا تُستخدم كإشارات)


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


def backtest_symbol(symbol, interval, klines, htf_klines, apply_extra_filters):
    ind = compute_indicators(klines)
    htf_ind = compute_indicators(htf_klines) if len(htf_klines) > 30 else None
    htf_close_times = [k[6] for k in htf_klines] if htf_ind else []

    n = len(ind["closes"])
    trades = []
    open_trade = None

    for i in range(WARMUP_CANDLES, n):
        high, low, price = ind["highs"][i], ind["lows"][i], ind["closes"][i]

        # 1) تحديث الصفقة المفتوحة أولاً (SL له أولوية داخل نفس الشمعة كافتراض متحفّظ)
        if open_trade:
            if low <= open_trade["sl"]:
                open_trade.update(exit_price=open_trade["sl"], exit_index=i, result="SL")
                trades.append(open_trade)
                open_trade = None
            else:
                if high >= open_trade["tps"][-1]:
                    open_trade.update(exit_price=open_trade["tps"][-1], exit_index=i, result="ALL_TP")
                    trades.append(open_trade)
                    open_trade = None

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
            final_score >= 2.5 and r["vol_confirm"] and persistent
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
        }

    return trades


# ---------------- تجميع الإحصائيات ----------------

def summarize(all_trades, label):
    if not all_trades:
        print(f"\n=== {label}: لا توجد صفقات ===")
        return

    wins = [t for t in all_trades if t["result"] == "ALL_TP"]
    losses = [t for t in all_trades if t["result"] == "SL"]
    total = len(all_trades)
    win_rate = len(wins) / total * 100 if total else 0

    def pct(t):
        return (t["exit_price"] - t["entry"]) / t["entry"] * 100

    avg_win = sum(pct(t) for t in wins) / len(wins) if wins else 0
    avg_loss = sum(pct(t) for t in losses) / len(losses) if losses else 0
    best = max(all_trades, key=pct) if all_trades else None
    worst = min(all_trades, key=pct) if all_trades else None

    print(f"\n=== {label} ===")
    print(f"إجمالي الصفقات: {total} | رابحة: {len(wins)} | خاسرة: {len(losses)} | نسبة النجاح: {win_rate:.1f}%")
    print(f"متوسط الربح: +{avg_win:.2f}% | متوسط الخسارة: {avg_loss:.2f}%")
    if best:
        print(f"أفضل صفقة: {best['symbol'].replace('USDT','/USDT')} ({pct(best):+.2f}%)")
    if worst:
        print(f"أسوأ صفقة: {worst['symbol'].replace('USDT','/USDT')} ({pct(worst):+.2f}%)")

    per_symbol = {}
    for t in all_trades:
        per_symbol.setdefault(t["symbol"], []).append(t)
    print("توزيع حسب العملة:")
    for sym, ts in sorted(per_symbol.items(), key=lambda x: -len(x[1])):
        w = sum(1 for t in ts if t["result"] == "ALL_TP")
        print(f"  {sym.replace('USDT','/USDT')}: {len(ts)} صفقة | نجاح {w}/{len(ts)}")


# ---------------- التشغيل الرئيسي ----------------

def main():
    symbols = pick_backtest_symbols(BACKTEST_SYMBOL_COUNT)
    print(f"عملات الاختبار ({len(symbols)}): {', '.join(s.replace('USDT','/USDT') for s in symbols)}")

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - BACKTEST_DAYS * 86400 * 1000
    htf_interval = HTF_MAP.get(BACKTEST_INTERVAL)

    trades_with_filters, trades_without_filters = [], []

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

        trades_with_filters += backtest_symbol(symbol, BACKTEST_INTERVAL, klines, htf_klines, True)
        trades_without_filters += backtest_symbol(symbol, BACKTEST_INTERVAL, klines, htf_klines, False)

    print("\n" + "=" * 50)
    print(f"نتائج الاختبار الرجعي — آخر {BACKTEST_DAYS} يومًا — فريم {BACKTEST_INTERVAL}")
    print("=" * 50)
    summarize(trades_with_filters, "مع الفلاتر الجديدة (ADX / انحراف / مقاومة / OBV)")
    summarize(trades_without_filters, "بدون الفلاتر الجديدة (المنطق القديم فقط)")


if __name__ == "__main__":
    main()
