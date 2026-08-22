#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
official_success_factors.py
============================
تحليل مقارن للإشارات الرسمية (type == "official") **وصفقات الانفجار**
(type == "breakout" — أُضيف لاحقًا بعد أن صار Breakout يحتوي نفس الحقول
التشخيصية الثمانية) — لكل نوع على حدة، يقارن كل الحقول التشخيصية بين مجموعتين:

  - ✅ الصفقات التي وصلت لكل أهدافها (closed_reason == "ALL_TP")
  - ❌ الصفقات التي لم تصل (SL / EXPIRED)

الهدف: كشف أي عامل (أو مستوى منه) كان حاضرًا بشكل متكرر بالمجموعة الناجحة
وغائبًا/نادرًا بالفاشلة (أو العكس)، لاستخدامه لاحقًا كفلتر إضافي.
لصفقات breakout تحديدًا يُضاف قسم خامس (analyze_breakout_factors) يقارن عوامل
جودة الاختراق الخاصة (trend_support / macd_bull / rsi_ok من breakout_details)
غير الموجودة إطلاقًا بصفقات official.

الحقول المفحوصة (كما تُحفظ فعليًا في scanner.py):
  - score            (رقم متصل)      -> متوسط لكل مجموعة + توزيع دلاء
  - rsi_state        (-1 / 0 / 1)     -> توزيع نسبي لكل مجموعة
  - bb_state          (-1 / 0 / 1)    -> توزيع نسبي لكل مجموعة
  - macd_bull, vol_confirm, ranging, near_resistance, obv_confirm,
    extended, squeeze, accumulation, divergence   (True/False/None) -> نسبة الحضور
  - htf_aligned       (True / False / None)         -> توزيع نسبي
  - (breakout فقط) trend_support, macd_bull, rsi_ok من breakout_details

تشغيل:
    export GIST_TOKEN=xxx
    export GIST_ID=xxx
    python official_success_factors.py
"""

import os
import sys
import json
from collections import defaultdict

import requests

GIST_FILENAME = "closed_trades.json"

BOOL_FIELDS = [
    "macd_bull", "vol_confirm", "ranging", "near_resistance", "obv_confirm",
    "extended", "squeeze", "accumulation", "divergence",
]
TRISTATE_FIELDS = {  # قيم -1/0/1 وشرح كل قيمة
    "rsi_state": {1: "تشبع بيعي (1)", 0: "محايد (0)", -1: "تشبع شرائي (-1)"},
    "bb_state": {1: "الحد السفلي (1)", 0: "منتصف النطاق (0)", -1: "الحد العلوي (-1)"},
}
TRIBOOL_FIELDS = ["htf_aligned"]  # True/False/None

# عوامل جودة الاختراق الخاصة بصفقات breakout فقط (محفوظة داخل breakout_details)
BREAKOUT_FACTOR_KEYS = ["trend_support", "macd_bull", "rsi_ok"]


def load_trades():
    local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "closed_trades.json")
    if os.path.exists(local_path):
        with open(local_path, "r", encoding="utf-8") as f:
            return json.load(f)

    token = os.environ.get("GIST_TOKEN")
    gist_id = os.environ.get("GIST_ID")
    if not token or not gist_id:
        sys.exit("خطأ: لا يوجد closed_trades.json محليًا، ولم يتم ضبط GIST_TOKEN/GIST_ID.")

    r = requests.get(
        f"https://api.github.com/gists/{gist_id}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
        timeout=15,
    )
    r.raise_for_status()
    files = r.json().get("files", {})
    if GIST_FILENAME not in files:
        sys.exit(f"لم يتم العثور على '{GIST_FILENAME}'. الملفات المتوفرة: {list(files.keys())}")
    return json.loads(files[GIST_FILENAME]["content"])


def pct(part, whole):
    return (part / whole * 100) if whole else 0.0


def score_bucket(score):
    if score is None:
        return "غير معروف"
    if score >= 3.5:
        return "3.5+"
    if score >= 2.5:
        return "2.5–3.49"
    if score >= 1.5:
        return "1.5–2.49"
    return "<1.5"


def analyze_group(win, lose, group_label, total_label):
    """يشغّل نفس التحليل المقارن (الدرجة + الحقول الثنائية + الحقول ثلاثية القيم +
    htf_aligned) على أي مجموعة صفقات (رسمية أو انفجار) — نفس منطق official_success_factors
    الأصلي، مستخرج كدالة عشان يُعاد استخدامه بدل تكرار الكود لكل نوع."""
    print("=" * 72)
    print(f"تحليل مقارن لـ{group_label} — إجمالي {total_label} صفقة")
    print(f"✅ وصلت لكل الأهداف: {len(win)}   |   ❌ لم تصل (SL/EXPIRED): {len(lose)}")
    print("=" * 72)

    # ---- 1) متوسط الدرجة + توزيع دلاء الدرجة ----
    print("\n--- الدرجة (score) ---")
    avg_win = sum(t.get("score", 0) or 0 for t in win) / len(win) if win else 0
    avg_lose = sum(t.get("score", 0) or 0 for t in lose) / len(lose) if lose else 0
    print(f"متوسط الدرجة عند النجاح: {avg_win:.2f}   |   متوسط الدرجة عند الفشل: {avg_lose:.2f}")

    print(f"\n{'فئة الدرجة':<14}{'% بمجموعة النجاح':<20}{'% بمجموعة الفشل':<20}{'الفرق'}")
    print("-" * 70)
    for bucket in ("<1.5", "1.5–2.49", "2.5–3.49", "3.5+"):
        w = sum(1 for t in win if score_bucket(t.get("score")) == bucket)
        l = sum(1 for t in lose if score_bucket(t.get("score")) == bucket)
        wp, lp = pct(w, len(win)), pct(l, len(lose))
        print(f"{bucket:<14}{wp:<20.1f}{lp:<20.1f}{wp - lp:+.1f}")

    # ---- 2) الحقول الثنائية (True/False) ----
    print("\n--- الحقول الثنائية (نسبة الحضور = True) ---")
    print(f"\n{'العامل':<18}{'% حاضر بالنجاح':<20}{'% حاضر بالفشل':<20}{'الفرق'}")
    print("-" * 70)
    rows = []
    for field in BOOL_FIELDS:
        w = sum(1 for t in win if t.get(field) is True)
        l = sum(1 for t in lose if t.get(field) is True)
        wp, lp = pct(w, len(win)), pct(l, len(lose))
        rows.append((field, wp, lp, wp - lp))
    rows.sort(key=lambda r: -abs(r[3]))
    for field, wp, lp, diff in rows:
        print(f"{field:<18}{wp:<20.1f}{lp:<20.1f}{diff:+.1f}")

    # ---- 3) الحقول ثلاثية القيم (-1/0/1) ----
    for field, labels in TRISTATE_FIELDS.items():
        print(f"\n--- {field} ---")
        print(f"\n{'القيمة':<20}{'% بالنجاح':<18}{'% بالفشل':<18}{'الفرق'}")
        print("-" * 70)
        for val, label in labels.items():
            w = sum(1 for t in win if t.get(field) == val)
            l = sum(1 for t in lose if t.get(field) == val)
            wp, lp = pct(w, len(win)), pct(l, len(lose))
            print(f"{label:<20}{wp:<18.1f}{lp:<18.1f}{wp - lp:+.1f}")

    # ---- 4) htf_aligned (True/False/None) ----
    for field in TRIBOOL_FIELDS:
        print(f"\n--- {field} ---")
        print(f"\n{'القيمة':<20}{'% بالنجاح':<18}{'% بالفشل':<18}{'الفرق'}")
        print("-" * 70)
        for val, label in ((True, "متوافقة (True)"), (False, "متعاكسة (False)"), (None, "لم تُفحص (None)")):
            w = sum(1 for t in win if t.get(field) == val)
            l = sum(1 for t in lose if t.get(field) == val)
            wp, lp = pct(w, len(win)), pct(l, len(lose))
            print(f"{label:<20}{wp:<18.1f}{lp:<18.1f}{wp - lp:+.1f}")


def analyze_breakout_factors(win, lose):
    """قسم إضافي خاص بصفقات الانفجار فقط: يقارن عوامل جودة الاختراق الثلاثة
    (trend_support / macd_bull / rsi_ok من breakout_details) بين الناجحة والفاشلة —
    هذه العوامل غير موجودة إطلاقًا بصفقات official، لذا قسم منفصل عن analyze_group."""
    print("\n--- عوامل جودة الاختراق (breakout_details) ---")
    print(f"\n{'العامل':<18}{'% حاضر بالنجاح':<20}{'% حاضر بالفشل':<20}{'الفرق'}")
    print("-" * 70)
    rows = []
    for field in BREAKOUT_FACTOR_KEYS:
        w = sum(1 for t in win if (t.get("breakout_details") or {}).get(field) is True)
        l = sum(1 for t in lose if (t.get("breakout_details") or {}).get(field) is True)
        wp, lp = pct(w, len(win)), pct(l, len(lose))
        rows.append((field, wp, lp, wp - lp))
    rows.sort(key=lambda r: -abs(r[3]))
    for field, wp, lp, diff in rows:
        print(f"{field:<18}{wp:<20.1f}{lp:<20.1f}{diff:+.1f}")


def main():
    trades = load_trades()
    official = [t for t in trades if t.get("type") == "official"]
    breakout = [t for t in trades if t.get("type") == "breakout"]

    if not official and not breakout:
        sys.exit("لا توجد صفقات رسمية ولا صفقات انفجار بعد.")

    if official:
        win = [t for t in official if t.get("closed_reason") == "ALL_TP"]
        lose = [t for t in official if t.get("closed_reason") in ("SL", "EXPIRED")]
        analyze_group(win, lose, "الإشارات الرسمية", len(official))
    else:
        print("(لا توجد صفقات رسمية بعد — تم تخطي هذا القسم)")

    if breakout:
        print("\n\n")
        b_win = [t for t in breakout if t.get("closed_reason") == "ALL_TP"]
        b_lose = [t for t in breakout if t.get("closed_reason") in ("SL", "EXPIRED")]
        analyze_group(b_win, b_lose, "صفقات الانفجار (Breakout)", len(breakout))
        analyze_breakout_factors(b_win, b_lose)
    else:
        print("\n(لا توجد صفقات انفجار مغلقة بعد — تم تخطي هذا القسم، سيظهر تلقائيًا مع أول صفقات breakout مغلقة)")

    print("\n" + "=" * 72)
    print("ملاحظة: الفرق (win% - lose%) الأكبر (موجبًا أو سالبًا) هو أقوى مرشّح")
    print("لفلتر إضافي — لكن لا تُقرَّر فلترة نهائية إلا بعد عينة كافية (30+ لكل جهة على الأقل).")
    print("=" * 72)


if __name__ == "__main__":
    main()
