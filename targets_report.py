#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
targets_report.py
==================
سكربت تحليل يقرأ closed_trades.json من نفس الـ Gist المستخدم في scanner.py
(نفس المفاتيح الحرفية: type / symbol / tps / hit_tps / closed_reason)
ويعرض 3 تقارير:

  1) نتائج الإشارات حسب النوع (رسمية / مبكرة): عدد الصفقات، نسبة لمس
     أي هدف (win)، ونسبة الوصول لكامل الأهداف الموضوعة (ALL_TP).
  2) قائمة كل الصفقات (رسمية أو مبكرة) التي كان لها هدفان (TP) أو أكثر
     عند الفتح، مع نتيجة كل واحدة.
  3) نسبة نجاح كل عملة (symbol) على حدة في الوصول لكامل أهدافها، من بين
     صفقاتها التي كان لها هدفان فأكثر.

صفقات breakout مستبعدة بالكامل من هذا التقرير (الطلب كان عن الرسمية والمبكرة فقط).

الإعداد
-------
نفس متغيرات البيئة المستخدمة في scanner.py:
    GIST_TOKEN, GIST_ID
(أو ضع ملف closed_trades.json محليًا بجانب السكربت لتشغيله بدون شبكة)

تشغيل:
    python targets_report.py
"""

import os
import sys
import json
from collections import defaultdict

import requests

GIST_FILENAME = "closed_trades.json"


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
    if GIST_FILENAME not in files:
        sys.exit(f"لم يتم العثور على '{GIST_FILENAME}' داخل الـ Gist. الملفات المتوفرة: {list(files.keys())}")

    return json.loads(files[GIST_FILENAME]["content"])


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


def main():
    trades = load_trades()
    if not isinstance(trades, list):
        sys.exit("خطأ: الملف لا يحتوي على قائمة صفقات كما هو متوقع.")

    print_section_by_type(trades)
    multi = print_section_multi_target(trades)
    print_section_per_symbol(multi)


if __name__ == "__main__":
    main()
