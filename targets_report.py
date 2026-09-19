#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
targets_report.py
==================
سكربت تحليل يقرأ closed_trades.json من نفس الـ Gist المستخدم في scanner.py
(نفس المفاتيح الحرفية: type / symbol / tps / hit_tps / closed_reason)
ويعرض 4 تقارير:

  1) نتائج الإشارات حسب النوع (رسمية / مبكرة): عدد الصفقات، نسبة لمس
     أي هدف (win)، ونسبة الوصول لكامل الأهداف الموضوعة (ALL_TP).
  2) قائمة كل الصفقات (رسمية أو مبكرة) التي كان لها هدفان (TP) أو أكثر
     عند الفتح، مع نتيجة كل واحدة.
  3) نسبة نجاح كل عملة (symbol) على حدة في الوصول لكامل أهدافها، من بين
     صفقاتها التي كان لها هدفان فأكثر.
  4) الانفجار (breakout): مجموع ربح الرابحة ومجموع خسارة الخاسرة والصافي
     (من الحقل net_pnl_pct)، بنفس شكل تقارير الرسمية والمبكرة.

الأقسام 1 إلى 3 للرسمية والمبكرة فقط؛ القسم 4 مخصص للانفجار.

الإعداد
-------
نفس متغيرات البيئة المستخدمة في scanner.py:
    GIST_TOKEN, GIST_ID
اختياري: BREAKEVEN_BAND_PCT (افتراضي 0.1) نطاق التعادل الذي لا يُحسب ربحًا ولا خسارة
(أو ضع ملف closed_trades.json محليًا بجانب السكربت لتشغيله بدون شبكة)

تشغيل:
    python targets_report.py
"""

import os
import sys
import json
from collections import defaultdict

import requests

GIST_FILENAME = "closed_trades.json"          # السجل النشط: أحدث الصفقات فقط
ARCHIVE_PREFIX = "closed_trades_archive_"     # ملفات الأرشيف المرقّمة (تحوي كل التاريخ الأقدم)

BREAKEVEN_BAND_PCT = float(os.environ.get("BREAKEVEN_BAND_PCT", "0.1"))
PNL_KEYS = ("net_pnl_pct", "pnl_pct", "profit_pct")   # الأساسي net_pnl_pct، والبقية احتياطية


def _read_json_file(file_entry, filename):
    """يقرأ محتوى ملف من استجابة Gist، مع التحقق من احتمال البتر (truncated) لملف كبير جدًا
    والجلب من raw_url في هذه الحالة بدل الاعتماد على content فقط."""
    if file_entry.get("truncated"):
        raw_url = file_entry.get("raw_url")
        try:
            rr = requests.get(raw_url, timeout=15)
            rr.raise_for_status()
            content = rr.text
        except Exception as e:
            print(f"⚠️ تعذّر جلب المحتوى الكامل غير المبتور لـ {filename}: {e}")
            content = file_entry.get("content", "[]")
    else:
        content = file_entry.get("content", "[]")
    return json.loads(content)


def load_trades():
    local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "closed_trades.json")
    if os.path.exists(local_path):
        with open(local_path, "r", encoding="utf-8") as f:
            return json.load(f)

    token = os.environ.get("GIST_TOKEN")
    gist_id = os.environ.get("GIST_ID")
    if not token or not gist_id:
        sys.exit(
            "خطأ: لا يوجد closed_trades.json محليًا، ولم يتم ضبط "
            "GIST_TOKEN و GIST_ID كمتغيرات بيئة لجلبه من الـ Gist."
        )

    r = requests.get(
        f"https://api.github.com/gists/{gist_id}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
        timeout=15,
    )
    r.raise_for_status()
    files = r.json().get("files", {})

    # يجمع السجل النشط + كل ملفات الأرشيف المرقّمة = التاريخ الكامل بلا أي سقف
    all_trades = []
    archive_names = sorted(fn for fn in files if fn.startswith(ARCHIVE_PREFIX))
    for name in archive_names:
        all_trades.extend(_read_json_file(files[name], name))
    if GIST_FILENAME in files:
        all_trades.extend(_read_json_file(files[GIST_FILENAME], GIST_FILENAME))

    if not all_trades and GIST_FILENAME not in files:
        sys.exit(f"لم يتم العثور على '{GIST_FILENAME}' داخل الـ Gist. الملفات المتوفرة: {list(files.keys())}")

    return all_trades


def num_targets(trade):
    return len(trade.get("tps") or [])


def is_full_success(trade):
    if trade.get("closed_reason") == "ALL_TP":
        return True
    hit = trade.get("hit_tps") or []
    total = num_targets(trade)
    return bool(total) and len(hit) >= total


def is_win(trade):
    if is_full_success(trade):
        return True
    return bool(trade.get("hit_tps"))


def pct(part, whole):
    return (part / whole * 100) if whole else 0.0


def get_pnl(trade):
    """الربح/الخسارة الصافية للصفقة كنسبة مئوية، أو None لو الحقل غير موجود."""
    for key in PNL_KEYS:
        val = trade.get(key)
        if val is None:
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


def print_section_by_type(trades):
    print("=" * 70)
    print("1) نتائج الإشارات حسب النوع (رسمية / مبكرة)")
    print("=" * 70)

    by_type = defaultdict(list)
    for t in trades:
        if t.get("type") in ("official", "early"):
            by_type[t["type"]].append(t)

    labels = {"official": "🔴 رسمية", "early": "🔵 مبكرة"}
    for ttype in ("official", "early"):
        group = by_type.get(ttype, [])
        n = len(group)
        wins = sum(1 for t in group if is_win(t))
        full = sum(1 for t in group if is_full_success(t))
        print(f"\n{labels[ttype]} — إجمالي: {n} صفقة")
        if n:
            print(f"  نسبة لمس هدف واحد على الأقل : {wins}/{n}  ({pct(wins, n):.1f}%)")
            print(f"  نسبة الوصول لكامل الأهداف   : {full}/{n}  ({pct(full, n):.1f}%)")
        else:
            print("  لا توجد صفقات بعد.")
    return by_type


def print_section_multi_target(trades):
    print("\n" + "=" * 70)
    print("2) كل الإشارات (رسمية + مبكرة) التي كان لها هدفان أو أكثر")
    print("=" * 70)

    multi = [t for t in trades if t.get("type") in ("official", "early") and num_targets(t) >= 2]
    if not multi:
        print("لا توجد صفقات بهدفين أو أكثر حتى الآن.")
        return multi

    print(f"الإجمالي: {len(multi)} صفقة\n")
    for t in multi:
        symbol = t.get("symbol", "UNKNOWN")
        ttype = "رسمية" if t["type"] == "official" else "مبكرة"
        n = num_targets(t)
        hit = len(t.get("hit_tps") or [])
        reason = t.get("closed_reason", "؟")
        status = "✅ كل الأهداف" if is_full_success(t) else f"❌ {reason} (تحقق {hit}/{n})"
        print(f"  {symbol:<12} | {ttype:<6} | أهداف: {n} | {status}")

    return multi


def print_section_per_symbol(multi_target_trades):
    print("\n" + "=" * 70)
    print("3) نسبة نجاح كل عملة بالوصول لكامل الأهداف (من صفقات الهدفين فأكثر)")
    print("=" * 70)

    if not multi_target_trades:
        print("لا توجد بيانات كافية بعد.")
        return

    by_symbol = defaultdict(list)
    for t in multi_target_trades:
        by_symbol[t.get("symbol", "UNKNOWN")].append(t)

    rows = []
    for symbol, group in by_symbol.items():
        n = len(group)
        full = sum(1 for t in group if is_full_success(t))
        rows.append((symbol, n, full, pct(full, n)))

    rows.sort(key=lambda r: (-r[3], -r[1]))

    print(f"\n{'العملة':<12}{'عدد الصفقات':<14}{'وصلت لكل الأهداف':<20}{'النسبة'}")
    print("-" * 60)
    for symbol, n, full, p in rows:
        print(f"{symbol:<12}{n:<14}{full:<20}{p:.1f}%")


def print_section_breakout_totals(trades):
    print("\n" + "=" * 70)
    print("4) الانفجار (breakout): مجموع الربح والخسارة")
    print("=" * 70)

    group = [t for t in trades if t.get("type") == "breakout"]
    n_all = len(group)
    print(f"\n🟠 انفجار — إجمالي: {n_all} صفقة")
    if not n_all:
        print("  لا توجد صفقات انفجار بعد.")
        return

    pnls, missing = [], 0
    for t in group:
        p = get_pnl(t)
        if p is None:
            missing += 1
        else:
            pnls.append(p)

    if missing:
        print(f"  ⚠️ {missing} صفقة بدون حقل ربح/خسارة (استُبعدت من المجاميع)")
    if not pnls:
        print("  لا توجد بيانات ربح/خسارة قابلة للحساب لهذا النوع.")
        return

    wins = [p for p in pnls if p > BREAKEVEN_BAND_PCT]
    losses = [p for p in pnls if p < -BREAKEVEN_BAND_PCT]
    flat = len(pnls) - len(wins) - len(losses)

    n = len(pnls)
    print(f"  نجاح {pct(len(wins), n):.0f}% (رابحة {len(wins)} / خاسرة {len(losses)})"
          f" | مجموع ربح الرابحة: {sum(wins):+.2f}% | مجموع خسارة الخاسرة: {sum(losses):+.2f}%")
    if wins:
        print(f"  متوسط الربح في الصفقة الرابحة   : {sum(wins) / len(wins):+.2f}%")
    if losses:
        print(f"  متوسط الخسارة في الصفقة الخاسرة : {sum(losses) / len(losses):+.2f}%")
    if flat:
        print(f"  تعادل (±{BREAKEVEN_BAND_PCT}%) : {flat} صفقة")
    print(f"  الصافي الإجمالي: {sum(pnls):+.2f}%  (متوسط {sum(pnls) / n:+.2f}% للصفقة)")


def main():
    trades = load_trades()
    if not isinstance(trades, list):
        sys.exit("خطأ: الملف لا يحتوي على قائمة صفقات كما هو متوقع.")

    print_section_by_type(trades)
    multi = print_section_multi_target(trades)
    print_section_per_symbol(multi)
    print_section_breakout_totals(trades)


if __name__ == "__main__":
    main()
