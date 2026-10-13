#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest_exit_rules.py
======================
يجيب على سؤال واحد: "لو كنت أستخدم قاعدة خروج مختلفة (Break-even / Trailing / Time-stop)
بدل القاعدة الحالية (TP1 + SL + سقف 96 ساعة)، هل كانت النتيجة أفضل على صفقاتي الفعلية؟"

كيف يعمل:
  1) يحمّل الأرشيف الكامل للصفقات المغلقة: closed_trades.json (النشط) + كل ملفات
     closed_trades_archive_NNNN.json عبر archive_gists_chain.json و archive_index.json
     (نفس تنظيم scanner.py). أي فشل بجلب جزء من الأرشيف يوقف السكربت (إلا مع ALLOW_PARTIAL=1).
  2) لكل صفقة لها market_regime: يجلب شموع 5m من Binance من لحظة الفتح حتى 96 ساعة،
     ويعيد تشغيل كل قاعدة خروج على نفس الشموع (مقارنة مزدوجة paired: نفس الصفقات لكل القواعد).
  3) يطبع لكل market_regime (ثم لكل regime × نوع إشارة): عدد الصفقات، نسبة النجاح، صافي/صفقة،
     R، PF، والفرق عن القاعدة الحالية Δ مع هامش ثقة 95% وفحص ثبات (النصف الأول مقابل الثاني).

افتراضات المحاكاة (مطابقة لـ scanner.py: check_open_positions):
  - الشموع تُفحص بالترتيب، ومع تعارض SL وTP داخل نفس الشمعة يُعتمد SL أولًا (محافظ).
  - الخروج عند سعر الـ SL/TP نفسه (بدون slippage)، والخروج بالـ time-stop عند إغلاق الشمعة.
  - أول شمعة محسوبة هي أول شمعة تبدأ بعد opened_at (كما في scanner.py).
  - نقل الـ SL (BE/Trailing) يُحسب من high الشمعة ويسري من الشمعة التالية (محافظ).
  - الـ SL الأصلي يُعاد بناؤه من initial_risk (لأن حماية الانقلاب REGIME_PROTECT قد غيّرت sl المخزّن).
  - الرسوم TRADING_FEE_PCT (افتراضي 0.2%) تُخصم من كل صفقة.
  - opened_at يُعتبر UTC (تشغيل GitHub Actions). لو مختلف: OPENED_AT_UTC_OFFSET_HOURS.

المتغيرات المطلوبة: GIST_TOKEN, GIST_ID
اختياري:
    TRADING_FEE_PCT (0.2) | BREAKEVEN_BAND_PCT (0.1) | MIN_TRADES_FOR_VERDICT (30)
    TIME_STOP_HOURS (96)  | MONITOR_INTERVAL (5m)    | ALLOW_PARTIAL (0)
    CANDLE_CACHE_DIR (.candle_cache) | OPENED_AT_UTC_OFFSET_HOURS (0)

الاستخدام:
    python backtest_exit_rules.py
    python backtest_exit_rules.py --since "2026-09-17 22:00:00"   # بعد آخر Reset
    python backtest_exit_rules.py --regime ranging --type breakout
    python backtest_exit_rules.py --limit 40                      # تجربة سريعة (أحدث 40 صفقة)
    python backtest_exit_rules.py --dump results.json             # نتائج كل صفقة لكل قاعدة

للقراءة فقط: لا يكتب شيئًا في الـ Gist.
"""

import os
import sys
import json
import math
import time
import argparse
import statistics
import datetime as dt
import concurrent.futures

import requests

# ---------------------------------------------------------------- الإعدادات
GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")

FEE_PCT = float(os.environ.get("TRADING_FEE_PCT", "0.2"))
BE_BAND = float(os.environ.get("BREAKEVEN_BAND_PCT", "0.1"))
MIN_N = int(os.environ.get("MIN_TRADES_FOR_VERDICT", "30"))
MAX_HOURS = float(os.environ.get("TIME_STOP_HOURS", "96"))
CANDLE_INTERVAL = os.environ.get("MONITOR_INTERVAL", "5m")
ALLOW_PARTIAL = os.environ.get("ALLOW_PARTIAL", "0") == "1"
CACHE_DIR = os.environ.get("CANDLE_CACHE_DIR", ".candle_cache")
OPENED_AT_UTC_OFFSET_H = float(os.environ.get("OPENED_AT_UTC_OFFSET_HOURS", "0"))

BASE_URL = "https://data-api.binance.vision/api/v3"
INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000}
CANDLE_MS = INTERVAL_MS.get(CANDLE_INTERVAL, 300_000)

ACTIVE_FILE = "closed_trades.json"
ARCHIVE_PREFIX = "closed_trades_archive_"
ARCHIVE_CHAIN_FILE = "archive_gists_chain.json"
ARCHIVE_INDEX_FILE = "archive_index.json"

TYPES = ("official", "early", "breakout", "experimental")
TYPE_SHORT = {"official": "رسمية", "early": "مبكرة", "breakout": "انفجار", "experimental": "تجريبية"}
REGIMES = ("trending_up", "ranging", "trending_down")
REGIME_LABEL = {"trending_up": "📈 صاعد (trending_up)", "ranging": "➖ عرضي (ranging)",
                "trending_down": "📉 نازل (trending_down)"}

# ---------------------------------------------------------------- قواعد الخروج
# be_r         : عند وصول أعلى سعر إلى +X×R ينتقل الـ SL إلى entry×(1+fee)
# trail_act_r  : بعد وصول +X×R يبدأ الـ trailing
# trail_r      : مسافة الـ trailing = Y×R تحت أعلى سعر وصله (R = المخاطرة الأصلية)
# no_tp        : إلغاء هدف TP1 (يبقى SL والـ trailing والـ time-stop)
# tp_r         : هدف مختلف = Z×R بدل TP1 المخزّن
# time_h       : time-stop بالساعات (الحد الأقصى دائمًا TIME_STOP_HOURS)
# أضف/عدّل قواعدك هنا — "baseline" لا يُحذف (هو القاعدة الحالية للبوت).
RULES = {
    "baseline": {},
    "be_0.5R": {"be_r": 0.5},
    "be_0.7R": {"be_r": 0.7},
    "time_24h": {"time_h": 24},
    "time_36h": {"time_h": 36},
    "time_48h": {"time_h": 48},
    "be0.5R+time36h": {"be_r": 0.5, "time_h": 36},
    "be0.7R+trail1.0R(noTP)": {"be_r": 0.7, "trail_act_r": 0.7, "trail_r": 1.0, "no_tp": True},
    "be0.7R+tp1.5R": {"be_r": 0.7, "tp_r": 1.5},
    "be0.7R+tp2R": {"be_r": 0.7, "tp_r": 2.0},
}

SEP = "═" * 31


# ---------------------------------------------------------------- تحميل الأرشيف الكامل
def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def _fetch_gist_files(gist_id):
    r = requests.get(f"https://api.github.com/gists/{gist_id}", headers=_gist_headers(), timeout=30)
    r.raise_for_status()
    return r.json().get("files", {})


def _read_json(entry, name, default):
    """قراءة كاملة وسليمة لملف JSON: لو المحتوى مبتور/فارغ يُجلب من raw_url. لا يرجع default عند الفشل."""
    content = entry.get("content")
    size = entry.get("size") or 0
    if not entry.get("truncated") and (content or size <= 2):
        try:
            return json.loads(content) if content else default
        except Exception:
            pass
    raw_url = entry.get("raw_url")
    if not raw_url:
        raise RuntimeError(f"{name}: المحتوى مبتور/غير صالح ولا يوجد raw_url")
    rr = requests.get(raw_url, headers=_gist_headers(), timeout=30)
    rr.raise_for_status()
    rr.encoding = "utf-8"
    try:
        return json.loads(rr.text) if rr.text else default
    except Exception as e:
        raise RuntimeError(f"{name}: JSON غير صالح بعد القراءة الكاملة: {e}") from e


def _dedupe(trades):
    seen, out = set(), []
    for t in trades:
        try:
            key = json.dumps(t, sort_keys=True, ensure_ascii=False)
        except Exception:
            out.append(t)
            continue
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out, len(trades) - len(out)


def load_all_trades():
    """السجل النشط + كل ملفات الأرشيف (الـ Gist الرئيسي + كل Gists السلسلة/الفهرس)."""
    if not GIST_TOKEN or not GIST_ID:
        sys.exit("❌ GIST_TOKEN أو GIST_ID غير موجودين في متغيرات البيئة.")

    main_files = _fetch_gist_files(GIST_ID)
    if ACTIVE_FILE not in main_files:
        sys.exit(f"❌ لم يُعثر على '{ACTIVE_FILE}' في الـ Gist. الملفات: {list(main_files)[:20]}")

    problems, notes, trades = [], [], []

    ids = []
    for fname, kind in ((ARCHIVE_CHAIN_FILE, list), (ARCHIVE_INDEX_FILE, dict)):
        if fname in main_files:
            try:
                data = _read_json(main_files[fname], fname, kind())
                if isinstance(data, list):
                    ids.extend(str(x) for x in data if x)
                elif isinstance(data, dict):
                    ids.extend(str(k) for k in data.keys())
            except Exception as e:
                problems.append(f"{fname}: {e}")
    ids.append(GIST_ID)  # ملفات أرشيف داخل الـ Gist الرئيسي نفسه (إن وُجدت)
    ids = list(dict.fromkeys(ids))
    notes.append(f"عدد Gists الأرشيف المفحوصة: {len(ids)}")

    for gid in ids:
        try:
            files = main_files if gid == GIST_ID else _fetch_gist_files(gid)
            names = sorted(fn for fn in files if fn.startswith(ARCHIVE_PREFIX))
            cnt = 0
            for nm in names:
                part = _read_json(files[nm], nm, [])
                cnt += len(part)
                trades.extend(part)
            notes.append(f"[{'الرئيسي' if gid == GIST_ID else gid[:8] + '…'}] {len(names)} ملف أرشيف | {cnt} صفقة")
        except Exception as e:
            problems.append(f"Gist {gid}: {e}")

    if problems:
        msg = "⚠️ فشل جلب جزء من الأرشيف:\n   - " + "\n   - ".join(problems)
        if ALLOW_PARTIAL:
            print(msg + "\n   (ALLOW_PARTIAL=1 → سيكمل بنتائج ناقصة)")
            notes.append("⚠️ النتائج ناقصة بسبب فشل جلب بعض الأرشيف")
        else:
            sys.exit(msg + "\nتوقّف لتجنّب نتائج مضلّلة. أعد المحاولة أو ضع ALLOW_PARTIAL=1.")

    try:
        active = _read_json(main_files[ACTIVE_FILE], ACTIVE_FILE, [])
    except Exception as e:
        sys.exit(f"❌ تعذّرت قراءة {ACTIVE_FILE}: {e}")
    notes.append(f"{ACTIVE_FILE} (النشط): {len(active)} صفقة")
    trades.extend(active)

    trades, removed = _dedupe(trades)
    if removed:
        notes.append(f"حُذف {removed} سجل مكرر تمامًا")
    return trades, notes


# ---------------------------------------------------------------- الوقت والشموع
def parse_ts_ms(s):
    try:
        t = dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None
    t = t.replace(tzinfo=dt.timezone.utc) - dt.timedelta(hours=OPENED_AT_UTC_OFFSET_H)
    return int(t.timestamp() * 1000)


def _http_get_klines(params, retries=4):
    """طلب klines مع إعادة محاولة. يرجع None لو الرمز غير موجود (400)."""
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(f"{BASE_URL}/klines", params=params, timeout=30)
            if r.status_code == 400:
                return None
            if r.status_code in (418, 429) or r.status_code >= 500:
                try:
                    wait = int(r.headers.get("Retry-After", 5 * (attempt + 1)))
                except ValueError:
                    wait = 5 * (attempt + 1)
                last = f"HTTP {r.status_code}"
                time.sleep(min(wait, 60))
                continue
            r.raise_for_status()
            try:
                used = int(r.headers.get("X-MBX-USED-WEIGHT-1M", 0) or 0)
            except ValueError:
                used = 0
            if used >= 900:
                time.sleep(30)
            return r.json()
        except requests.RequestException as e:
            last = str(e)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"فشل جلب klines ({params.get('symbol')}): {last}")


def fetch_candles(symbol, start_ms, end_ms):
    """شموع مغلقة تبدأ بعد start_ms وحتى end_ms (أو الآن). كل شمعة: [open_ms, close_ms, o, h, l, c].
    يرجع None لو الرمز غير موجود. النتائج المكتملة تُخزَّن محليًا (CACHE_DIR)."""
    now_ms = int(time.time() * 1000)
    end_ms = min(end_ms, now_ms)
    cache_ok = end_ms < now_ms - 3_600_000
    path = os.path.join(CACHE_DIR, f"{symbol}_{CANDLE_INTERVAL}_{start_ms}_{end_ms}.json")
    if cache_ok and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    raw, cursor = [], start_ms + 1
    while cursor < end_ms:
        batch = _http_get_klines({"symbol": symbol, "interval": CANDLE_INTERVAL,
                                  "startTime": cursor, "endTime": end_ms, "limit": 1000})
        if batch is None:
            return None
        if not batch:
            break
        raw.extend(batch)
        if len(batch) < 1000:
            break
        cursor = int(batch[-1][0]) + 1
        time.sleep(0.05)

    out, seen = [], set()
    for k in raw:
        o, c = int(k[0]), int(k[6])
        if o <= start_ms or c > now_ms or o in seen:
            continue
        seen.add(o)
        out.append([o, c, float(k[1]), float(k[2]), float(k[3]), float(k[4])])
    out.sort(key=lambda x: x[0])

    if cache_ok:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f, separators=(",", ":"))
        except Exception:
            pass
    return out


# ---------------------------------------------------------------- محاكاة قاعدة خروج
def simulate(entry, sl0, tp1, opened_ms, candles, rule, fee_pct=FEE_PCT, max_hours=MAX_HOURS):
    """يرجع (exit_price, reason) أو (None, 'UNRESOLVED') لو الشموع انتهت قبل أي خروج.
    reason: SL | STOP_MOVED (SL بعد نقله BE/Trailing) | TP | TIME"""
    risk = entry - sl0
    be_r = rule.get("be_r")
    trail_act = rule.get("trail_act_r")
    trail_r = rule.get("trail_r")
    if rule.get("no_tp"):
        tp = None
    elif rule.get("tp_r"):
        tp = entry + rule["tp_r"] * risk
    else:
        tp = tp1
    time_h = min(rule.get("time_h", max_hours), max_hours)
    be_price = entry * (1 + fee_pct / 100.0)

    stop = sl0
    max_high = entry
    for open_ms, close_ms, _o, high, low, close in candles:
        # 1) SL أولًا عند التعارض (محافظ، مثل scanner.py)
        if low <= stop:
            return stop, ("SL" if stop <= sl0 else "STOP_MOVED")
        # 2) الهدف
        if tp is not None and high >= tp:
            return tp, "TP"
        # 3) تحديث الـ SL (يسري من الشمعة التالية)
        if high > max_high:
            max_high = high
        reached = (max_high - entry) / risk
        if be_r is not None and reached >= be_r:
            stop = max(stop, be_price)
        if trail_r is not None and trail_act is not None and reached >= trail_act:
            stop = max(stop, max_high - trail_r * risk)
        # 4) time-stop عند إغلاق الشمعة
        if (close_ms - opened_ms) / 3_600_000.0 >= time_h:
            return close, "TIME"
    return None, "UNRESOLVED"


def net_pct(entry, exit_price):
    return (exit_price / entry - 1.0) * 100.0 - FEE_PCT


# ---------------------------------------------------------------- تجهيز الصفقات
def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def prepare_trade(t):
    """يرجع (dict جاهز | None, سبب الاستبعاد)."""
    if t.get("type", "official") not in TYPES:
        return None, "type"
    if t.get("market_regime") not in REGIMES:
        return None, "no_regime"
    entry = _num(t.get("entry"))
    tps = t.get("tps") or []
    tp1 = _num(tps[0]) if tps else None
    risk = _num(t.get("initial_risk"))
    if not risk or risk <= 0:
        sl = _num(t.get("sl"))
        risk = (entry - sl) if (entry and sl) else None
    opened_ms = parse_ts_ms(t.get("opened_at"))
    if not entry or entry <= 0 or not risk or risk <= 0 or risk >= entry or not tp1 or tp1 <= entry \
            or opened_ms is None or not t.get("symbol"):
        return None, "bad_fields"
    return {
        "symbol": t["symbol"], "type": t.get("type", "official"), "regime": t["market_regime"],
        "entry": entry, "sl0": entry - risk, "tp1": tp1, "risk_pct": risk / entry * 100.0,
        "opened_ms": opened_ms, "opened_at": t.get("opened_at"),
        "recorded_reason": t.get("closed_reason"),
    }, None


def process_trade(p):
    candles = fetch_candles(p["symbol"], p["opened_ms"],
                            p["opened_ms"] + int(MAX_HOURS * 3_600_000) + CANDLE_MS)
    if not candles:
        return None
    res = {}
    for name, rule in RULES.items():
        ex, reason = simulate(p["entry"], p["sl0"], p["tp1"], p["opened_ms"], candles, rule)
        if ex is None:
            res[name] = None
        else:
            n = net_pct(p["entry"], ex)
            res[name] = {"exit": ex, "reason": reason, "net": n, "r": n / p["risk_pct"]}
    return {**p, "res": res}


# ---------------------------------------------------------------- الإحصاء
def mean_ci(vals):
    n = len(vals)
    if n == 0:
        return None
    m = sum(vals) / n
    sd = statistics.stdev(vals) if n >= 2 else 0.0
    se = sd / math.sqrt(n) if n >= 2 else 0.0
    return m, m - 1.96 * se, m + 1.96 * se


def rule_stats(recs, name):
    nets = [r["res"][name]["net"] for r in recs]
    rs = [r["res"][name]["r"] for r in recs]
    wins = [x for x in nets if x > BE_BAND]
    losses = [x for x in nets if x < -BE_BAND]
    gl = -sum(losses)
    return {
        "n": len(nets), "wr": len(wins) / len(nets) * 100 if nets else 0.0,
        "mean": sum(nets) / len(nets) if nets else 0.0,
        "mean_r": sum(rs) / len(rs) if rs else 0.0,
        "pf": (sum(wins) / gl) if gl > 0 else None,
    }


def delta_stats(recs, name):
    """الفرق (قاعدة − baseline) لكل صفقة، مع هامش ثقة وفحص ثبات بين نصفَي الفترة."""
    diffs = [r["res"][name]["net"] - r["res"]["baseline"]["net"] for r in recs]
    m = mean_ci(diffs)
    if m is None:
        return None
    mean, lo, hi = m
    half = len(diffs) // 2
    first, second = diffs[:half], diffs[half:]
    stable = None
    if len(first) >= 5 and len(second) >= 5:
        a, b = sum(first) / len(first), sum(second) / len(second)
        if abs(a) < 1e-12 and abs(b) < 1e-12:
            stable = None  # القاعدة لم تغيّر أي نتيجة
        else:
            stable = (a > 0 and b > 0) or (a < 0 and b < 0)
    if len(diffs) < MIN_N:
        icon = "⚪"
    elif lo > 0:
        icon = "🟢"
    elif hi < 0:
        icon = "🔴"
    else:
        icon = "🟡"
    return {"mean": mean, "lo": lo, "hi": hi, "icon": icon, "stable": stable, "n": len(diffs)}


def fmt_pf(pf):
    return "∞" if pf is None else f"{pf:.2f}"


def print_group(title, recs, detailed=True):
    """recs مرتبة زمنيًا حسب opened_ms (مهم لفحص الثبات)."""
    n = len(recs)
    print(f"\n{SEP}\n{title} — n={n}\n{SEP}")
    if n == 0:
        print("لا صفقات.")
        return []
    base = rule_stats(recs, "baseline")
    print(f"القاعدة الحالية (baseline): نجاح {base['wr']:.0f}% | صافي/صفقة {base['mean']:+.2f}% | "
          f"R {base['mean_r']:+.2f} | PF {fmt_pf(base['pf'])}")
    if n < MIN_N:
        print(f"⚪ عيّنة صغيرة ({n}/{MIN_N}) — لا مقارنة بين القواعد (أي فرق هنا ضجيج غالبًا).")
        return []
    if not detailed:
        return []
    winners = []
    for name in RULES:
        if name == "baseline":
            continue
        s, d = rule_stats(recs, name), delta_stats(recs, name)
        st = {True: "ثبات ✔", False: "ثبات ✘", None: "ثبات —"}[d["stable"]]
        print(f"{name}: نجاح {s['wr']:.0f}% | صافي {s['mean']:+.2f}% | R {s['mean_r']:+.2f} | PF {fmt_pf(s['pf'])}\n"
              f"    Δ vs baseline {d['mean']:+.2f}% [{d['lo']:+.2f}, {d['hi']:+.2f}] {d['icon']} | {st}")
        if d["icon"] == "🟢" and d["stable"]:
            winners.append((name, d["mean"]))
    return winners


# ---------------------------------------------------------------- التشغيل
def parse_since(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    sys.exit("❌ صيغة --since غير صحيحة، المطلوب: 'YYYY-MM-DD HH:MM:SS' أو 'YYYY-MM-DD'")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="فقط الصفقات المفتوحة بعد هذا الوقت (مثلاً لحظة Reset)")
    ap.add_argument("--regime", choices=REGIMES, default=None)
    ap.add_argument("--type", choices=TYPES, default=None)
    ap.add_argument("--limit", type=int, default=None, help="أحدث N صفقة فقط (للتجربة السريعة)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dump", default=None, help="مسار ملف JSON لحفظ نتائج كل صفقة")
    args = ap.parse_args()

    since_ms = int(parse_since(args.since).timestamp() * 1000) if args.since else None

    trades, notes = load_all_trades()
    print("📥 مصدر البيانات: Gist (النشط + كل الأرشيف)")
    for n in notes:
        print(f"   - {n}")
    print(f"   إجمالي المحمَّل: {len(trades)} صفقة")

    prepared, skipped = [], {}
    for t in trades:
        p, why = prepare_trade(t)
        if p is None:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        if since_ms is not None and p["opened_ms"] < since_ms:
            skipped["before_since"] = skipped.get("before_since", 0) + 1
            continue
        if args.regime and p["regime"] != args.regime:
            continue
        if args.type and p["type"] != args.type:
            continue
        prepared.append(p)
    prepared.sort(key=lambda p: p["opened_ms"])
    if args.limit:
        prepared = prepared[-args.limit:]
    print(f"   مؤهلة للمحاكاة: {len(prepared)} | مستبعدة: {skipped or 'لا شيء'}")
    if not prepared:
        sys.exit("لا توجد صفقات مؤهلة (تحتاج market_regime + entry + initial_risk/sl + tps).")

    os.makedirs(CACHE_DIR, exist_ok=True)
    print(f"⏳ جلب الشموع ({CANDLE_INTERVAL}) ومحاكاة {len(RULES)} قاعدة لكل صفقة…")
    records, no_candles = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for i, rec in enumerate(pool.map(process_trade, prepared), 1):
            if rec is None:
                no_candles += 1
            else:
                records.append(rec)
            if i % 25 == 0 or i == len(prepared):
                print(f"   {i}/{len(prepared)}")

    # مقارنة مزدوجة عادلة: فقط الصفقات التي حُسمت تحت كل القواعد
    paired = [r for r in records if all(v is not None for v in r["res"].values())]
    unresolved = len(records) - len(paired)
    paired.sort(key=lambda r: r["opened_ms"])
    print(f"✅ محاكاة مكتملة: {len(paired)} صفقة | بلا شموع (رمز مشطوب/فشل): {no_candles} | "
          f"غير محسومة بعد (شموع لا تغطي {MAX_HOURS:.0f}س): {unresolved}")
    if not paired:
        sys.exit("لا صفقات محسومة لإجراء المقارنة.")

    # فحص أمانة المحاكاة: هل baseline يعيد إنتاج النتائج المسجّلة فعلًا؟
    reason_map = {"TP": "TP1", "SL": "SL", "TIME": "EXPIRED"}
    comparable = [r for r in paired if r["recorded_reason"] in ("TP1", "SL", "EXPIRED")]
    if comparable:
        ok = sum(1 for r in comparable if reason_map.get(r["res"]["baseline"]["reason"]) == r["recorded_reason"])
        rate = ok / len(comparable) * 100
        print(f"🔎 أمانة المحاكاة: baseline طابق السبب المسجّل في {ok}/{len(comparable)} صفقة ({rate:.0f}%)"
              f" — صفقات REGIME_EXIT مستثناة من الفحص.")
        if rate < 90:
            print("   ⚠️ أقل من 90%: النتائج أدناه غير موثوقة. راجع المنطقة الزمنية (OPENED_AT_UTC_OFFSET_HOURS) "
                  "أو فجوات الشموع قبل الاعتماد على أي استنتاج.")

    # ---- التقارير ----
    print_group("📊 الكل (كل الأنظمة والأنواع)", paired, detailed=False)
    all_winners = {}
    for regime in REGIMES:
        if args.regime and regime != args.regime:
            continue
        group = [r for r in paired if r["regime"] == regime]
        all_winners[regime] = print_group(REGIME_LABEL[regime], group)
        for ttype in TYPES:
            sub = [r for r in group if r["type"] == ttype]
            if sub:
                print_group(f"{REGIME_LABEL[regime]} × {TYPE_SHORT[ttype]}", sub)

    # ---- الخلاصة ----
    print(f"\n{SEP}\n🏁 الخلاصة (على مستوى نظام السوق، n ≥ {MIN_N})\n{SEP}")
    for regime, winners in all_winners.items():
        if not winners:
            print(f"{REGIME_LABEL[regime]}: لا قاعدة أثبتت تفوقًا ثابتًا على baseline → أبقِ القاعدة الحالية.")
        else:
            best = ", ".join(f"{n} ({d:+.2f}%/صفقة)" for n, d in sorted(winners, key=lambda x: -x[1]))
            print(f"{REGIME_LABEL[regime]}: مرشّحة للتجربة على المحفظة الوهمية: {best}")
    print("\nتحذيرات:")
    print(f"- اختبرنا {len(RULES) - 1} قاعدة في عدة خلايا: بعض نتائج 🟢 ستظهر بالصدفة. اعتمد فقط ما ثبت بالنصفين (ثبات ✔) "
          f"وأعد الفحص لاحقًا على صفقات جديدة قبل التطبيق.")
    print("- المحاكاة تفترض تنفيذ بسعر SL/TP نفسه بدون slippage، ولا تحاكي فجوة إلغاء/إعادة إنشاء أمر OCO.")
    print("- الصفقات المغلقة فقط (المفتوحة حاليًا غير مشمولة)، والفرق Δ تقريبي (فاصل ثقة 95% طبيعي).")

    if args.dump:
        slim = [{k: v for k, v in r.items()} for r in paired]
        with open(args.dump, "w", encoding="utf-8") as f:
            json.dump(slim, f, ensure_ascii=False)
        print(f"💾 حُفظت نتائج {len(slim)} صفقة في {args.dump}")


if __name__ == "__main__":
    main()
