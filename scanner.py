"""
ماسح السوق — نسخة البايثون (تعمل بجدولة تلقائية عبر GitHub Actions)
نفس منطق أداة HTML: فلترة سيولة/حركة -> تحليل عميق -> تأكيد فريم أعلى -> استقرار -> تنبيه تيليجرام
"""

import os
import json
import time
import concurrent.futures
import requests

# ---------- الإعدادات (تُقرأ من متغيرات البيئة / GitHub Secrets) ----------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
INTERVAL = os.environ.get("SCAN_INTERVAL", "1h")          # 15m / 1h / 4h / 1d
DEPTH = int(os.environ.get("SCAN_DEPTH", "40"))            # عدد العملات للفحص العميق
SCAN_LIMIT = 400                                            # عدد الشموع التاريخية لكل عملة
LIQUIDITY_FLOOR = 1_000_000                                 # أدنى سيولة 24س بالدولار

EXCLUDE_SUFFIX = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
EXCLUDE_SYMS = {"USDCUSDT","FDUSDUSDT","TUSDUSDT","DAIUSDT","USDPUSDT",
                "EURUSDT","GBPUSDT","AEURUSDT","BFUSDUSDT"}

HTF_MAP = {"15m": "1h", "1h": "4h", "4h": "1d", "1d": "1w"}

BASE_URL = "https://data-api.binance.vision/api/v3"

# ---------- إعدادات فلاتر التحليل الإضافية (ADX / الانحراف / المقاومة / OBV) ----------
ADX_PERIOD = 14
ADX_THRESHOLD = 20              # تحت هذا المستوى يُعتبر السوق عرضيًا (بلا اتجاه واضح)
DIVERGENCE_LOOKBACK = 20        # عدد الشموع للبحث فيها عن قيعان سعرية للمقارنة مع RSI
DIVERGENCE_PIVOT_SPAN = 3       # عدد الشموع على كل جانب لاعتبار نقطة "قاع محلي"
RESISTANCE_LOOKBACK = 50        # عدد الشموع للبحث فيها عن أقرب مقاومة سابقة
RESISTANCE_PIVOT_SPAN = 3       # عدد الشموع على كل جانب لاعتبار نقطة "قمة محلية"
RESISTANCE_PROXIMITY_PCT = 1.5  # لو السعر أقرب من هذه النسبة% لمقاومة فوقه -> تحذير
OBV_TREND_WINDOW = 10           # عدد الشموع لقياس اتجاه OBV مقابل اتجاه السعر


# ---------------- دوال المؤشرات الفنية ----------------

def ema(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values, period=14):
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain, avg_loss = gains / period, losses / period
    out = [None] * period
    out.append(100 - (100 / (1 + (avg_gain / (avg_loss or 1e-9)))))
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        gain = d if d > 0 else 0
        loss = -d if d < 0 else 0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out.append(100 - (100 / (1 + avg_gain / (avg_loss or 1e-9))))
    return out


def macd(values):
    e12, e26 = ema(values, 12), ema(values, 26)
    macd_line = [a - b for a, b in zip(e12, e26)]
    return macd_line, ema(macd_line, 9)


def bollinger(values, period=20, mult=2):
    upper, lower = [], []
    for i in range(len(values)):
        if i < period - 1:
            upper.append(None); lower.append(None); continue
        window = values[i - period + 1:i + 1]
        mean = sum(window) / period
        sd = (sum((x - mean) ** 2 for x in window) / period) ** 0.5
        upper.append(mean + mult * sd)
        lower.append(mean - mult * sd)
    return upper, lower


def rolling_avg(values, period):
    out = [None] * len(values)
    for i in range(period, len(values)):
        out[i] = sum(values[i - period:i]) / period
    return out


def adx(highs, lows, closes, period=ADX_PERIOD):
    """
    مؤشر قوة الاتجاه (ADX) — يميّز السوق المتجه بوضوح عن السوق العرضي المتذبذب.
    قيمة أقل من ~20 تعني غالبًا سوقًا بلا اتجاه واضح، حيث تكثر الإشارات الكاذبة.
    يرجع قائمة بنفس طول closes، بقيم None قبل اكتمال فترة الحساب.
    """
    n = len(closes)
    out = [None] * n
    if n <= period * 2:
        return out

    tr = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))

    tr_sum = sum(tr[1:period + 1])
    plus_sum = sum(plus_dm[1:period + 1])
    minus_sum = sum(minus_dm[1:period + 1])

    dx = [None] * n
    for i in range(period + 1, n):
        tr_sum = tr_sum - (tr_sum / period) + tr[i]
        plus_sum = plus_sum - (plus_sum / period) + plus_dm[i]
        minus_sum = minus_sum - (minus_sum / period) + minus_dm[i]
        pdi = 100 * plus_sum / tr_sum if tr_sum else 0
        mdi = 100 * minus_sum / tr_sum if tr_sum else 0
        dx[i] = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0

    start = period * 2
    valid_dx = [x for x in dx[period + 1:start + 1] if x is not None]
    if not valid_dx:
        return out
    out[start] = sum(valid_dx) / len(valid_dx)
    for i in range(start + 1, n):
        if out[i - 1] is None or dx[i] is None:
            continue
        out[i] = (out[i - 1] * (period - 1) + dx[i]) / period
    return out


def obv(closes, vols):
    """
    On-Balance Volume — يجمع الحجم مع اتجاه السعر، لكشف هل الحجم يدعم الحركة فعليًا
    أم أن الصعود/الهبوط يحدث بحجم ضعيف (أقل موثوقية).
    """
    out = [0.0] * len(closes)
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            out[i] = out[i - 1] + vols[i]
        elif closes[i] < closes[i - 1]:
            out[i] = out[i - 1] - vols[i]
        else:
            out[i] = out[i - 1]
    return out


def obv_confirms_trend(obv_vals, trend_up, window=OBV_TREND_WINDOW):
    """يتحقق هل اتجاه OBV خلال آخر window شمعة يتماشى مع اتجاه السعر (EMA9/21)."""
    if len(obv_vals) <= window:
        return False
    obv_slope_up = obv_vals[-1] > obv_vals[-1 - window]
    return obv_slope_up == trend_up


def bullish_divergence(closes, rsi_vals, lookback=DIVERGENCE_LOOKBACK, pivot_span=DIVERGENCE_PIVOT_SPAN):
    """
    يكشف انحراف صعودي: السعر يصنع قاعًا أدنى من القاع السابق، بينما RSI يصنع قاعًا أعلى —
    من أقوى إشارات احتمال الانعكاس للأعلى عند المحترفين.
    """
    n = len(closes)
    if n < lookback + pivot_span * 2:
        return False
    start = n - lookback
    lows_idx = []
    for i in range(max(start, pivot_span), n - pivot_span):
        if rsi_vals[i] is None:
            continue
        window = closes[i - pivot_span:i + pivot_span + 1]
        if closes[i] == min(window):
            lows_idx.append(i)
    if len(lows_idx) < 2:
        return False
    i1, i2 = lows_idx[-2], lows_idx[-1]
    if rsi_vals[i1] is None or rsi_vals[i2] is None:
        return False
    price_lower_low = closes[i2] < closes[i1]
    rsi_higher_low = rsi_vals[i2] > rsi_vals[i1]
    return price_lower_low and rsi_higher_low


def nearest_resistance(highs, closes, lookback=RESISTANCE_LOOKBACK, pivot_span=RESISTANCE_PIVOT_SPAN):
    """يرجع أقرب مستوى مقاومة (قمة سعرية سابقة) فوق السعر الحالي، أو None لو لا توجد."""
    n = len(highs)
    window_n = min(lookback, n)
    start = n - window_n
    price = closes[-1]
    pivots = []
    for i in range(max(start, pivot_span), n - pivot_span):
        window = highs[i - pivot_span:i + pivot_span + 1]
        if highs[i] == max(window):
            pivots.append(highs[i])
    above = [p for p in pivots if p > price]
    return min(above) if above else None


def compute_indicators(klines):
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    vols = [float(k[5]) for k in klines]
    macd_line, signal = macd(closes)
    bb_upper, bb_lower = bollinger(closes)
    return {
        "closes": closes, "highs": highs, "lows": lows, "vols": vols,
        "ema9": ema(closes, 9), "ema21": ema(closes, 21),
        "rsi": rsi(closes),
        "macd": macd_line, "signal": signal,
        "bb_upper": bb_upper, "bb_lower": bb_lower,
        "vol_avg": rolling_avg(vols, 20),
        "adx": adx(highs, lows, closes),
        "obv": obv(closes, vols),
    }


def score_at(i, ind, apply_extra_filters=True):
    if i < 26 or ind["bb_upper"][i] is None or ind["rsi"][i] is None or ind["vol_avg"][i] is None:
        return None
    trend_up = ind["ema9"][i] > ind["ema21"][i]
    rv = ind["rsi"][i]
    rsi_state = 1 if rv < 35 else (-1 if rv > 65 else 0)
    macd_bull = ind["macd"][i] > ind["signal"][i]
    price = ind["closes"][i]
    bb_state = 1 if price <= ind["bb_lower"][i] else (-1 if price >= ind["bb_upper"][i] else 0)
    vol_confirm = ind["vols"][i] > ind["vol_avg"][i] * 1.1
    trend_dir = 1 if trend_up else -1
    vol_score = trend_dir * 0.5 if vol_confirm else 0
    score = trend_dir + rsi_state + (1 if macd_bull else -1) + bb_state + vol_score

    # --- فلاتر إضافية لتحسين جودة الإشارة (ADX / انحراف / مقاومة / OBV) ---
    # تُحسب دائمًا للعرض التشخيصي، لكن تُطبَّق على الدرجة فقط لو apply_extra_filters=True
    # (يُستخدم False في الاختبار الرجعي لمقارنة الأداء بدونها)

    adx_val = ind["adx"][i] if i < len(ind["adx"]) else None
    ranging = adx_val is not None and adx_val < ADX_THRESHOLD

    divergence = bullish_divergence(ind["closes"][:i + 1], ind["rsi"][:i + 1])

    resistance = nearest_resistance(ind["highs"][:i + 1], ind["closes"][:i + 1])
    near_resistance = False
    if resistance:
        dist_pct = (resistance - price) / price * 100
        near_resistance = 0 <= dist_pct <= RESISTANCE_PROXIMITY_PCT

    obv_confirm = obv_confirms_trend(ind["obv"][:i + 1], trend_up)

    if apply_extra_filters:
        if ranging:
            score *= 0.5
        if divergence:
            score += 1
        if near_resistance:
            score -= 1
        if obv_confirm:
            score += trend_dir * 0.5

    return {
        "score": score, "trend_up": trend_up, "vol_confirm": vol_confirm, "rv": rv,
        "adx_val": adx_val, "ranging": ranging,
        "divergence": divergence,
        "near_resistance": near_resistance, "resistance": resistance,
        "obv_confirm": obv_confirm,
    }


def atr_percent(ind, period=14):
    return atr_value(ind, period) / ind["closes"][-1] * 100


def atr_value(ind, period=14):
    """متوسط المدى الحقيقي بالقيمة المطلقة (وحدة السعر نفسها)، يُستخدم لحساب وقف خسارة يتناسب مع تقلب كل عملة."""
    n = len(ind["closes"])
    trs = []
    for i in range(n - period, n):
        prev_close = ind["closes"][i - 1] if i > 0 else ind["closes"][i]
        tr = max(ind["highs"][i] - ind["lows"][i],
                  abs(ind["highs"][i] - prev_close),
                  abs(ind["lows"][i] - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs)


# ---------------- جلب البيانات من Binance ----------------

def _request_with_retry(url, params=None, timeout=20, retries=3, backoff=1.5):
    """طلب HTTP مع إعادة محاولة تلقائية عند فشل الشبكة أو ضغط مؤقت من Binance (429/5xx)."""
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.exceptions.HTTPError(f"status {r.status_code}")
            r.raise_for_status()
            return r
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))  # انتظار متزايد بين المحاولات
    raise last_err


def fetch_ticker24h():
    r = _request_with_retry(f"{BASE_URL}/ticker/24hr")
    return r.json()


def fetch_prices_map(tickers):
    """يبني قاموسًا {رمز: آخر سعر} من نفس بيانات ticker24h بدون طلب إضافي."""
    out = {}
    for t in tickers:
        try:
            out[t["symbol"]] = float(t["lastPrice"])
        except Exception:
            continue
    return out


def fetch_klines(symbol, interval, limit=SCAN_LIMIT):
    r = _request_with_retry(f"{BASE_URL}/klines",
                             params={"symbol": symbol, "interval": interval, "limit": limit})
    return r.json()


def drop_unclosed_candle(klines):
    """
    يستبعد آخر شمعة إذا كانت لسا مفتوحة (لم تُغلق بعد وقت التشغيل)، لتفادي تحليل
    بيانات ناقصة قابلة للتغيّر (Repainting) — Binance ترجع الشمعة الجارية كآخر عنصر دائمًا.
    عنصر الشمعة: [open_time, open, high, low, close, volume, close_time, ...]
    """
    if not klines:
        return klines
    now_ms = time.time() * 1000
    close_time = klines[-1][6]
    if close_time > now_ms:
        return klines[:-1]
    return klines


# ---------------- تحليل عملة واحدة ----------------

def analyze_symbol(t, interval):
    symbol = t["symbol"]
    try:
        klines = fetch_klines(symbol, interval, SCAN_LIMIT)
        klines = drop_unclosed_candle(klines)
        if len(klines) < 60:
            return None
        ind = compute_indicators(klines)
        last = len(ind["closes"]) - 1
        r = score_at(last, ind)
        if not r:
            return None

        prev_r = score_at(last - 1, ind)
        persistent = bool(prev_r) and (prev_r["score"] > 0) == (r["score"] > 0) and abs(prev_r["score"]) >= 1.5

        final_score = r["score"]
        htf_checked, htf_aligned = False, None
        if abs(r["score"]) >= 1:
            htf = HTF_MAP.get(interval)
            if htf:
                try:
                    htf_klines = fetch_klines(symbol, htf, 60)
                    htf_klines = drop_unclosed_candle(htf_klines)
                    htf_closes = [float(k[4]) for k in htf_klines]
                    htf_up = ema(htf_closes, 9)[-1] > ema(htf_closes, 21)[-1]
                    trend_dir = 1 if r["trend_up"] else -1
                    htf_aligned = htf_up == r["trend_up"]
                    final_score += (trend_dir * 0.5) if htf_aligned else (-trend_dir * 0.5)
                    htf_checked = True
                except Exception:
                    pass

        # خطة دخول (شراء فقط — السوق الفوري لا يدعم فتح صفقة بيع مكشوفة)، محسوبة ديناميكيًا حسب التحليل:
        # وقف الخسارة من التقلب الفعلي (ATR) للعملة، وعدد الأهداف حسب قوة درجة التوافق
        entry = sl = None
        tps = []
        if final_score >= 1:
            entry = ind["closes"][last]
            atrv = atr_value(ind)
            sl = entry - atrv * 1.5
            risk = entry - sl

            if final_score >= 4:
                tp_count = 4
            elif final_score >= 3:
                tp_count = 3
            elif final_score >= 2.5:
                tp_count = 2
            else:
                tp_count = 1

            tps = [entry + risk * i for i in range(1, tp_count + 1)]

        return {
            "symbol": symbol,
            "price": ind["closes"][last],
            "change_pct": float(t["priceChangePercent"]),
            "score": final_score,
            "trend_up": r["trend_up"],
            "vol_confirm": r["vol_confirm"],
            "atr_pct": atr_percent(ind),
            "persistent": persistent,
            "htf_checked": htf_checked,
            "htf_aligned": htf_aligned,
            "ranging": r["ranging"],
            "divergence": r["divergence"],
            "near_resistance": r["near_resistance"],
            "obv_confirm": r["obv_confirm"],
            "entry": entry, "sl": sl, "tps": tps,
        }
    except Exception as e:
        print(f"[تخطي] {symbol}: {e}")
        return None


# ---------------- المسح الكامل (مرحلتين) ----------------

def run_scan(tickers=None):
    if tickers is None:
        tickers = fetch_ticker24h()

    liquid = [
        t for t in tickers
        if t["symbol"].endswith("USDT")
        and not t["symbol"].endswith(EXCLUDE_SUFFIX)
        and t["symbol"] not in EXCLUDE_SYMS
        and float(t["quoteVolume"]) >= LIQUIDITY_FLOOR
    ]
    # ترتيب مركّب: يجمع بين رتبة السيولة الحالية ورتبة قوة الحركة، بدل الاعتماد على الحركة وحدها
    # (عملة عالية السيولة لكن حركتها المئوية بسيطة قد تكون أهم من عملة صغيرة تحركت كثيرًا نسبيًا)
    by_volume = sorted(liquid, key=lambda t: float(t["quoteVolume"]), reverse=True)
    by_momentum = sorted(liquid, key=lambda t: abs(float(t["priceChangePercent"])), reverse=True)
    volume_rank = {t["symbol"]: i for i, t in enumerate(by_volume)}
    momentum_rank = {t["symbol"]: i for i, t in enumerate(by_momentum)}
    combined = sorted(liquid, key=lambda t: volume_rank[t["symbol"]] + momentum_rank[t["symbol"]])
    shortlist = combined[:DEPTH]

    print(f"سيولة كافية: {len(liquid)} عملة | فحص عميق: {len(shortlist)} عملة | فريم: {INTERVAL}")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(analyze_symbol, t, INTERVAL) for t in shortlist]
        for f in concurrent.futures.as_completed(futures):
            r = f.result()
            if r:
                results.append(r)

    return results


# ---------------- تيليجرام ----------------

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ TELEGRAM_TOKEN أو TELEGRAM_CHAT_ID غير موجودين — تخطي الإرسال.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
        if not resp.ok:
            print("فشل إرسال تيليجرام:", resp.text)
    except Exception as e:
        print("خطأ إرسال تيليجرام:", e)


def format_alert(r, market_caution=False):
    is_buy = r["score"] >= 2.5
    dot = "🟢" if is_buy else "🔴"
    title = "إشارة شراء" if is_buy else "تجنب شراء"

    badges = []
    if r["persistent"]:
        badges.append("مستقرة")
    if r["htf_checked"]:
        badges.append("متوافقة مع فريم أعلى" if r["htf_aligned"] else "تعاكس فريم أعلى")
    if r.get("divergence"):
        badges.append("انحراف صعودي مؤكد")
    if r.get("obv_confirm"):
        badges.append("حجم داعم (OBV)")
    if r.get("ranging"):
        badges.append("⚠️ سوق عرضي (ADX ضعيف)")
    if r.get("near_resistance"):
        badges.append("⚠️ قريب من مقاومة قوية")
    badge_txt = f" ({', '.join(badges)})" if badges else ""

    lines = [
        f"{dot} {title}",
        r['symbol'].replace('USDT', '/USDT'),
        f"الدرجة: {r['score']:.1f} | فريم: {INTERVAL}{badge_txt}",
        f"السعر: {r['price']:.6g}",
    ]

    if r.get("entry") is not None:
        lines.append(f"الدخول: {r['entry']:.6g}")
        for i, tp in enumerate(r.get("tps", []), start=1):
            lines.append(f"TP{i}: {tp:.6g}")
        lines.append(f"وقف الخسارة: {r['sl']:.6g}")
    else:
        # السوق الفوري لا يدعم فتح صفقة بيع مكشوفة — فلا توجد خطة دخول لإشارات "تجنب شراء"
        lines.append("لا توجد خطة دخول (تحذير فقط)")

    if market_caution:
        lines.append("⚠️ سيطرة BTC (Dominance) تتحرك بقوة الآن — إشارات العملات البديلة أقل موثوقية مؤقتًا")

    return "\n".join(lines)


# ---------------- إدارة الحالة عبر GitHub Gist (بديل عن الكتابة داخل المستودع) ----------------

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")
GIST_FILENAME = "alerted_state.json"
POSITIONS_GIST_FILE = "open_positions.json"   # الصفقات المفتوحة قيد المتابعة (نفس الـ Gist، ملف منفصل)
CLOSED_GIST_FILE = "closed_trades.json"       # سجل الصفقات المغلقة (لإحصائية الأداء)
MAX_CLOSED_HISTORY = 300                      # سقف لعدد الصفقات المؤرشفة كي لا يتضخم الـ Gist بلا حدود
DOM_SHIFT_THRESHOLD = float(os.environ.get("DOM_SHIFT_THRESHOLD", "0.3"))  # نقطة مئوية خلال دورة تشغيل واحدة


def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def _gist_get_file(filename):
    """يقرأ محتوى ملف واحد داخل الـ Gist (يرجع None لو غير موجود أو حصل خطأ)."""
    if not GIST_TOKEN or not GIST_ID:
        return None
    try:
        r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=_gist_headers(), timeout=15)
        r.raise_for_status()
        files = r.json().get("files", {})
        if filename not in files:
            return None
        return files[filename]["content"]
    except Exception as e:
        print(f"تعذّر قراءة {filename} من Gist ({e})")
        return None


def _gist_patch_files(files_dict):
    """يحفظ عدة ملفات دفعة واحدة داخل نفس الـ Gist (الملفات غير المذكورة تبقى كما هي)."""
    if not GIST_TOKEN or not GIST_ID:
        return
    payload = {"files": {fn: {"content": content} for fn, content in files_dict.items()}}
    try:
        r = requests.patch(f"https://api.github.com/gists/{GIST_ID}", headers=_gist_headers(),
                            json=payload, timeout=15)
        if not r.ok:
            print("فشل حفظ الحالة في Gist:", r.text)
    except Exception as e:
        print("خطأ حفظ الحالة في Gist:", e)


def load_state():
    """يحمّل ذاكرة الإشارات المرسلة وآخر قيمة BTC Dominance من Gist خاص، بدل ملف داخل المستودع."""
    if not GIST_TOKEN or not GIST_ID:
        print("⚠️ GIST_TOKEN أو GIST_ID غير موجودين — سيبدأ البوت بذاكرة فارغة هذا التشغيل.")
        return set(), None
    content = _gist_get_file(GIST_FILENAME)
    if not content:
        return set(), None
    try:
        data = json.loads(content)
        return set(data.get("alerted", [])), data.get("btc_dominance_prev")
    except Exception as e:
        print(f"تعذّر تحليل حالة Gist ({e}) — سيبدأ البوت بذاكرة فارغة.")
        return set(), None


def load_positions():
    """يحمّل الصفقات المفتوحة قيد المتابعة من الـ Gist."""
    content = _gist_get_file(POSITIONS_GIST_FILE)
    if not content:
        return []
    try:
        return json.loads(content)
    except Exception as e:
        print(f"تعذّر تحليل open_positions من Gist ({e})")
        return []


def load_closed():
    content = _gist_get_file(CLOSED_GIST_FILE)
    if not content:
        return []
    try:
        return json.loads(content)
    except Exception:
        return []


def save_all_state(alerted_symbols, btc_dominance, positions, closed_delta):
    """
    يحفظ في نفس الطلب: حالة التنبيهات + BTC Dominance + الصفقات المفتوحة،
    ويُلحق أي صفقات أُغلقت هذا التشغيل بسجل closed_trades (مع سقف للحجم).
    """
    files = {
        GIST_FILENAME: json.dumps(
            {"alerted": sorted(alerted_symbols), "btc_dominance_prev": btc_dominance},
            ensure_ascii=False
        ),
        POSITIONS_GIST_FILE: json.dumps(positions, ensure_ascii=False, indent=2),
    }
    if closed_delta:
        history = load_closed()
        history.extend(closed_delta)
        if len(history) > MAX_CLOSED_HISTORY:
            history = history[-MAX_CLOSED_HISTORY:]
        files[CLOSED_GIST_FILE] = json.dumps(history, ensure_ascii=False, indent=2)

    _gist_patch_files(files)


# ---------------- تتبع الصفقات المفتوحة (TP / SL) ----------------

def open_new_positions(positions, fresh_signals):
    """يضيف كل إشارة شراء جديدة أُرسلت كصفقة مفتوحة قيد المتابعة. يُعدّل القائمة في المكان (in place)."""
    for r in fresh_signals:
        if r.get("entry") is None:
            continue  # لا خطة دخول (تجنب شراء) -> لا داعي لتتبعها
        positions.append({
            "symbol": r["symbol"],
            "entry": r["entry"],
            "sl": r["sl"],
            "tps": r["tps"],
            "hit_tps": [],
            "score": r["score"],
            "trend_up": r["trend_up"],   # اتجاه EMA9/21 وقت فتح الصفقة، يُستخدم لاحقًا لكشف انعكاس الإشارة
            "interval": INTERVAL,
            "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })


TIME_STOP_HOURS = float(os.environ.get("TIME_STOP_HOURS", "96"))  # سقف زمني أقصى (شبكة أمان) قبل اعتبار الصفقة منتهية الصلاحية — افتراضيًا 4 أيام


def _hours_since(opened_at_str):
    try:
        opened = time.strptime(opened_at_str, "%Y-%m-%d %H:%M:%S")
        opened_epoch = time.mktime(opened)
        return (time.time() - opened_epoch) / 3600
    except Exception:
        return 0


def format_duration(hours):
    """يحوّل عدد الساعات لصيغة مقروءة: أيام + ساعات، بجمع عربي مبسّط."""
    total_minutes = round(hours * 60)
    days, rem_minutes = divmod(total_minutes, 24 * 60)
    hrs, minutes = divmod(rem_minutes, 60)

    def hours_word(n):
        if n == 1:
            return "ساعة"
        if n == 2:
            return "ساعتين"
        if 3 <= n <= 10:
            return f"{n} ساعات"
        return f"{n} ساعة"

    parts = []
    if days:
        parts.append("يوم" if days == 1 else ("يومين" if days == 2 else f"{days} أيام"))
    if hrs:
        parts.append(hours_word(hrs))
    if not parts:
        parts.append(f"{minutes} دقيقة")
    return " و".join(parts)


def format_sl_hit(pos, price):
    entry = pos["entry"]
    sl = pos["sl"]
    pct_drop = (sl - entry) / entry * 100
    duration = format_duration(_hours_since(pos["opened_at"]))
    return (
        f"❌ {pos['symbol'].replace('USDT', '/USDT')}\n"
        f"سعر الدخول: {entry:.6g}\n"
        f"SL: {sl:.6g}\n"
        f"نسبة النزول: {pct_drop:.2f}%\n"
        f"المدة الزمنية لضرب وقف الخسارة: {duration}"
    )


def format_tp_hit(pos, tp_index, price):
    entry = pos["entry"]
    tp = pos["tps"][tp_index]
    pct_gain = (tp - entry) / entry * 100
    duration = format_duration(_hours_since(pos["opened_at"]))
    return (
        f"✅ {pos['symbol'].replace('USDT', '/USDT')}\n"
        f"سعر الدخول: {entry:.6g}\n"
        f"TP{tp_index + 1}: {tp:.6g}\n"
        f"نسبة الصعود: +{pct_gain:.2f}%\n"
        f"المدة الزمنية لتحقيق الهدف: {duration}"
    )


def trend_reversed(symbol, interval, original_trend_up):
    """
    يفحص هل انعكس اتجاه EMA9/21 منذ فتح الصفقة (إبطال الإشارة الأصلية).
    يرجع True/False عند نجاح الفحص، أو None لو تعذّر الجلب (لا نغلق الصفقة بالخطأ في هذه الحالة).
    """
    try:
        klines = fetch_klines(symbol, interval, limit=30)
        klines = drop_unclosed_candle(klines)
        closes = [float(k[4]) for k in klines]
        if len(closes) < 22:
            return None
        current_trend_up = ema(closes, 9)[-1] > ema(closes, 21)[-1]
        return current_trend_up != original_trend_up
    except Exception:
        return None


def check_open_positions(positions, price_map):
    """
    يقارن الصفقات المفتوحة بالسعر الحالي، يرسل إشعار تيليجرام عند تحقق هدف،
    ضرب وقف خسارة، انعكاس الإشارة الأصلية (EMA)، أو انتهاء السقف الزمني.
    يرجع (الصفقات المتبقية مفتوحة، الصفقات التي أُغلقت الآن).
    """
    still_open, closed_now = [], []

    for pos in positions:
        price = price_map.get(pos["symbol"])
        if price is None:
            still_open.append(pos)
            continue

        if price <= pos["sl"]:
            send_telegram(format_sl_hit(pos, price))
            pos["closed_reason"] = "SL"
            pos["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            pos["exit_price"] = price
            closed_now.append(pos)
            time.sleep(1)
            continue

        newly_hit = [i for i, tp in enumerate(pos["tps"]) if i not in pos["hit_tps"] and price >= tp]
        if newly_hit:
            pos["hit_tps"].extend(newly_hit)
            for i in newly_hit:
                send_telegram(format_tp_hit(pos, i, price))
                time.sleep(1)

        if len(pos["hit_tps"]) >= len(pos["tps"]):
            pos["closed_reason"] = "ALL_TP"
            pos["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            pos["exit_price"] = price
            closed_now.append(pos)
            continue

        # لم يتحقق TP ولا SL بعد -> افحص إبطال الإشارة (انعكاس الاتجاه) قبل السقف الزمني
        reversed_signal = trend_reversed(pos["symbol"], pos.get("interval", INTERVAL), pos["trend_up"])
        if reversed_signal:
            send_telegram(
                f"⚠️ انعكس الاتجاه\n{pos['symbol'].replace('USDT','/USDT')}\n"
                f"الدخول: {pos['entry']:.6g} | الحالي: {price:.6g}"
            )
            pos["closed_reason"] = "INVALIDATED"
            pos["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            pos["exit_price"] = price
            closed_now.append(pos)
            time.sleep(1)
            continue

        hours_open = _hours_since(pos["opened_at"])
        if hours_open >= TIME_STOP_HOURS:
            send_telegram(
                f"⏱️ انتهت صلاحية المراقبة (سقف زمني)\n{pos['symbol'].replace('USDT','/USDT')}\n"
                f"الدخول: {pos['entry']:.6g} | الحالي: {price:.6g} | مدة المراقبة: {hours_open:.0f}س"
            )
            pos["closed_reason"] = "EXPIRED"
            pos["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            pos["exit_price"] = price
            closed_now.append(pos)
            time.sleep(1)
            continue

        still_open.append(pos)

    if closed_now:
        print(f"صفقات أُغلقت هذا المسح: {len(closed_now)}")

    return still_open, closed_now


# ---------------- BTC Dominance (تحذير جودة إشارات العملات البديلة) ----------------


def fetch_btc_dominance():
    r = _request_with_retry("https://api.coingecko.com/api/v3/global")
    return r.json()["data"]["market_cap_percentage"]["btc"]


# ---------------- التشغيل الرئيسي ----------------

def main():
    print(f"بدء المسح — {time.strftime('%Y-%m-%d %H:%M:%S')}")

    tickers = fetch_ticker24h()
    price_map = fetch_prices_map(tickers)

    # قبل أي مسح جديد: تفقّد الصفقات المفتوحة سابقًا مقابل السعر الحالي (TP / SL)
    open_positions = load_positions()
    open_positions, closed_now = check_open_positions(open_positions, price_map)

    results = run_scan(tickers)

    strong = [
        r for r in results
        if r["score"] >= 2.5 and r["vol_confirm"] and r["atr_pct"] >= 0.12 and r["persistent"]
        and not r["ranging"] and not r["near_resistance"]
    ]
    strong_symbols = {r["symbol"] for r in strong}

    prev_alerted, prev_dominance = load_state()
    fresh = [r for r in strong if r["symbol"] not in prev_alerted]

    # تتبّع BTC Dominance: تحذير إضافي لو تحركت بقوة منذ آخر تشغيل (إشارات العملات البديلة تصير أقل موثوقية)
    btc_dominance = None
    market_caution = False
    try:
        btc_dominance = fetch_btc_dominance()
        if prev_dominance is not None:
            shift = btc_dominance - prev_dominance
            market_caution = abs(shift) >= DOM_SHIFT_THRESHOLD
            print(f"BTC Dominance: {btc_dominance:.2f}% (تغيّر {shift:+.2f} نقطة منذ آخر تشغيل)"
                  + (" — تحذير سوق مفعّل" if market_caution else ""))
        else:
            print(f"BTC Dominance: {btc_dominance:.2f}% (أول قراءة، لا مقارنة بعد)")
    except Exception as e:
        print("تعذّر جلب BTC Dominance:", e)

    print(f"إشارات قوية حاليًا: {len(strong)} | جديدة (لم تُرسل قبل): {len(fresh)}")

    for r in fresh:
        caution = market_caution and not r["symbol"].startswith("BTC")
        send_telegram(format_alert(r, caution))
        time.sleep(1)  # تجنب تجاوز حد تيليجرام لعدد الرسائل بالثانية

    # تسجيل الإشارات الجديدة كصفقات مفتوحة قيد المتابعة لاحقًا
    open_new_positions(open_positions, fresh)

    # حفظ موحّد: ذاكرة الإشارات + BTC Dominance + الصفقات المفتوحة + أرشيف الصفقات المغلقة حديثًا
    save_all_state(strong_symbols, btc_dominance, open_positions, closed_now)
    print("انتهى المسح.")


if __name__ == "__main__":
    main()
