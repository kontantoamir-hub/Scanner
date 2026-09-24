#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paper_stats.py — تقرير أداء المحفظة الوهمية (Paper Trading)
=============================================================
نظير trade_stats.py لكن للمحفظة الوهمية: يجيب "أي نوع إشارة هو الأفضل فعليًا؟"
(official / early / breakout / experimental) ثم يفصّل داخل كل نوع أي العوامل/الظروف تنجح.

يحسب الآن 5 محافظ وهمية:
  1) الرئيسية: تتداول كل الإشارات معًا (الصفقات بدون حقل "portfolio")
  2-5) أربع محافظ فرعية مستقلة، واحدة لكل نوع إشارة (الحقل "portfolio" = sub_official / sub_early /
       sub_breakout / sub_experimental)، لكلٍّ رأس مال مستقل (400$ افتراضيًا)
ويعرض مقارنة بين الخمس ثم تفصيلًا لكل محفظة. الحالة الحالية للفرعية من paper_sub_portfolios.json.

التحميل: السجل النشط (paper_closed_trades.json) + كل ملفات الأرشيف (paper_closed_trades_archive_NNNN.json)
عبر paper_archive_gists_chain.json و paper_archive_index.json — سواء داخل الـGist الرئيسي أو Gists الأرشيف المنفصلة.
للقراءة فقط: لا يعدّل شيئًا في الـGist.

المتغيرات (Secrets):
    GIST_TOKEN (أو PAPER_GIST_TOKEN)   توكن GitHub
    PAPER_GIST_ID                      افتراضي: نفس المعرّف الموجود في paper_trading.py
    PAPER_TELEGRAM_TOKEN, PAPER_TELEGRAM_CHAT_ID   اختياري لإرسال التقرير
اختياري:
    PAPER_TRADING_FEE_PCT   (0.2)  يُستخدم فقط لو الصفقة بلا pnl_pct مخزّن
    BREAKEVEN_BAND_PCT      (0.1)
    MIN_TRADES_FOR_VERDICT  (30)
    ALLOW_PARTIAL           لو 1: يكمل حتى لو فشل جلب Gist أرشيف

الاستخدام:
    python paper_stats.py
    python paper_stats.py --since "2026-09-17 22:00:00"     # فقط الصفقات المغلقة بعد لحظة الـ Reset
    python paper_stats.py --portfolio early                 # محفظة واحدة فقط: main/official/early/breakout/experimental
"""

import os
import sys
import json
import math
import argparse
import statistics
import datetime as dt

import requests

# ---------------------------------------------------------------- الإعدادات
GIST_TOKEN = os.environ.get("PAPER_GIST_TOKEN") or os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("PAPER_GIST_ID", "af82b35a4fde92f671d596bc6c18f4f2")
TELEGRAM_TOKEN = os.environ.get("PAPER_TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("PAPER_TELEGRAM_CHAT_ID")

CLOSED_FILE = "paper_closed_trades.json"
ARCHIVE_PREFIX = "paper_closed_trades_archive_"
ARCHIVE_CHAIN_FILE = "paper_archive_gists_chain.json"
ARCHIVE_INDEX_FILE = "paper_archive_index.json"
BALANCE_FILE = "paper_balance.json"
POSITIONS_FILE = "paper_positions.json"
SUB_STATE_FILE = "paper_sub_portfolios.json"   # {type: {"balance": {...}, "positions": [...]}}

FEE_PCT = float(os.environ.get("PAPER_TRADING_FEE_PCT", "0.2"))
BE_BAND = float(os.environ.get("BREAKEVEN_BAND_PCT", "0.1"))
MIN_N = int(os.environ.get("MIN_TRADES_FOR_VERDICT", "30"))
ALLOW_PARTIAL = os.environ.get("ALLOW_PARTIAL", "0") == "1"
DEFAULT_SIZE = 50.0  # حجم الشريحة الافتراضي لو الصفقة بلا size_usd

TYPES = [
    ("official", "🔴 رسمية"),
    ("early", "🔵 مبكرة"),
    ("breakout", "🟠 انفجار"),
    ("experimental", "🟣 تجريبية"),
]
TYPE_KEYS = [k for k, _ in TYPES]
TYPE_LABEL = dict(TYPES)
DEFAULT_CAPITAL = 400.0
# المحافظ الخمس: الرئيسية + فرعية لكل نوع. مفتاح الصفقة = حقل "portfolio" (غيابه = main)
PORTFOLIOS = [("main", "💼 الرئيسية (كل الإشارات)")] + [(f"sub_{k}", f"محفظة {lbl}") for k, lbl in TYPES]
PORTFOLIO_KEYS = [k for k, _ in PORTFOLIOS]
TYPE_SHORT = {"official": "رسمية", "early": "مبكرة", "breakout": "انفجار", "experimental": "تجريبية"}

FLAG_KEYS = [
    ("squeeze", "انضغاط تقلب (Squeeze)"),
    ("accumulation", "تراكم صامت (Accumulation)"),
    ("divergence", "دايفرجنس (Divergence)"),
    ("momentum", "زخم (Momentum)"),
    ("extended", "امتداد زائد (Overextension)"),
    ("vol_confirm", "تأكيد الحجم"),
    ("obv_confirm", "تأكيد OBV"),
    ("htf_aligned", "توافق فريم أعلى"),
    ("di_confirm", "تأكيد +DI/-DI"),
    ("momentum_agree", "اتفاق EMA/MACD"),
    ("macd_bull", "MACD إيجابي"),
    ("trend_up", "اتجاه EMA صاعد"),
    ("ranging", "سوق عرضي (ADX منخفض)"),
    ("near_resistance", "قرب مقاومة"),
]
BREAKOUT_FACTORS = {"trend_support": "دعم اتجاه EMA7/14", "macd_bull": "MACD إيجابي", "rsi_ok": "RSI صحي"}
EXPERIMENTAL_FACTORS = {"trend_support": "دعم اتجاه EMA7/14", "volume_confirm": "تأكيد حجم + OBV",
                        "mfi_bullish": "تدفق أموال صاعد (MFI)"}
RSI_LABELS = {1: "تشبع بيعي (RSI<35)", 0: "محايد", -1: "تشبع شرائي (RSI>65)"}
BB_LABELS = {1: "عند الحد السفلي", 0: "منتصف النطاق", -1: "عند الحد العلوي"}

SEP = "═" * 31


# ---------------------------------------------------------------- تحميل البيانات
def _headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def _fetch_gist_files(gist_id):
    r = requests.get(f"https://api.github.com/gists/{gist_id}", headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json().get("files", {})


def _read_json_file(entry, filename, default):
    content = entry.get("content")
    if entry.get("truncated") or content is None:
        try:
            rr = requests.get(entry.get("raw_url"), headers=_headers(), timeout=30)
            rr.raise_for_status()
            content = rr.text
        except Exception as e:
            raise RuntimeError(f"تعذّر جلب المحتوى الكامل لـ {filename}: {e}") from e
    return json.loads(content) if content else default


def _archive_names(files):
    return sorted(fn for fn in files if fn.startswith(ARCHIVE_PREFIX))


def _archive_gist_ids(main_files):
    ids = []
    if ARCHIVE_CHAIN_FILE in main_files:
        try:
            chain = _read_json_file(main_files[ARCHIVE_CHAIN_FILE], ARCHIVE_CHAIN_FILE, [])
            if isinstance(chain, list):
                ids.extend(str(x) for x in chain if x)
        except Exception as e:
            print(f"⚠️ تعذّر تحليل {ARCHIVE_CHAIN_FILE}: {e}")
    if ARCHIVE_INDEX_FILE in main_files:
        try:
            index = _read_json_file(main_files[ARCHIVE_INDEX_FILE], ARCHIVE_INDEX_FILE, {})
            if isinstance(index, dict):
                ids.extend(str(k) for k in index.keys())
        except Exception:
            pass
    seen, out = set(), []
    for g in ids:
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out


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


def load_all():
    """يرجع (كل الصفقات المغلقة من السجل النشط + كل الأرشيف، ملاحظات، balance، الصفقات المفتوحة،
    حالة المحافظ الفرعية)."""
    if not GIST_TOKEN or not GIST_ID:
        sys.exit("❌ GIST_TOKEN أو PAPER_GIST_ID غير موجودين.")
    main_files = _fetch_gist_files(GIST_ID)
    if CLOSED_FILE not in main_files:
        sys.exit(f"لم يتم العثور على '{CLOSED_FILE}' داخل Gist المحفظة الوهمية. الملفات: {list(main_files)[:20]}")

    trades, problems, notes = [], [], []

    for name in _archive_names(main_files):
        part = _read_json_file(main_files[name], name, [])
        notes.append(f"[الرئيسي] {name}: {len(part)} صفقة")
        trades.extend(part)

    ids = _archive_gist_ids(main_files)
    notes.append(f"عدد Gists الأرشيف في السلسلة: {len(ids)}")
    for aid in ids:
        if aid == GIST_ID:
            continue
        try:
            files = _fetch_gist_files(aid)
            cnt = 0
            for name in _archive_names(files):
                part = _read_json_file(files[name], name, [])
                cnt += len(part)
                trades.extend(part)
            notes.append(f"[أرشيف {aid[:8]}…] {cnt} صفقة")
        except Exception as e:
            problems.append(f"Gist أرشيف {aid}: {e}")

    if problems:
        msg = "⚠️ فشل جلب جزء من الأرشيف:\n   - " + "\n   - ".join(problems)
        if ALLOW_PARTIAL:
            print(msg + "\n   (ALLOW_PARTIAL=1 → سيكمل بنتائج ناقصة)")
            notes.append("⚠️ النتائج ناقصة بسبب فشل جلب بعض الأرشيف")
        else:
            sys.exit(msg + "\nتوقّف لتجنّب تقرير مضلّل. أعد المحاولة أو ضع ALLOW_PARTIAL=1.")

    active = _read_json_file(main_files[CLOSED_FILE], CLOSED_FILE, [])
    notes.append(f"{CLOSED_FILE} (النشط): {len(active)} صفقة")
    trades.extend(active)

    trades, removed = _dedupe(trades)
    if removed:
        notes.append(f"تم حذف {removed} سجل مكرر تمامًا")

    balance, positions, sub_state = {}, [], {}
    try:
        if BALANCE_FILE in main_files:
            balance = _read_json_file(main_files[BALANCE_FILE], BALANCE_FILE, {}) or {}
        if POSITIONS_FILE in main_files:
            positions = _read_json_file(main_files[POSITIONS_FILE], POSITIONS_FILE, []) or []
    except Exception as e:
        print(f"⚠️ تعذّر قراءة الرصيد/الصفقات المفتوحة: {e}")
    try:
        if SUB_STATE_FILE in main_files:
            sub_state = _read_json_file(main_files[SUB_STATE_FILE], SUB_STATE_FILE, {}) or {}
        else:
            notes.append(f"{SUB_STATE_FILE} غير موجود بعد — المحافظ الفرعية لم تبدأ (شغّل paper_trading.py المحدّث أولًا)")
    except Exception as e:
        print(f"⚠️ تعذّر قراءة حالة المحافظ الفرعية: {e}")
    return trades, notes, balance, positions, sub_state


# ---------------------------------------------------------------- الحسابات
def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse(s):
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def prep(trades, since=None):
    """يضيف للصفقة: _pct (صافي%)، _usd، _h (مدة بالساعات)، _opened، _closed. يتجاهل ما لا يمكن حساب ربحه."""
    out = []
    for t in trades:
        if t.get("type") not in TYPE_KEYS:
            continue
        pct = _num(t.get("pnl_pct"))
        if pct is None:
            e, x = _num(t.get("entry")), _num(t.get("exit_price"))
            if e and x and e > 0 and x > 0:
                pct = (x / e - 1) * 100 - FEE_PCT
        if pct is None:
            continue
        size = _num(t.get("size_usd")) or DEFAULT_SIZE
        usd = _num(t.get("pnl_usd"))
        if usd is None:
            usd = size * pct / 100
        opened, closed = _parse(t.get("opened_at")), _parse(t.get("closed_at"))
        if since and (closed is None or closed < since):
            continue
        hours = (closed - opened).total_seconds() / 3600 if opened and closed else None
        pf = t.get("portfolio") or "main"   # بلا حقل = المحفظة الرئيسية (توافق مع الأرشيف القديم)
        if pf not in PORTFOLIO_KEYS:
            continue
        out.append({**t, "_pct": pct, "_usd": usd, "_size": size, "_h": hours, "_opened": opened,
                    "_closed": closed, "_pf": pf})
    return out


def analyze(group):
    n = len(group)
    res = {"n": n}
    if n == 0:
        return res
    pcts = [t["_pct"] for t in group]
    wins = [p for p in pcts if p > BE_BAND]
    losses = [p for p in pcts if p < -BE_BAND]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    mean = sum(pcts) / n
    sd = statistics.stdev(pcts) if n >= 2 else 0.0
    se = sd / math.sqrt(n) if n >= 2 else 0.0
    gw, gl = sum(wins), abs(sum(losses))

    # عائد على رأس المال المحجوز: ربح $ ÷ (حجم × أيام احتفاظ) — يعاقب النوع الذي يحبس الشرائح طويلًا
    locked = sum(t["_size"] * t["_h"] / 24 for t in group if t["_h"] is not None)
    usd_timed = sum(t["_usd"] for t in group if t["_h"] is not None)
    hours = [t["_h"] for t in group if t["_h"] is not None]

    r_list = []
    for t in group:
        risk = _num(t.get("initial_risk"))
        e = _num(t.get("entry"))
        if not risk and e and _num(t.get("sl")):
            risk = e - _num(t.get("sl"))
        if risk and e and risk > 0:
            r_list.append(t["_pct"] / (risk / e * 100))

    running = peak = dd = 0.0
    for t in sorted(group, key=lambda x: x["_closed"] or dt.datetime.min):
        running += t["_usd"]
        peak = max(peak, running)
        dd = max(dd, peak - running)

    reasons = {}
    for t in group:
        k = t.get("closed_reason", "?")
        reasons[k] = reasons.get(k, 0) + 1

    res.update({
        "wins": len(wins), "losses": len(losses), "flat": n - len(wins) - len(losses),
        "wr": len(wins) / n * 100,
        "be_wr": (abs(avg_loss) / (avg_win + abs(avg_loss)) * 100) if (wins and losses) else None,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "mean": mean, "lo": mean - 1.96 * se, "hi": mean + 1.96 * se,
        "pf": (gw / gl) if gl > 0 else None,
        "total_usd": sum(t["_usd"] for t in group),
        "avg_h": (sum(hours) / len(hours)) if hours else None,
        "locked_ret": (usd_timed / locked * 100) if locked > 0 else None,
        "avg_r": (sum(r_list) / len(r_list)) if r_list else None,
        "max_dd": dd, "reasons": reasons,
    })
    return res


def verdict_icon(r):
    if r["n"] < MIN_N:
        return "⚪"
    if r["lo"] > 0:
        return "🟢"
    if r["hi"] < 0:
        return "🔴"
    return "🟡"


def verdict_text(r):
    n = r["n"]
    if n < MIN_N:
        lean = "إيجابي" if r["mean"] > 0 else "سلبي"
        return f"⚪ عيّنة صغيرة ({n}/{MIN_N}) — لا حكم بعد، الاتجاه الأولي {lean}"
    if r["lo"] > 0:
        return "🟢 إيجابي (بثقة تقريبية 95%)"
    if r["hi"] < 0:
        return "🔴 سلبي (بثقة تقريبية 95%)"
    return "🟡 غير حاسم — هامش الثقة يشمل الصفر، يلزم صفقات أكثر"


def pf_text(r):
    return f"PF {r['pf']:.2f}" if r.get("pf") is not None else "PF ∞"


def fmt_group(label, subset):
    r = analyze(subset)
    if r["n"] == 0:
        return None
    if r["n"] < MIN_N:
        return (f"{label}: {r['n']} ⚪ عيّنة صغيرة (رابحة {r['wins']} / خاسرة {r['losses']}"
                f" | صافي {r['mean']:+.2f}%)")
    return f"{label}: {r['n']} | نجاح {r['wr']:.0f}% | صافي {r['mean']:+.2f}% | {pf_text(r)} {verdict_icon(r)}"


def section(title, rows):
    rows = [x for x in rows if x]
    return ["", f"— {title} —"] + rows if rows else []


def bucket_rows(group, fn, order, indent="  "):
    d = {}
    for t in group:
        b = fn(t)
        if b is not None:
            d.setdefault(b, []).append(t)
    return [fmt_group(f"{indent}{b}", d[b]) for b in order if b in d]


# ---------------------------------------------------------------- الأقسام
def type_block(ttype, group):
    r = analyze(group)
    lines = [SEP, f"{TYPE_LABEL[ttype]} — {r['n']} صفقة", SEP]
    if r["n"] == 0:
        lines.append("لا توجد صفقات مغلقة لهذا النوع بعد.")
        return lines, r
    lines.append(f"رابحة {r['wins']} (متوسط {r['avg_win']:+.2f}%) | خاسرة {r['losses']} (متوسط {r['avg_loss']:+.2f}%)"
                 + (f" | تعادل {r['flat']}" if r["flat"] else ""))
    lines.append(f"نجاح فعلي {r['wr']:.1f}%" + (f" | مطلوب للتعادل {r['be_wr']:.1f}%" if r["be_wr"] is not None else ""))
    lines.append(f"صافي/صفقة {r['mean']:+.2f}% (95%: {r['lo']:+.2f}% إلى {r['hi']:+.2f}%) | {pf_text(r)}")
    lines.append(f"الصافي بالدولار {r['total_usd']:+.2f}$ | أقصى تراجع {r['max_dd']:.2f}$")
    if r["avg_h"] is not None:
        lines.append(f"متوسط مدة الصفقة {r['avg_h']:.1f}س"
                     + (f" | عائد/24س على رأس المال المحجوز {r['locked_ret']:+.2f}%" if r["locked_ret"] is not None else ""))
    if r["avg_r"] is not None:
        lines.append(f"متوسط R: {r['avg_r']:+.2f}")
    reasons = " | ".join(f"{k} {v}" for k, v in sorted(r["reasons"].items()))
    lines.append(f"طرق الإغلاق: {reasons}")
    lines.append(f"الحكم: {verdict_text(r)}")

    # SL التي اقتربت من TP1 (نحتاج max_high المحفوظ من الصفقات الجديدة فقط)
    sl_trades = [t for t in group if t.get("closed_reason") == "SL" and _num(t.get("max_high")) and t.get("tps")]
    if sl_trades:
        near = 0
        for t in sl_trades:
            e, tp = _num(t["entry"]), _num(t["tps"][0])
            if tp and tp > e and (_num(t["max_high"]) - e) / (tp - e) >= 0.5:
                near += 1
        lines.append(f"خسائر SL التي قطعت ≥50% من الطريق لـTP1: {near} من {len(sl_trades)}")
    exp_trades = [t for t in group if t.get("closed_reason") == "EXPIRED"]
    if exp_trades:
        lines.append(f"انتهت بسقف زمني (EXPIRED): {len(exp_trades)} | متوسط عائدها "
                     f"{sum(t['_pct'] for t in exp_trades) / len(exp_trades):+.2f}%")

    lines += details_lines(ttype, group)
    return lines, r


def details_lines(ttype, group):
    out = []

    # --- العوامل البولية (تظهر فقط للصفقات التي حُفظت بها الحقول التشخيصية) ---
    rows = []
    for key, label in FLAG_KEYS:
        yes = [t for t in group if t.get(key) is True]
        no = [t for t in group if t.get(key) is False]
        if not yes and not no:
            continue
        rows.append(fmt_group(f"{label} ✔", yes))
        rows.append(fmt_group(f"{label} ✘", no))
    out += section("حسب العوامل (✔ حاضر / ✘ غائب)", rows)

    for key, labels, title in (("rsi_state", RSI_LABELS, "حسب حالة RSI"), ("bb_state", BB_LABELS, "حسب حالة بولينجر")):
        d = {}
        for t in group:
            v = t.get(key)
            if v in labels:
                d.setdefault(labels[v], []).append(t)
        out += section(title, [fmt_group(f"  {lab}", lst) for lab, lst in d.items()])

    # --- خاص بكل نوع ---
    if ttype == "early":
        out += section("حسب مستوى الثقة", bucket_rows(group, lambda t: t.get("confidence"),
                                                       ["مؤكدة قوية", "مؤكدة", "احتمالية"]))
        combo = {}
        for t in group:
            n = sum(1 for k in ("squeeze", "accumulation", "divergence", "momentum") if t.get(k) is True)
            if n:
                combo.setdefault(f"{n} شروط", []).append(t)
        out += section("حسب عدد الشروط المتحققة", [fmt_group(f"  {k}", combo[k]) for k in sorted(combo)])
    elif ttype in ("breakout", "experimental"):
        field = "breakout_details" if ttype == "breakout" else "experimental_details"
        names = BREAKOUT_FACTORS if ttype == "breakout" else EXPERIMENTAL_FACTORS
        rows = []
        for k, label in names.items():
            lst = [t for t in group if (t.get(field) or {}).get(k) is True]
            rows.append(fmt_group(label, lst))
        out += section("حسب عوامل الجودة (الحاضرة فقط)", rows)

    # --- درجة الإشارة ---
    if ttype in ("breakout", "experimental"):
        out += section("حسب جودة الإشارة", bucket_rows(
            group, lambda t: f"جودة {int(t['score'])}/3" if _num(t.get("score")) is not None else None,
            ["جودة 1/3", "جودة 2/3", "جودة 3/3"]))
    else:
        def sb(t):
            s = _num(t.get("score"))
            if s is None:
                return None
            s = abs(s)
            return "3.5+" if s >= 3.5 else "2.5-3.49" if s >= 2.5 else "1.5-2.49" if s >= 1.5 else "<1.5"
        out += section("حسب الـ score", bucket_rows(group, sb, ["<1.5", "1.5-2.49", "2.5-3.49", "3.5+"]))

    # --- مسافة SL و TP1 (تُحسب من entry/sl/tps دائمًا، حتى للصفقات القديمة) ---
    def sl_b(t):
        e, sl = _num(t.get("entry")), _num(t.get("sl"))
        if not e or not sl:
            return None
        p = (e - sl) / e * 100
        return "<1%" if p < 1 else "1-2%" if p < 2 else "2-3%" if p < 3 else "3-5%" if p < 5 else "5%+"
    out += section("حسب مسافة وقف الخسارة (SL%)", bucket_rows(group, sl_b, ["<1%", "1-2%", "2-3%", "3-5%", "5%+"]))

    def tp_b(t):
        e = _num(t.get("entry"))
        tps = t.get("tps") or []
        if not e or not tps:
            return None
        p = (tps[0] - e) / e * 100
        return "<1.2%" if p < 1.2 else "1.2-2%" if p < 2 else "2-3%" if p < 3 else "3%+"
    out += section("حسب مسافة الهدف TP1%", bucket_rows(group, tp_b, ["<1.2%", "1.2-2%", "2-3%", "3%+"]))

    def atr_b(t):
        a = _num(t.get("atr_pct"))
        if a is None:
            return None
        return "<0.3%" if a < 0.3 else "0.3-0.6%" if a < 0.6 else "0.6-1%" if a < 1 else "1%+"
    out += section("حسب التقلب ATR%", bucket_rows(group, atr_b, ["<0.3%", "0.3-0.6%", "0.6-1%", "1%+"]))

    # --- وقت الدخول (بتوقيت الخادم = UTC على GitHub Actions) ---
    def hr_b(t):
        o = t.get("_opened")
        if not o:
            return None
        return ["00-06", "06-12", "12-18", "18-24"][o.hour // 6]
    out += section("حسب ساعة الدخول (UTC)", bucket_rows(group, hr_b, ["00-06", "06-12", "12-18", "18-24"]))

    # --- التزامن مع أنواع أخرى ---
    def conc_b(t):
        c = t.get("concurrent_signals")
        if c is None:
            return None
        return "منفردة" if not c else "مع " + "+".join(TYPE_SHORT.get(x, x) for x in sorted(c))
    d = {}
    for t in group:
        b = conc_b(t)
        if b:
            d.setdefault(b, []).append(t)
    out += section("حسب التزامن مع أنواع أخرى (نفس العملة/نفس الدورة)",
                   [fmt_group(f"  {b}", d[b]) for b in sorted(d)])
    return out


def signals_block(balance, positions):
    lines = [SEP, "📥 الإشارات المرصودة / المفوّتة", SEP]
    counters = (balance or {}).get("signal_counters") or {}
    if counters:
        for ttype, label in TYPES:
            c = counters.get(ttype)
            if not c:
                continue
            seen = c.get("seen", 0)
            taken = c.get("taken", 0)
            lines.append(f"{label}: وصلت {seen} | نُفّذت {taken} | فاتت لامتلاء الشرائح {c.get('missed_max_positions', 0)}"
                         f" | فاتت لنقص الرصيد {c.get('missed_balance', 0)}")
        lines.append("(العدّادات تبدأ من لحظة تطبيق تحديث paper_trading.py فقط)")
    else:
        lines.append("لا عدّادات بعد — تظهر بعد تطبيق تحديث paper_trading.py وتشغيل دورات جديدة.")
    if balance:
        lines.append(f"الرصيد المتاح ${balance.get('available', 0):.2f} من ${balance.get('initial_capital', 0):.0f}"
                     f" | ربح/خسارة محقّق {balance.get('realized_pnl_usd', 0):+.2f}$")
    if positions:
        by = {}
        for p in positions:
            by[p.get("type", "?")] = by.get(p.get("type", "?"), 0) + 1
        lines.append("🔄 مفتوحة الآن: " + " | ".join(f"{TYPE_SHORT.get(k, k)} {v}" for k, v in by.items()))
    return lines


def combo_block(valid):
    """كل الأنواع معًا: أي تزامن يعطي أفضل نتيجة."""
    d = {}
    for t in valid:
        c = t.get("concurrent_signals")
        if c is None:
            continue
        combo = "+".join(TYPE_SHORT.get(x, x) for x in sorted(set(c) | {t["type"]}))
        d.setdefault(combo, []).append(t)
    rows = [fmt_group(f"  {k}", v) for k, v in sorted(d.items(), key=lambda kv: -len(kv[1]))]
    return section("مزيج الأنواع المتزامنة (كل الأنواع)", rows)


def portfolios_compare_block(by_pf, balance, sub_state):
    """مقارنة المحافظ الخمس: كلها برأس مال 400$، فالمقارنة بالدولار وبالعائد على رأس المال عادلة."""
    lines = [SEP, "🏆 مقارنة المحافظ الخمس (حسب صافي الربح بالدولار)", SEP]
    rows = []
    for key, label in PORTFOLIOS:
        group = by_pf[key]
        if key == "main":
            bal, open_n = balance or {}, None
        else:
            entry = (sub_state or {}).get(key[4:]) or {}
            bal, open_n = entry.get("balance") or {}, len(entry.get("positions") or [])
        cap = _num(bal.get("initial_capital")) or DEFAULT_CAPITAL
        rows.append((key, label, analyze(group), cap, bal, open_n))
    rows.sort(key=lambda x: -(x[2]["total_usd"]) if x[2]["n"] else float("inf"))
    for i, (key, label, r, cap, bal, open_n) in enumerate(rows, 1):
        if not r["n"]:
            lines.append(f"{i}. {label}: لا صفقات مغلقة بعد")
            continue
        lines.append(f"{i}. {label} | {r['n']} | نجاح {r['wr']:.0f}% | صافي {r['mean']:+.2f}% | {pf_text(r)} | "
                     f"{r['total_usd']:+.2f}$ ({r['total_usd'] / cap * 100:+.1f}% من {cap:.0f}$) {verdict_icon(r)}")
    lines.append("")
    lines.append("(الرئيسية تتقاسم شرائحها بين كل الأنواع، أما الفرعية فمستقلة: كل نوع له رأس ماله وشرائحه)")
    lines.append(f"(حدّ الحكم {MIN_N} صفقة لكل محفظة؛ ⚪ = عيّنة صغيرة)")
    return lines


def main_portfolio_messages(valid, balance, positions):
    """التقرير الأصلي كما كان، لكن على صفقات المحفظة الرئيسية فقط."""
    if not valid:
        return ["\n".join([SEP, "💼 المحفظة الرئيسية", SEP, "لا توجد صفقات مغلقة بعد."])]
    per_type = {k: [t for t in valid if t["type"] == k] for k in TYPE_KEYS}
    results = {k: analyze(v) for k, v in per_type.items()}

    # --- مقارنة الأنواع داخل الرئيسية ---
    cmp_lines = [SEP, "💼 الرئيسية — ترتيب الأنواع (حسب صافي/صفقة)", SEP]
    ranked = sorted([k for k in TYPE_KEYS if results[k]["n"]], key=lambda k: -results[k]["mean"])
    for i, k in enumerate(ranked, 1):
        r = results[k]
        lr = f" | {r['locked_ret']:+.2f}%/24س" if r["locked_ret"] is not None else ""
        cmp_lines.append(f"{i}. {TYPE_LABEL[k]} {r['n']} | نجاح {r['wr']:.0f}% | صافي {r['mean']:+.2f}% | "
                         f"{pf_text(r)} | {r['total_usd']:+.2f}${lr} {verdict_icon(r)}")
    solid = [k for k in ranked if results[k]["n"] >= MIN_N]
    if solid:
        best = solid[0]
        cmp_lines.append("")
        cmp_lines.append(f"الأفضل (بين الأنواع ذات عيّنة كافية): {TYPE_LABEL[best]} — {verdict_text(results[best])}")
    else:
        cmp_lines.append("")
        cmp_lines.append(f"⚪ لا نوع وصل {MIN_N} صفقة بعد — الترتيب أعلاه اتجاه أولي وليس حكمًا.")
    if len(solid) >= 2:
        a, b = results[solid[0]], results[solid[1]]
        if a["lo"] <= b["hi"]:
            cmp_lines.append("ملاحظة: هوامش الثقة للنوعين الأوّلين متداخلة — الفرق بينهما غير مؤكد إحصائيًا.")
    cmp_lines.append("(%/24س = عائد على رأس المال المحجوز؛ يعاقب النوع الذي يحبس الشرائح طويلًا)")

    all_lines, _ = type_block_all(valid)
    messages = ["\n".join(cmp_lines)]
    for k in TYPE_KEYS:
        if per_type[k]:
            lines, _ = type_block(k, per_type[k])
            lines[1] = f"💼 الرئيسية — {lines[1]}"
            messages.append("\n".join(lines))
    messages.append("\n".join(all_lines + combo_block(valid)))
    messages.append("\n".join(signals_block(balance, positions)))
    return messages


def sub_portfolio_message(ttype, group, sub_entry):
    """تقرير محفظة نوع واحد المستقلة: حالتها + نفس التحليل التفصيلي للنوع."""
    lines, r = type_block(ttype, group)
    lines[1] = f"محفظة {TYPE_LABEL[ttype]} المستقلة — {r['n']} صفقة مغلقة"
    status = []
    if sub_entry:
        bal = sub_entry.get("balance") or {}
        status.append(f"الرصيد المتاح ${bal.get('available', 0):.2f} من ${bal.get('initial_capital', 0):.0f}"
                      f" | ربح/خسارة محقّق {bal.get('realized_pnl_usd', 0):+.2f}$"
                      f" | مفتوحة الآن {len(sub_entry.get('positions') or [])}")
        c = (bal.get("signal_counters") or {}).get(ttype)
        if c:
            status.append(f"الإشارات: وصلت {c.get('seen', 0)} | نُفّذت {c.get('taken', 0)} | فاتت لامتلاء الشرائح "
                          f"{c.get('missed_max_positions', 0)} | فاتت لنقص الرصيد {c.get('missed_balance', 0)}")
    else:
        status.append("⚪ لم تبدأ بعد — لا حالة محفوظة لهذه المحفظة.")
    lines[3:3] = status
    return "\n".join(lines)


def build_messages(trades, notes, balance, positions, since=None, sub_state=None, only=None):
    """only: None = كل المحافظ الخمس، أو 'main' / اسم نوع (official/early/breakout/experimental)."""
    sub_state = sub_state or {}
    valid = prep(trades, since)
    by_pf = {key: [t for t in valid if t["_pf"] == key] for key in PORTFOLIO_KEYS}
    n_sub = sum(len(by_pf[k]) for k in PORTFOLIO_KEYS if k != "main")
    head = ["📊 تقرير المحافظ الوهمية الخمس",
            f"الصفقات المغلقة القابلة للحساب: {len(valid)} (رئيسية {len(by_pf['main'])} + فرعية {n_sub})"
            + (f" (منذ {since:%Y-%m-%d %H:%M})" if since else ""),
            f"حدّ الحكم: {MIN_N} صفقة لكل مجموعة"]
    if not valid:
        return ["\n".join(head + ["لا توجد صفقات مغلقة بعد."])]

    messages = ["\n".join(head)]
    if only is None:
        messages.append("\n".join(portfolios_compare_block(by_pf, balance, sub_state)))
    if only in (None, "main"):
        messages.extend(main_portfolio_messages(by_pf["main"], balance, positions))
    for k in TYPE_KEYS:
        if only in (None, k):
            messages.append(sub_portfolio_message(k, by_pf[f"sub_{k}"], sub_state.get(k)))
    messages.append("\n".join(["ملاحظات:", *[f"- {n}" for n in notes],
                               "- الأرقام مبنية على الصفقات المغلقة فقط، والهامش الإحصائي تقريبي.",
                               "- الصفقات بلا حقل portfolio تُحسب للمحفظة الرئيسية؛ الفرعية تحمل sub_<النوع>.",
                               "- الحقول التشخيصية (العوامل/التزامن/ATR) تظهر فقط للصفقات المفتوحة بعد تطبيق التحديث."]))
    return messages


def type_block_all(valid):
    r = analyze(valid)
    lines = [SEP, f"📊 الكل — {r['n']} صفقة", SEP,
             f"نجاح {r['wr']:.1f}% | صافي/صفقة {r['mean']:+.2f}% (95%: {r['lo']:+.2f}% إلى {r['hi']:+.2f}%) | {pf_text(r)}",
             f"الصافي بالدولار {r['total_usd']:+.2f}$ | أقصى تراجع {r['max_dd']:.2f}$",
             f"الحكم: {verdict_text(r)}"]
    return lines, r


# ---------------------------------------------------------------- تيليجرام
def _split(text, limit=3800):
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


def send_telegram(messages):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ PAPER_TELEGRAM_TOKEN/CHAT_ID غير موجودين — الاكتفاء بالطباعة.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for msg in messages:
        for part in _split(msg):
            try:
                resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": part}, timeout=15)
                if resp.status_code != 200:
                    print(f"⚠️ تيليجرام رفض الرسالة ({resp.status_code}): {resp.text[:200]}")
            except Exception as e:
                print("تعذّر الإرسال عبر تيليجرام:", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=str, default=None,
                    help="فقط الصفقات المغلقة بعد هذا الوقت 'YYYY-MM-DD HH:MM:SS' (مثلاً لحظة Reset)")
    ap.add_argument("--portfolio", choices=["main"] + TYPE_KEYS, default=None,
                    help="محفظة واحدة فقط بدل الخمس: main أو اسم النوع (تقرير المحفظة الفرعية له)")
    args = ap.parse_args()
    since = _parse(args.since) if args.since else None
    if args.since and since is None:
        sys.exit("❌ صيغة --since غير صحيحة، المطلوب: 'YYYY-MM-DD HH:MM:SS'")

    trades, notes, balance, positions, sub_state = load_all()
    print("📥 مصدر البيانات: Gist المحفظة الوهمية (النشط + كل الأرشيف)")
    for n in notes:
        print(f"   - {n}")
    print(f"   إجمالي المحمَّل: {len(trades)} صفقة\n")

    messages = build_messages(trades, notes, balance, positions, since, sub_state, args.portfolio)
    print("\n\n".join(messages))
    send_telegram(messages)


if __name__ == "__main__":
    main()
