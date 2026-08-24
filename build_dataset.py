"""
build_dataset.py — تجريبي فقط
يقرأ closed_trades.json من الـGist، ويبني جدول بيانات (CSV) جاهز لتدريب نموذج ML.

المتغيرات البيئية المطلوبة (نفس المستخدمة بـscanner.py):
  GIST_TOKEN
  GIST_ID

الناتج: dataset.csv بنفس المجلد
"""

import os
import json
import requests
import pandas as pd

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")
TRADES_FILENAME = "closed_trades.json"  # عدّل الاسم إذا كان مختلفًا بالـGist عندك

# الحقول التشخيصية الثمانية
DIAGNOSTIC_FIELDS = [
    "rsi_state", "macd_bull", "bb_state", "vol_confirm",
    "ranging", "near_resistance", "obv_confirm", "htf_aligned",
]

# حقول إضافية مفيدة كـ features لو موجودة بسجل الصفقة
EXTRA_FIELDS = [
    "type",          # official / early / breakout
    "score",         # درجة التوافق الرسمية
    "early_source",  # accumulation / squeeze / extended / None
    "symbol",
    "timeframe",
]


def fetch_gist_trades():
    if not GIST_TOKEN or not GIST_ID:
        raise RuntimeError("لازم تحدد GIST_TOKEN و GIST_ID كمتغيرات بيئة قبل التشغيل")

    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {
        "Authorization": f"token {GIST_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    gist = resp.json()

    if TRADES_FILENAME not in gist["files"]:
        available = list(gist["files"].keys())
        raise RuntimeError(f"'{TRADES_FILENAME}' غير موجود بالـGist. الملفات المتوفرة: {available}")

    content = gist["files"][TRADES_FILENAME]["content"]
    return json.loads(content)


def compute_target(trade):
    """
    يحسب هدف التصنيف (label) وهدف الانحدار (net_return_pct) لكل صفقة.
    يعتمد على الحقول اللي ذكرتها سابقًا: closed_reason, hit_tps, tps
    """
    closed_reason = trade.get("closed_reason", "")
    net_return = trade.get("net_return_pct")  # لو محسوبة أصلاً بالسجل

    # لو ما فيه net_return_pct محفوظ، نحسبه تقريبيًا من entry/exit إذا متوفرين
    if net_return is None:
        entry = trade.get("entry")
        exit_price = trade.get("exit_price") or trade.get("exit")
        if entry and exit_price:
            net_return = ((exit_price - entry) / entry) * 100

    # تصنيف ثنائي: WIN=1 لو النتيجة ربح صافي موجب، غير ذلك 0
    label = None
    if net_return is not None:
        label = 1 if net_return > 0 else 0
    elif closed_reason:
        label = 1 if closed_reason == "ALL_TP" else 0

    return label, net_return


def build_dataframe(trades):
    rows = []
    skipped = 0

    for trade in trades:
        # نتجاهل الصفقات الناقصة الحقول الثمانية (القديمة قبل التوسيع)
        if not all(field in trade for field in DIAGNOSTIC_FIELDS):
            skipped += 1
            continue

        label, net_return = compute_target(trade)
        if label is None:
            skipped += 1
            continue

        row = {field: trade.get(field) for field in DIAGNOSTIC_FIELDS}
        for field in EXTRA_FIELDS:
            row[field] = trade.get(field)

        row["label"] = label
        row["net_return_pct"] = net_return
        rows.append(row)

    print(f"تم بناء {len(rows)} صف صالح، وتجاهل {skipped} صفقة (ناقصة حقول أو بدون نتيجة واضحة)")
    return pd.DataFrame(rows)


def main():
    trades = fetch_gist_trades()
    print(f"إجمالي الصفقات المسحوبة من الـGist: {len(trades)}")

    df = build_dataframe(trades)
    if df.empty:
        print("⚠️ ما فيه أي صفقات صالحة للتدريب حاليًا (كلها ناقصة الحقول الثمانية على الأغلب)")
        return

    df.to_csv("dataset.csv", index=False)
    print("تم الحفظ بـ dataset.csv")
    print(df["label"].value_counts())


if __name__ == "__main__":
    main()
