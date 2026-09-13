# -*- coding: utf-8 -*-
"""
pattern_analysis.py — تحليل القواسم المشتركة للعملات الناجحة تاريخيًا
=======================================================================
سكربت مستقل (لا يُعدّل أي شيء في scanner.py) يقرأ سجل الصفقات المغلقة
(closed_trades.json + كل ملفات الأرشيف عبر سلسلة archive_gists_chain.json،
بنفس آلية القراءة المستخدمة في trade_stats.py) ويُصفّي فقط صفقات مجموعة
عملات محددة سلفًا (اعتُبرت "ناجحة" في مراجعة سابقة)، ثم يبحث عن قواسم
مشتركة في نوعية الإشارة التي فتحت كل صفقة:

  - هل أغلب صفقاتها Squeeze فقط / Accumulation فقط / كلاهما / بدون أي منهما؟
  - نسبة الصفقات التي فُتحت بدون Divergence وبدون Accumulation
  - توزيع rsi_state وقت الدخول (تقريبي — انظر ملاحظة أدناه)
  - توزيع أنواع الإشارة الأربعة (رسمية/مبكرة/انفجار/تجريبية) ومستوى الثقة
    للإشارات المبكرة (احتمالية/مؤكدة/مؤكدة قوية)
  - نسبة نجاح تقريبية لكل تقسيمة (فتح TP1 على الأقل مقابل SL مباشر)

⚠️ ملاحظة مهمة على دقة تحليل RSI: سجل الصفقة يحفظ rsi_state (تصنيف مبسّط:
1 = تشبع بيعي <35، -1 = تشبع شرائي >65، 0 = محايد بين 35 و65) وليس قيمة RSI
الخام وقت الدخول. لذلك لا يمكن التحقق بدقة من نطاق "40-60" تحديدًا من السجل
التاريخي؛ التقرير يعرض توزيع rsi_state كأفضل تقريب متاح مع توضيح الحد.

الاستخدام:
    GIST_TOKEN=... GIST_ID=... python3 pattern_analysis.py
اختياريًا لإرسال التقرير على تيليجرام أيضًا:
    TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=... python3 pattern_analysis.py --telegram
"""

import os
import sys
import json
import argparse
import requests

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

CLOSED_GIST_FILE = "closed_trades.json"
ARCHIVE_PREFIX = "closed_trades_archive_"
ARCHIVE_CHAIN_FILE = "archive_gists_chain.json"

# العملات "الناجحة" المطلوب تحليلها (بدون لاحقة USDT)
TARGET_BASES = {"LSK", "VTHO", "NOM", "MIRA", "FIL", "TREE", "PUNDIX", "CFG", "ETC", "ZK", "CVC"}


def _headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def _get_all_files(gist_id):
    if not GIST_TOKEN or not gist_id:
        return {}
    r = requests.get(f"https://api.github.com/gists/{gist_id}", headers=_headers(), timeout=20)
    r.raise_for_status()
    return r.json().get("files", {})


def load_all_closed_trades():
    """يجمع كل الصفقات المغلقة: السجل النشط (closed_trades.json) + كل ملفات
    الأرشيف عبر كل الـGists المذكورة في سلسلة archive_gists_chain.json."""
    if not GIST_TOKEN or not GIST_ID:
        print("⚠️ GIST_TOKEN أو GIST_ID غير موجودين في البيئة.")
        return []

    main_files = _get_all_files(GIST_ID)
    trades = []

    active_raw = main_files.get(CLOSED_GIST_FILE, {}).get("content")
    if active_raw:
        try:
            trades.extend(json.loads(active_raw))
        except Exception as e:
            print(f"تعذّر قراءة {CLOSED_GIST_FILE}: {e}")

    chain_raw = main_files.get(ARCHIVE_CHAIN_FILE, {}).get("content")
    try:
        chain = json.loads(chain_raw) if chain_raw else []
    except Exception:
        chain = []

    for gist_id in chain:
        files = main_files if gist_id == GIST_ID else _get_all_files(gist_id)
        for fname, fdata in files.items():
            if not fname.startswith(ARCHIVE_PREFIX):
                continue
            try:
                trades.extend(json.loads(fdata["content"]))
            except Exception as e:
                print(f"تعذّر قراءة ملف الأرشيف {fname} من Gist {gist_id}: {e}")

    return trades


def base_symbol(symbol):
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def is_win(trade):
    """فوز تقريبي: تحقق هدف واحد على الأقل قبل الإغلاق (ALL_TP أو SL بعد TP1+)،
    أو إغلاق EXPIRED بربح فعلي. خسارة: SL بدون أي TP محقق."""
    hit = trade.get("hit_tps") or []
    reason = trade.get("closed_reason")
    if reason == "ALL_TP":
        return True
    if reason == "SL":
        return len(hit) > 0
    if reason == "EXPIRED":
        entry, exitp = trade.get("entry"), trade.get("exit_price")
        if entry and exitp is not None:
            return exitp > entry
    return len(hit) > 0


def pct(n, d):
    return (n / d * 100) if d else 0.0


def analyze(trades):
    matched = [t for t in trades if base_symbol(t.get("symbol", "")) in TARGET_BASES]
    lines = []
    lines.append(f"📊 تحليل قواسم العملات الناجحة ({len(TARGET_BASES)} عملة مستهدفة)")
    lines.append(f"عدد الصفقات المطابقة من السجل الكامل: {len(matched)} / إجمالي الصفقات المقروءة: {len(trades)}")

    if not matched:
        lines.append("لا توجد صفقات مطابقة لهذه العملات في السجل الحالي.")
        return "\n".join(lines), matched

    # 1) توزيع نوع الإشارة
    lines.append("\n— حسب نوع الإشارة —")
    by_type = {}
    for t in matched:
        by_type.setdefault(t.get("type", "?"), []).append(t)
    type_labels = {"official": "رسمية", "early": "مبكرة", "breakout": "انفجار", "experimental": "تجريبية"}
    for typ, ts in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        wins = sum(1 for t in ts if is_win(t))
        lines.append(
            f"{type_labels.get(typ, typ)}: {len(ts)} صفقة ({pct(len(ts), len(matched)):.0f}% من الإجمالي) "
            f"— نجاح {pct(wins, len(ts)):.0f}%"
        )

    # 1.ب) توزيع مستوى الثقة داخل الإشارات المبكرة فقط
    early = by_type.get("early", [])
    if early:
        lines.append("\n— مستوى الثقة داخل الإشارات المبكرة —")
        by_conf = {}
        for t in early:
            by_conf.setdefault(t.get("confidence", "?"), []).append(t)
        for conf, ts in sorted(by_conf.items(), key=lambda kv: -len(kv[1])):
            wins = sum(1 for t in ts if is_win(t))
            lines.append(f"{conf}: {len(ts)} صفقة — نجاح {pct(wins, len(ts)):.0f}%")

    # 2) مزيج Squeeze/Accumulation/Divergence (فقط للأنواع اللي تحفظ هذي الحقول: رسمية ومبكرة)
    diag_capable = [t for t in matched if t.get("type") in ("official", "early")]
    if diag_capable:
        lines.append("\n— مزيج المؤشرات الإضافية (رسمية + مبكرة، n=%d) —" % len(diag_capable))

        def flag(t, key):
            return bool(t.get(key))

        squeeze_only = [t for t in diag_capable if flag(t, "squeeze") and not flag(t, "accumulation")]
        accum_only = [t for t in diag_capable if flag(t, "accumulation") and not flag(t, "squeeze")]
        both = [t for t in diag_capable if flag(t, "squeeze") and flag(t, "accumulation")]
        neither = [t for t in diag_capable if not flag(t, "squeeze") and not flag(t, "accumulation")]
        no_divergence = [t for t in diag_capable if not flag(t, "divergence")]
        no_accum = [t for t in diag_capable if not flag(t, "accumulation")]
        with_macd_bull = [t for t in diag_capable if flag(t, "macd_bull")]

        for label, group in [
            ("Squeeze فقط (بدون Accumulation)", squeeze_only),
            ("Accumulation فقط (بدون Squeeze)", accum_only),
            ("Squeeze + Accumulation معًا", both),
            ("بدون Squeeze ولا Accumulation", neither),
        ]:
            wins = sum(1 for t in group if is_win(t))
            lines.append(f"{label}: {len(group)} ({pct(len(group), len(diag_capable)):.0f}%) — نجاح {pct(wins, len(group)):.0f}%")

        lines.append(f"بدون Divergence إطلاقًا: {len(no_divergence)} ({pct(len(no_divergence), len(diag_capable)):.0f}%)")
        lines.append(f"بدون Accumulation إطلاقًا: {len(no_accum)} ({pct(len(no_accum), len(diag_capable)):.0f}%)")
        lines.append(f"مع MACD صاعد (macd_bull) وقت الدخول: {len(with_macd_bull)} ({pct(len(with_macd_bull), len(diag_capable)):.0f}%)")

        # 3) توزيع rsi_state (تقريبي لمنطقة RSI — انظر التحذير أعلى الملف)
        lines.append("\n— توزيع rsi_state وقت الدخول (تقريب لمنطقة RSI، ليس القيمة الخام) —")
        rsi_labels = {1: "تشبع بيعي (RSI<35)", -1: "تشبع شرائي (RSI>65)", 0: "محايد (35≤RSI≤65، يشمل 40-60)"}
        by_rsi = {}
        for t in diag_capable:
            by_rsi.setdefault(t.get("rsi_state"), []).append(t)
        for state, ts in sorted(by_rsi.items(), key=lambda kv: -len(kv[1])):
            wins = sum(1 for t in ts if is_win(t))
            lines.append(f"{rsi_labels.get(state, str(state))}: {len(ts)} ({pct(len(ts), len(diag_capable)):.0f}%) — نجاح {pct(wins, len(ts)):.0f}%")
    else:
        lines.append("\n(لا توجد صفقات رسمية/مبكرة ضمن هذه العملات — تعذّر تحليل مزيج المؤشرات)")

    # 4) خلاصة سريعة تلقائية
    lines.append("\n— خلاصة —")
    total_wins = sum(1 for t in matched if is_win(t))
    lines.append(f"نسبة النجاح الإجمالية لهذه العملات: {pct(total_wins, len(matched)):.0f}% ({total_wins}/{len(matched)})")
    if diag_capable:
        no_div_pct = pct(len(no_divergence), len(diag_capable))
        no_accum_pct = pct(len(no_accum), len(diag_capable))
        if no_div_pct >= 70:
            lines.append(f"→ الغالبية العظمى ({no_div_pct:.0f}%) بدون Divergence أصلاً — يدعم استبعاده كليًا من الإشارة الرسمية.")
        if no_accum_pct >= 70:
            lines.append(f"→ الغالبية العظمى ({no_accum_pct:.0f}%) بدون Accumulation أصلاً.")

    return "\n".join(lines), matched


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ TELEGRAM_TOKEN/TELEGRAM_CHAT_ID غير موجودين — تخطي الإرسال.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    # تيليجرام يحدد 4096 حرف كحد أقصى للرسالة الواحدة
    for i in range(0, len(text), 3900):
        chunk = text[i:i + 3900]
        try:
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk}, timeout=15)
        except Exception as e:
            print(f"فشل إرسال تيليجرام: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram", action="store_true", help="أرسل التقرير على تيليجرام أيضًا")
    args = parser.parse_args()

    trades = load_all_closed_trades()
    report, _ = analyze(trades)
    print(report)

    if args.telegram:
        send_telegram(report)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"خطأ: {e}", file=sys.stderr)
        sys.exit(1)
