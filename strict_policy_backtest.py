#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
strict_policy_backtest.py
=========================
يجيب على: "لو طبّقنا شروط سياسة strict (المستخدمة اليوم في السوق العرضي) على السوق الصاعد،
هل كانت الصفقات التي تمر منها أفضل من التي تُحجب؟" — بأثر رجعي على صفقاتك المسجّلة.

شروط strict مطابقة لـ regime_allows() في scanner.py:
  1) htf_aligned == True (توافق الفريم الأعلى)
  2) تأكيد الحجم: vol_confirm (وللتجريبية: vol_confirm أو volume_confirm من details)
  3) غير ممتدة: extended ليست True
  4) للرسمية فقط: |score| >= REGIME_STRICT_MIN_SCORE_OFFICIAL (افتراضي 2.5)

يحمّل الأرشيف الكامل عبر دالة backtest_exit_rules.load_all_trades، ويستخدم دوال التحليل في
regime_type_diagnostics.py (يجب أن تكون الملفات الثلاثة في نفس المجلد). للقراءة فقط.

ماذا يطبع لكل نوع إشارة (ثم الكل):
  - الصفقات المسجّلة، التي كانت ستمر، والتي كانت ستُحجب (مع n وصافي/صفقة وPF)
  - الفرق (مرّت − حُجبت) مع هامش ثقة 95% وفحص ثبات بين نصف الفترة الأول والثاني
  - أسباب الحجب
  - أثر كل شرط على حدة (لمعرفة أيها يصنع الفرق)
  - فحص سلامة: كم صفقة ranging المسجّلة كانت ستمر من strict (يُتوقع ~100% للصفقات بعد تفعيل السياسة)

حدود مهمة:
  - الصفقات الأقدم التي لا تحمل حقول (htf_aligned / vol_confirm / extended) تُستبعد (unknown) ويظهر عددها.
  - الاختبار يقيس الأثر على الصفقات المسجّلة فقط. الصفقات التي كان strict سيحجبها لا تتأثر بها
    الصفقات الأخرى (لا تفاعل بين الصفقات)، لكن في الواقع تحرير الشرائح قد يغيّر ما يُفتح لاحقًا.
  - سياسة strict الحقيقية تتشدد أكثر عند تقلب السوق extreme، ولا تُحاكى هنا.
  - هذه البيانات نفسها التي اقترحت الفكرة، فالنتيجة فرضية تحتاج تأكيدًا على صفقات جديدة.

المتغيرات: GIST_TOKEN, GIST_ID (+ REGIME_STRICT_MIN_SCORE_OFFICIAL, MIN_SIDE_N, MIN_TRADES_FOR_VERDICT)

الاستخدام:
    python strict_policy_backtest.py
    python strict_policy_backtest.py --regime trending_up --since "2026-09-17 22:00:00"
"""

import os
import sys
import math
import argparse
import statistics

import backtest_exit_rules as bt
import regime_type_diagnostics as d

STRICT_MIN_SCORE_OFFICIAL = float(os.environ.get("REGIME_STRICT_MIN_SCORE_OFFICIAL", "2.5"))
MIN_SIDE = d.MIN_SIDE_N
MIN_N = d.MIN_N
SEP = d.SEP

NEEDED_KEYS = ("htf_aligned", "vol_confirm", "extended")
COND_LABELS = {
    "htf": "توافق فريم أعلى",
    "volume": "تأكيد الحجم",
    "extended": "غير ممتدة (extended ≠ True)",
    "score": f"score ≥ {STRICT_MIN_SCORE_OFFICIAL} (الرسمية فقط)",
}


# ---------------------------------------------------------------- تقييم strict
def conditions(t):
    """يرجع قاموس {شرط: True/False}، أو None لو الصفقة بلا الحقول اللازمة. الشروط الفاشلة = False."""
    if any(k not in t for k in NEEDED_KEYS):
        return None
    ttype = t.get("type", "official")
    vol_ok = bool(t.get("vol_confirm"))
    if ttype == "experimental":
        vol_ok = vol_ok or bool(t.get("volume_confirm")) \
            or bool((t.get("experimental_details") or {}).get("volume_confirm"))
    cond = {
        "htf": t.get("htf_aligned") is True,
        "volume": vol_ok,
        "extended": not bool(t.get("extended")),
    }
    if ttype == "official":
        score = bt._num(t.get("score"))
        cond["score"] = abs(score or 0.0) >= STRICT_MIN_SCORE_OFFICIAL
    return cond


def classify(group):
    """يقسّم المجموعة إلى (pass, block, unknown) ويضيف _failed لكل صفقة محجوبة."""
    passed, blocked, unknown = [], [], []
    for t in group:
        c = conditions(t)
        if c is None:
            unknown.append(t)
            continue
        failed = [k for k, ok in c.items() if not ok]
        t["_failed"] = failed
        (blocked if failed else passed).append(t)
    return passed, blocked, unknown


# ---------------------------------------------------------------- إحصاء المقارنة
def welch(a, b):
    """الفرق (متوسط a − متوسط b) مع هامش ثقة 95%، أو None."""
    if len(a) < 2 or len(b) < 2:
        return None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    se = math.sqrt(statistics.variance(a) / len(a) + statistics.variance(b) / len(b))
    diff = ma - mb
    return diff, diff - 1.96 * se, diff + 1.96 * se


def diff_icon(na, nb, lo, hi):
    if na < MIN_SIDE or nb < MIN_SIDE:
        return "⚪"
    if lo > 0:
        return "🟢"
    if hi < 0:
        return "🔴"
    return "🟡"


def halves_text(passed, blocked, group):
    """فحص الثبات: فرق (مرّت − حُجبت) في نصف الفترة الأول والثاني (حسب وقت الفتح)."""
    ordered = sorted(group, key=lambda t: t["_opened"] or bt.dt.datetime.min)
    cut = len(ordered) // 2
    first_ids = {id(t) for t in ordered[:cut]}
    out, signs = [], []
    for name, pick in (("النصف الأول", lambda t: id(t) in first_ids), ("النصف الثاني", lambda t: id(t) not in first_ids)):
        a = [t["_net"] for t in passed if pick(t)]
        b = [t["_net"] for t in blocked if pick(t)]
        if len(a) >= 5 and len(b) >= 5:
            diff = sum(a) / len(a) - sum(b) / len(b)
            out.append(f"{name}: {diff:+.2f}% (مرّت {len(a)} / حُجبت {len(b)})")
            signs.append(diff > 0)
        else:
            out.append(f"{name}: — (عيّنة صغيرة)")
    stable = "ثبات ✔" if len(signs) == 2 and all(signs) else ("ثبات ✘" if len(signs) == 2 else "ثبات —")
    return " | ".join(out) + f" → {stable}"


def line(label, grp):
    r = d.analyze(grp)
    if r["n"] == 0:
        return f"{label}: n=0"
    return (f"{label}: n={r['n']} | نجاح {r['wr']:.0f}% | صافي {r['mean']:+.2f}% "
            f"[{r['lo']:+.2f}, {r['hi']:+.2f}] | {d.pf_text(r)} {d.icon(r)}")


# ---------------------------------------------------------------- الأقسام
def type_block(title, group, with_conditions=True):
    lines = [SEP, title, SEP]
    passed, blocked, unknown = classify(group)
    lines.append(f"المسجّلة: {len(group)} | بلا الحقول اللازمة (مستبعدة): {len(unknown)}")
    known = passed + blocked
    if not known:
        return lines + ["لا صفقات قابلة للتقييم."], None

    lines.append(line("كل المقيَّمة", known))
    lines.append(line("كانت ستمر (strict)", passed))
    lines.append(line("كانت ستُحجب", blocked))

    res = welch([t["_net"] for t in passed], [t["_net"] for t in blocked])
    verdict = None
    if res:
        diff, lo, hi = res
        ic = diff_icon(len(passed), len(blocked), lo, hi)
        lines.append(f"الفرق (مرّت − حُجبت): {diff:+.2f}% [{lo:+.2f}, {hi:+.2f}] {ic}")
        lines.append(halves_text(passed, blocked, known))
        verdict = (ic, diff)
        if len(passed) < MIN_SIDE or len(blocked) < MIN_SIDE:
            lines.append(f"⚪ أحد الطرفين أقل من {MIN_SIDE} صفقة — لا استنتاج.")
    else:
        lines.append("لا مقارنة: أحد الطرفين أقل من صفقتين.")

    if passed:
        before = sum(t["_net"] for t in known)
        after = sum(t["_net"] for t in passed)
        lines.append(f"لو طُبّق strict: مجموع الصافي {before:+.1f}% ({len(known)} صفقة) → {after:+.1f}% ({len(passed)} صفقة)")

    if blocked:
        why = {}
        for t in blocked:
            for k in t["_failed"]:
                why[k] = why.get(k, 0) + 1
        lines.append("أسباب الحجب (قد تجتمع): " + " | ".join(f"{COND_LABELS[k]} ✘ {v}" for k, v in sorted(why.items(), key=lambda kv: -kv[1])))

    if with_conditions and len(known) >= MIN_N:
        lines.append("")
        lines.append("— أثر كل شرط على حدة (الصفقات المقيَّمة) —")
        for key in ("htf", "volume", "extended", "score"):
            ok = [t for t in known if conditions(t).get(key) is True]
            no = [t for t in known if conditions(t).get(key) is False]
            if not ok and not no:
                continue
            res = welch([t["_net"] for t in ok], [t["_net"] for t in no])
            tail = ""
            if res:
                diff, lo, hi = res
                tail = f" | الفرق {diff:+.2f}% [{lo:+.2f}, {hi:+.2f}] {diff_icon(len(ok), len(no), lo, hi)}"
            lines.append(f"{COND_LABELS[key]}:\n   ✔ n={len(ok)} صافي {d.analyze(ok).get('mean', 0):+.2f}%"
                         f" | ✘ n={len(no)} صافي {d.analyze(no).get('mean', 0):+.2f}%{tail}")
    return lines, verdict


def sanity_block(valid):
    """في ranging تُطبَّق strict فعليًا اليوم: الصفقات المسجّلة بعد تفعيل السياسة يجب أن تمر منها تقريبًا كلها."""
    g = [t for t in valid if t.get("market_regime") == "ranging"]
    passed, blocked, unknown = classify(g)
    known = len(passed) + len(blocked)
    lines = [SEP, "🧪 فحص سلامة (ranging: strict مطبّقة فعليًا اليوم)", SEP]
    if not known:
        return lines + ["لا صفقات ranging قابلة للتقييم."]
    lines.append(f"مرّ {len(passed)} من {known} ({len(passed) / known * 100:.0f}%) | مستبعدة بلا حقول: {len(unknown)}")
    if len(passed) / known < 0.9:
        lines.append("ملاحظة: النسبة أقل من 90% — صفقات قبل تفعيل سياسة strict، أو شروط الاختبار تختلف عن البوت. "
                     "تحقق قبل الاعتماد على النتائج أعلاه.")
    return lines


# ---------------------------------------------------------------- التشغيل
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--regime", choices=bt.REGIMES, default="trending_up")
    ap.add_argument("--since", default=None, help="فقط الصفقات المفتوحة بعد هذا الوقت 'YYYY-MM-DD HH:MM:SS'")
    args = ap.parse_args()
    since = d._parse(args.since) if args.since else None
    if args.since and since is None:
        sys.exit("❌ صيغة --since غير صحيحة، المطلوب: 'YYYY-MM-DD HH:MM:SS'")

    trades, notes = bt.load_all_trades()
    print("📥 مصدر البيانات: Gist (النشط + كل الأرشيف)")
    for n in notes:
        print(f"   - {n}")
    print(f"   إجمالي المحمَّل: {len(trades)} صفقة")

    valid = d.prep(trades, since)
    G = [t for t in valid if t.get("market_regime") == args.regime]
    print(f"   قابلة للحساب: {len(valid)} | في {args.regime}: {len(G)}")
    if not G:
        sys.exit("لا توجد صفقات في هذا النظام.")

    verdicts = {}
    out = []
    for ttype in bt.TYPES:
        sub = [t for t in G if t["type"] == ttype]
        if not sub:
            continue
        lines, v = type_block(f"{bt.TYPE_SHORT[ttype]} × {args.regime}", sub, with_conditions=True)
        out.append("\n".join(lines))
        verdicts[ttype] = v
    lines, v = type_block(f"📊 كل الأنواع × {args.regime}", G, with_conditions=True)
    out.append("\n".join(lines))
    verdicts["ALL"] = v

    summary = [SEP, "🏁 الخلاصة", SEP]
    for k, v in verdicts.items():
        name = "الكل" if k == "ALL" else bt.TYPE_SHORT[k]
        if v is None:
            summary.append(f"{name}: لا مقارنة")
        else:
            ic, diff = v
            note = {"🟢": "strict يحسّن بثقة 95%", "🔴": "strict يضر بثقة 95%",
                    "🟡": "غير حاسم", "⚪": "عيّنة صغيرة"}[ic]
            summary.append(f"{name}: {ic} الفرق {diff:+.2f}% — {note}")
    summary += ["",
                "كيف تقرأها: 🟢 مع ثبات ✔ ومع طرفين كل منهما n ≥ "
                f"{MIN_SIDE} = مرشّحة لتسجيل strict_pass على الصفقات الجديدة ثم التطبيق لاحقًا.",
                "أي نتيجة أخرى = لا تغيّر السياسة بناءً على هذا الاختبار وحده.",
                "تحذير: اختبرنا عدة أنواع وشروط، وبعض الفروق ستظهر بالصدفة."]
    out.append("\n".join(summary))
    out.append("\n".join(sanity_block(valid)))
    print("\n\n".join(out))


if __name__ == "__main__":
    main()
