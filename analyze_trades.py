"""
تحليل أداء تفصيلي للصفقات المغلقة الحقيقية (closed_trades.json على نفس الـ Gist).
سكربت تشغيل يدوي فقط — لا يعدّل أي بيانات، فقط يقرأ سجل الصفقات ويعرض تقريرًا نصيًا مفصلاً.

يعيد حساب متوسط العائد الفعلي (avg_pnl_pct) لكل فئة على حدة (نوع/درجة/عامل تشخيصي)، بدل
الاكتفاء بنسبة "لمس هدف واحد" المستخدمة في compute_stats بملف scanner.py، والتي قد تكون
مضلّلة (صفقة لمست TP1 ثم أُغلقت breakeven تُحسب هناك "ربح" رغم عائد ≈ 0%).

يحتاج نفس متغيرات البيئة المستخدمة بالبوت الحي: GIST_TOKEN و GIST_ID.

تشغيل يدوي فقط:
    python analyze_trades.py
"""

from scanner import load_closed


def net_pct(t):
    """العائد الصافي الفعلي للصفقة (٪) بناءً على سعري الدخول والخروج الحقيقيين."""
    entry, exit_price = t.get("entry"), t.get("exit_price")
    if not entry or not exit_price:
        return None
    return (exit_price - entry) / entry * 100


def group_by(trades, key_func):
    groups = {}
    for t in trades:
        k = key_func(t)
        groups.setdefault(k, []).append(t)
    return groups


def summarize_group(label, trades):
    pcts = [p for p in (net_pct(t) for t in trades) if p is not None]
    if not pcts:
        print(f"  {label}: {len(trades)} صفقة | لا بيانات عائد كافية")
        return
    avg = sum(pcts) / len(pcts)
    positive_rate = sum(1 for p in pcts if p > 0) / len(pcts) * 100
    print(f"  {label}: {len(trades)} صفقة | نسبة عائد موجب فعليًا: {positive_rate:.1f}% | متوسط العائد الصافي: {avg:+.2f}%")


def score_key(t):
    s = t.get("score")
    return str(int(s)) if isinstance(s, (int, float)) else "؟"


def main():
    history = load_closed()
    if not history:
        print("لا توجد صفقات مغلقة مسجّلة بعد على الـ Gist.")
        return

    print(f"إجمالي الصفقات المغلقة: {len(history)}\n")

    overall_pcts = [p for p in (net_pct(t) for t in history) if p is not None]
    if overall_pcts:
        avg = sum(overall_pcts) / len(overall_pcts)
        positive_rate = sum(1 for p in overall_pcts if p > 0) / len(overall_pcts) * 100
        print(f"متوسط العائد العام (صافٍ، كل الصفقات): {avg:+.2f}%")
        print(f"نسبة الصفقات ذات عائد موجب فعليًا: {positive_rate:.1f}%\n")

    print("=" * 55)
    print("حسب النوع (رسمية / مبكرة)")
    print("=" * 55)
    for label, group in sorted(group_by(history, lambda t: t.get("type", "official")).items()):
        summarize_group(label, group)

    early = [t for t in history if t.get("type") == "early"]
    if early:
        print("\n" + "=" * 55)
        print("حسب درجة الثقة (للإشارات المبكرة فقط)")
        print("=" * 55)
        for label, group in sorted(group_by(early, lambda t: t.get("confidence", "غير محدد")).items()):
            summarize_group(label, group)

    print("\n" + "=" * 55)
    print("حسب الدرجة (score، مقرّبة لأقرب عدد صحيح)")
    print("=" * 55)
    for label, group in sorted(group_by(history, score_key).items(), key=lambda x: x[0]):
        summarize_group(label, group)

    print("\n" + "=" * 55)
    print("حسب كل عامل تشخيصي ثنائي لحاله (حاضر مقابل غائب)")
    print("=" * 55)
    boolean_factors = [
        "macd_bull", "vol_confirm", "ranging", "near_resistance",
        "obv_confirm", "htf_aligned", "squeeze", "accumulation",
        "divergence", "extended",
    ]
    for factor in boolean_factors:
        print(f"\n-- {factor} --")
        present = [t for t in history if t.get(factor) is True]
        absent = [t for t in history if t.get(factor) is False]
        summarize_group("حاضر", present)
        summarize_group("غائب", absent)

    print("\n" + "=" * 55)
    print("حسب المؤشرات ذات القيم المتعددة (rsi_state / bb_state: -1 هابط, 0 محايد, 1 صاعد)")
    print("=" * 55)
    for factor in ["rsi_state", "bb_state"]:
        print(f"\n-- {factor} --")
        for label, group in sorted(group_by(history, lambda t: t.get(factor)).items(), key=lambda x: str(x[0])):
            summarize_group(str(label), group)

    if early:
        print("\n" + "=" * 55)
        print("تقاطعات (الإشارات المبكرة فقط): Squeeze وتراكم صامت معًا مقابل كل عامل لحاله")
        print("=" * 55)
        both = [t for t in early if t.get("squeeze") and t.get("accumulation")]
        squeeze_only = [t for t in early if t.get("squeeze") and not t.get("accumulation")]
        accum_only = [t for t in early if t.get("accumulation") and not t.get("squeeze")]
        summarize_group("الاثنين معًا", both)
        summarize_group("Squeeze فقط", squeeze_only)
        summarize_group("تراكم فقط", accum_only)

    print("\n" + "=" * 55)
    print("حسب سبب الإغلاق")
    print("=" * 55)
    for label, group in sorted(group_by(history, lambda t: t.get("closed_reason", "؟")).items()):
        summarize_group(label, group)


if __name__ == "__main__":
    main()
