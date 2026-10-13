#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
regime_type_diagnostics.py
==========================
يجيب على: "لماذا يخسر نوع إشارة معيّن (افتراضيًا: المبكرة) في نظام سوق معيّن (افتراضيًا: trending_up)؟"

يحمّل الأرشيف الكامل (closed_trades.json + كل ملفات الأرشيف عبر archive_gists_chain.json و
archive_index.json) باستخدام نفس دالة التحميل في backtest_exit_rules.py (يجب أن يكون الملفان
في نفس المجلد). للقراءة فقط: لا يكتب شيئًا في الـ Gist ولا يجلب شموعًا.

ماذا يطبع:
  1) المقارنة: المجموعة المستهدفة (النوع × النظام) مقابل نفس النوع في باقي الأنظمة.
  2) بنية الربح/الخسارة: متوسط الربح، متوسط الخسارة، نسبة النجاح المطلوبة للتعادل، وطرق الإغلاق.
  3) التقسيمات: العوامل ✔/✘، مستوى الثقة، عدد الشروط، RSI/Bollinger، score، مسافة SL وTP1،
     نسبة الهدف للمخاطرة (R:R)، ساعة الدخول، التزامن مع أنواع أخرى، حالة النظام (regime_*).
  4) ترتيب أقوى الفروق بين ✔ و✘ مع هامش ثقة 95%.

تنبيهات مهمة:
  - الحقول التشخيصية (العوامل/التزامن/regime_*) تظهر فقط للصفقات المسجّلة بعد إضافتها؛
    الصفقات الأقدم تُستبعد من ذلك التقسيم فقط (يظهر عددها).
  - كل تقسيم يعرض n. أقل من MIN_TRADES_FOR_VERDICT (30) = ⚪ عيّنة صغيرة، لا تبنِ عليه قرارًا.
  - عند فحص عشرات التقسيمات ستظهر بعض الفروق "المهمة" بالصدفة. اعتبر النتيجة فرضية للاختبار
    على صفقات جديدة، وليست حقيقة.

المتغيرات: GIST_TOKEN, GIST_ID (+ اختياري: TRADING_FEE_PCT, BREAKEVEN_BAND_PCT,
           MIN_TRADES_FOR_VERDICT, ALLOW_PARTIAL, MIN_SIDE_N لحدّ كل طرف في ترتيب الفروق، افتراضي 20)

الاستخدام:
    python regime_type_diagnostics.py
    python regime_type_diagnostics.py --type early --regime trending_up
    python regime_type_diagnostics.py --type breakout --regime trending_up
    python regime_type_diagnostics.py --since "2026-09-17 22:00:00"
"""

import os
import sys
import math
import argparse
import statistics
import datetime as dt

import backtest_exit_rules as bt

FEE_PCT = bt.FEE_PCT
BE_BAND = bt.BE_BAND
MIN_N = bt.MIN_N
MIN_SIDE_N = int(os.environ.get("MIN_SIDE_N", "20"))
SEP = "═" * 31

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
    ("ranging", "سوق عرضي للعملة (ADX منخفض)"),
    ("near_resistance", "قرب مقاومة"),
]
RSI_LABELS = {1: "تشبع بيعي (RSI<35)", 0: "محايد", -1: "تشبع شرائي (RSI>65)"}
BB_LABELS = {1: "عند الحد السفلي", 0: "منتصف النطاق", -1: "عند الحد العلوي"}
TYPE_SHORT = bt.TYPE_SHORT


# ---------------------------------------------------------------- تجهيز الصفقات
def _parse(s):
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def prep(trades, since=None):
    """يضيف لكل صفقة: _net (صافي% بعد الرسوم)، _h (مدة بالساعات)، _opened، _rr (الهدف/المخاطرة)، _sl_pct، _tp_pct."""
    out = []
    for t in trades:
        entry, exit_p = bt._num(t.get("entry")), bt._num(t.get("exit_price"))
        if not entry or not exit_p or entry <= 0 or exit_p <= 0:
            continue
        opened, closed = _parse(t.get("opened_at")), _parse(t.get("closed_at"))
        if since and (opened is None or opened < since):
            continue
        risk = bt._num(t.get("initial_risk"))
        if not risk or risk <= 0:
            sl = bt._num(t.get("sl"))
            risk = (entry - sl) if sl else None
        tps = t.get("tps") or []
        tp1 = bt._num(tps[0]) if tps else None
        rec = {**t,
               "_net": (exit_p / entry - 1) * 100 - FEE_PCT,
               "_h": (closed - opened).total_seconds() / 3600 if opened and closed else None,
               "_opened": opened,
               "_sl_pct": (risk / entry * 100) if risk and risk > 0 else None,
               "_tp_pct": ((tp1 - entry) / entry * 100) if tp1 and tp1 > entry else None,
               "_rr": ((tp1 - entry) / risk) if (tp1 and tp1 > entry and risk and risk > 0) else None}
        rec["type"] = t.get("type", "official")
        out.append(rec)
    return out


# ---------------------------------------------------------------- الإحصاء
def analyze(group):
    n = len(group)
    res = {"n": n}
    if n == 0:
        return res
    nets = [t["_net"] for t in group]
    wins = [x for x in nets if x > BE_BAND]
    losses = [x for x in nets if x < -BE_BAND]
    mean = sum(nets) / n
    sd = statistics.stdev(nets) if n >= 2 else 0.0
    se = sd / math.sqrt(n) if n >= 2 else 0.0
    gl = -sum(losses)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    res.update({
        "wins": len(wins), "losses": len(losses), "flat": n - len(wins) - len(losses),
        "wr": len(wins) / n * 100, "mean": mean, "lo": mean - 1.96 * se, "hi": mean + 1.96 * se,
        "pf": (sum(wins) / gl) if gl > 0 else None,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "be_wr": (abs(avg_loss) / (avg_win + abs(avg_loss)) * 100) if (wins and losses) else None,
    })
    return res


def icon(r):
    if r["n"] < MIN_N:
        return "⚪"
    if r["lo"] > 0:
        return "🟢"
    if r["hi"] < 0:
        return "🔴"
    return "🟡"


def pf_text(r):
    return f"PF {r['pf']:.2f}" if r.get("pf") is not None else "PF ∞"


def fmt_group(label, subset):
    r = analyze(subset)
    if r["n"] == 0:
        return None
    if r["n"] < MIN_N:
        return f"{label}: {r['n']} ⚪ عيّنة صغيرة (رابحة {r['wins']} / خاسرة {r['losses']} | صافي {r['mean']:+.2f}%)"
    return f"{label}: {r['n']} | نجاح {r['wr']:.0f}% | صافي {r['mean']:+.2f}% | {pf_text(r)} {icon(r)}"


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
def header_block(ttype, regime, G, C_other, all_regime):
    lines = [SEP, f"🔬 تشخيص: {TYPE_SHORT.get(ttype, ttype)} × {regime}", SEP]
    for label, grp in ((f"المستهدفة ({TYPE_SHORT.get(ttype, ttype)} في {regime})", G),
                       (f"نفس النوع في باقي الأنظمة", C_other),
                       (f"كل الأنواع في {regime}", all_regime)):
        r = analyze(grp)
        if r["n"] == 0:
            lines.append(f"{label}: لا صفقات")
        else:
            lines.append(f"{label}: n={r['n']} | نجاح {r['wr']:.0f}% | صافي {r['mean']:+.2f}% "
                         f"[{r['lo']:+.2f}, {r['hi']:+.2f}] | {pf_text(r)} {icon(r)}")
    return lines


def payoff_block(G):
    r = analyze(G)
    lines = ["", "— بنية الربح/الخسارة —"]
    if r["n"] == 0:
        return lines + ["لا صفقات."]
    lines.append(f"رابحة {r['wins']} (متوسط {r['avg_win']:+.2f}%) | خاسرة {r['losses']} (متوسط {r['avg_loss']:+.2f}%)"
                 + (f" | تعادل {r['flat']}" if r["flat"] else ""))
    if r["be_wr"] is not None:
        gap = r["wr"] - r["be_wr"]
        lines.append(f"نجاح فعلي {r['wr']:.1f}% | مطلوب للتعادل {r['be_wr']:.1f}% | الفجوة {gap:+.1f} نقطة"
                     + (" ← أقل من المطلوب" if gap < 0 else ""))
    reasons = {}
    for t in G:
        k = t.get("closed_reason", "?")
        reasons.setdefault(k, []).append(t)
    for k, lst in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
        m = sum(x["_net"] for x in lst) / len(lst)
        lines.append(f"  إغلاق {k}: {len(lst)} ({len(lst) / len(G) * 100:.0f}%) | متوسط {m:+.2f}%")
    hs = [t["_h"] for t in G if t["_h"] is not None]
    if hs:
        lines.append(f"متوسط مدة الصفقة {sum(hs) / len(hs):.1f}س")
    return lines


def split_blocks(ttype, G):
    out = []
    rows, missing = [], {}
    for key, label in FLAG_KEYS:
        yes = [t for t in G if t.get(key) is True]
        no = [t for t in G if t.get(key) is False]
        missing[key] = len(G) - len(yes) - len(no)
        if yes or no:
            rows.append(fmt_group(f"{label} ✔", yes))
            rows.append(fmt_group(f"{label} ✘", no))
    out += section("حسب العوامل (✔ حاضر / ✘ غائب) — الصفقات الأقدم بلا هذه الحقول تُستبعد", rows)

    for key, labels, title in (("rsi_state", RSI_LABELS, "حسب حالة RSI"), ("bb_state", BB_LABELS, "حسب حالة بولينجر")):
        d = {}
        for t in G:
            v = t.get(key)
            if v in labels:
                d.setdefault(labels[v], []).append(t)
        out += section(title, [fmt_group(f"  {lab}", lst) for lab, lst in d.items()])

    if ttype == "early":
        out += section("حسب مستوى الثقة", bucket_rows(G, lambda t: t.get("confidence"),
                                                       ["مؤكدة قوية", "مؤكدة", "احتمالية"]))
        combo = {}
        for t in G:
            flags = [t.get(k) for k in ("squeeze", "accumulation", "divergence", "momentum")]
            if all(f is None for f in flags):
                continue
            n = sum(1 for f in flags if f is True)
            combo.setdefault(f"{n} شروط", []).append(t)
        out += section("حسب عدد الشروط المتحققة", [fmt_group(f"  {k}", combo[k]) for k in sorted(combo)])
        src = {}
        for t in G:
            names = [k for k in ("squeeze", "accumulation", "divergence", "momentum") if t.get(k) is True]
            if names:
                src.setdefault("+".join(names), []).append(t)
        out += section("حسب مصدر الإشارة (تركيبة الشروط)",
                       [fmt_group(f"  {k}", v) for k, v in sorted(src.items(), key=lambda kv: -len(kv[1]))])

    def sb(t):
        s = bt._num(t.get("score"))
        if s is None:
            return None
        s = abs(s)
        return "3.5+" if s >= 3.5 else "2.5-3.49" if s >= 2.5 else "1.5-2.49" if s >= 1.5 else "<1.5"
    out += section("حسب الـ score", bucket_rows(G, sb, ["<1.5", "1.5-2.49", "2.5-3.49", "3.5+"]))

    def slb(t):
        p = t["_sl_pct"]
        if p is None:
            return None
        return "<1%" if p < 1 else "1-2%" if p < 2 else "2-3%" if p < 3 else "3-5%" if p < 5 else "5%+"
    out += section("حسب مسافة وقف الخسارة (SL%)", bucket_rows(G, slb, ["<1%", "1-2%", "2-3%", "3-5%", "5%+"]))

    def tpb(t):
        p = t["_tp_pct"]
        if p is None:
            return None
        return "<1.2%" if p < 1.2 else "1.2-2%" if p < 2 else "2-3%" if p < 3 else "3%+"
    out += section("حسب مسافة الهدف TP1%", bucket_rows(G, tpb, ["<1.2%", "1.2-2%", "2-3%", "3%+"]))

    def rrb(t):
        x = t["_rr"]
        if x is None:
            return None
        return "<0.6" if x < 0.6 else "0.6-0.9" if x < 0.9 else "0.9-1.1" if x < 1.1 else "1.1+"
    out += section("حسب نسبة الهدف للمخاطرة (TP1 ÷ SL) — يُقلَّم الهدف عند أقرب مقاومة",
                   bucket_rows(G, rrb, ["<0.6", "0.6-0.9", "0.9-1.1", "1.1+"]))

    def hr(t):
        o = t["_opened"]
        return None if not o else ["00-06", "06-12", "12-18", "18-24"][o.hour // 6]
    out += section("حسب ساعة الدخول (UTC — بافتراض أن opened_at بتوقيت UTC)",
                   bucket_rows(G, hr, ["00-06", "06-12", "12-18", "18-24"]))

    d = {}
    for t in G:
        c = t.get("concurrent_signals")
        if c is None:
            continue
        b = "منفردة" if not c else "مع " + "+".join(TYPE_SHORT.get(x, x) for x in sorted(c))
        d.setdefault(b, []).append(t)
    out += section("حسب التزامن مع أنواع أخرى", [fmt_group(f"  {b}", d[b]) for b in sorted(d)])

    for key, title in (("regime_phase", "حسب مرحلة السوق (regime_phase)"), ("regime_vol", "حسب تقلب السوق (regime_vol)")):
        d = {}
        for t in G:
            v = t.get(key)
            if v:
                d.setdefault(str(v), []).append(t)
        out += section(title, [fmt_group(f"  {k}", v) for k, v in sorted(d.items(), key=lambda kv: -len(kv[1]))])

    def rsb(t):
        s = bt._num(t.get("regime_score"))
        if s is None:
            return None
        return "<0.35" if s < 0.35 else "0.35-0.6" if s < 0.6 else "0.6+"
    out += section("حسب درجة نظام السوق (regime_score) داخل trending_up",
                   bucket_rows(G, rsb, ["<0.35", "0.35-0.6", "0.6+"]))
    return out


def ranking_block(G):
    """أقوى الفروق بين ✔ و✘ لكل عامل (Welch)، بشرط n ≥ MIN_SIDE_N لكل طرف."""
    rows = []
    for key, label in FLAG_KEYS:
        yes = [t["_net"] for t in G if t.get(key) is True]
        no = [t["_net"] for t in G if t.get(key) is False]
        if len(yes) < MIN_SIDE_N or len(no) < MIN_SIDE_N:
            continue
        diff = sum(yes) / len(yes) - sum(no) / len(no)
        se = math.sqrt(statistics.variance(yes) / len(yes) + statistics.variance(no) / len(no))
        lo, hi = diff - 1.96 * se, diff + 1.96 * se
        ic = "🟢" if lo > 0 else ("🔴" if hi < 0 else "🟡")
        rows.append((abs(diff), f"{label}: حاضر n={len(yes)} مقابل غائب n={len(no)} | الفرق {diff:+.2f}% "
                                f"[{lo:+.2f}, {hi:+.2f}] {ic}"))
    lines = ["", f"— ترتيب أقوى الفروق (✔ مقابل ✘، كل طرف n ≥ {MIN_SIDE_N}) —"]
    if not rows:
        return lines + ["لا عامل يحقق شرط العيّنة في الطرفين."]
    rows.sort(key=lambda x: -x[0])
    lines += [r for _, r in rows[:8]]
    lines.append("(🟢 = وجود العامل أفضل بثقة 95% | 🔴 = أسوأ | 🟡 = غير حاسم. الفرق الموجب = الحاضر أفضل)")
    return lines


# ---------------------------------------------------------------- التشغيل
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", choices=bt.TYPES, default="early")
    ap.add_argument("--regime", choices=bt.REGIMES, default="trending_up")
    ap.add_argument("--since", default=None, help="فقط الصفقات المفتوحة بعد هذا الوقت 'YYYY-MM-DD HH:MM:SS'")
    args = ap.parse_args()
    since = _parse(args.since) if args.since else None
    if args.since and since is None:
        sys.exit("❌ صيغة --since غير صحيحة، المطلوب: 'YYYY-MM-DD HH:MM:SS'")

    trades, notes = bt.load_all_trades()
    print("📥 مصدر البيانات: Gist (النشط + كل الأرشيف)")
    for n in notes:
        print(f"   - {n}")
    print(f"   إجمالي المحمَّل: {len(trades)} صفقة")

    valid = prep(trades, since)
    G = [t for t in valid if t["type"] == args.type and t.get("market_regime") == args.regime]
    C_other = [t for t in valid if t["type"] == args.type and t.get("market_regime") in bt.REGIMES
               and t.get("market_regime") != args.regime]
    all_regime = [t for t in valid if t.get("market_regime") == args.regime]
    no_regime = sum(1 for t in valid if t["type"] == args.type and t.get("market_regime") not in bt.REGIMES)
    print(f"   قابلة للحساب: {len(valid)} | المجموعة المستهدفة: {len(G)} | "
          f"من نفس النوع بلا market_regime (مستبعدة): {no_regime}")
    if not G:
        sys.exit("لا توجد صفقات في المجموعة المستهدفة.")

    lines = header_block(args.type, args.regime, G, C_other, all_regime)
    lines += payoff_block(G)
    lines += split_blocks(args.type, G)
    lines += ranking_block(G)
    lines += ["", "تحذيرات:",
              "- عدد التقسيمات كبير، فبعض الفروق 🟢/🔴 ستظهر بالصدفة. اعتبرها فرضيات تُختبر على صفقات جديدة.",
              "- الصفقات المغلقة فقط. الحقول التشخيصية متاحة للصفقات الأحدث فقط (راجع n في كل سطر)."]
    print("\n".join(lines))


if __name__ == "__main__":
    main()
