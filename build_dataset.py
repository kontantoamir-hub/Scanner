"""
build_dataset.py
=================
يبني dataset جاهز للتدريب من صفقات النوع "official" (رسمية) فقط.

المصدر: الأرشيف الكامل (الملف النشط closed_trades.json + كل ملفات الأرشيف عبر
archive_gists_chain.json -- أبدًا Gist واحد أو الملف النشط فقط).

Target: تصنيف ثنائي outcome (1 = فوز، 0 = خسارة) حسب net_pnl_pct بنفس منطق
compute_stats في scanner.py. الصفقات "المحايدة" (داخل BREAKEVEN_BAND_PCT) تُستبعد
من الـdataset لأنها لا تمثّل فوزًا أو خسارة واضحة (تصنيف ثنائي نظيف).

Features المُستخرجة (كلها مسجّلة أصلاً مع كل صفقة official في scanner.py):
  - score              (رقمي)  : الدرجة الإجمالية للإشارة وقت الدخول
  - initial_risk_pct   (رقمي)  : المخاطرة الأصلية كنسبة من سعر الدخول (SL بعيد كام%)
  - rsi_state          (فئوي)  : overbought / neutral (وربما قيم أخرى حسب الكود)
  - macd_bull          (منطقي)
  - bb_state           (فئوي)  : mid / upper
  - vol_confirm        (منطقي)
  - ranging            (منطقي) : ADX الخاص بالعملة نفسها (فلتر "سوق عرضي" الحالي -- يختلف عن market_regime)
  - near_resistance    (منطقي)
  - obv_confirm        (منطقي)
  - htf_aligned        (منطقي)
  - trend_up           (منطقي) : اتجاه EMA9/21 وقت الدخول
  - squeeze            (منطقي)
  - accumulation       (منطقي)
  - divergence         (منطقي)
  - extended           (منطقي)
  - market_regime      (فئوي)  : trending_up / trending_down / ranging (BTCUSDT) -- قد تكون
                                  فارغة للصفقات الأقدم من 17 سبتمبر 2026 (قبل إضافة الفيتشر)

القيم المنطقية (True/False/None) تُكتب كما هي بالـCSV؛ التحويل لأرقام (0/1/NaN) وأي
one-hot encoding للفئوي يُترك لسكريبت التدريب (train_model.py) حتى يبقى هذا الملف
مجرد "استخراج بيانات خام نظيفة" بدون قرارات نمذجة مبكرة.

تشغيل: GIST_TOKEN و GIST_ID لازم يكونوا معرّفين.
    python3 build_dataset.py
يُنتج: dataset_official.csv (في نفس المجلد)
"""

import csv
import scanner

FEATURE_COLUMNS = [
    "score", "initial_risk_pct", "rsi_state", "macd_bull", "bb_state",
    "vol_confirm", "ranging", "near_resistance", "obv_confirm", "htf_aligned",
    "trend_up", "squeeze", "accumulation", "divergence", "extended", "market_regime",
]
OUTPUT_COLUMNS = ["symbol", "opened_at", "closed_at", "closed_reason", "net_pnl_pct", "outcome"] + FEATURE_COLUMNS

OUTPUT_FILE = "dataset_official.csv"


def get_full_content(file_meta):
    if not file_meta:
        return None
    if file_meta.get("truncated") and file_meta.get("raw_url"):
        r = scanner.requests.get(file_meta["raw_url"], timeout=20)
        r.raise_for_status()
        return r.text
    return file_meta.get("content")


def load_json_file(files_dict, filename, default):
    meta = files_dict.get(filename)
    if meta is None:
        return default
    content = get_full_content(meta)
    if not content:
        return default
    import json
    return json.loads(content)


def load_full_archive():
    main_files = scanner._gist_get_all_files()
    all_trades = list(load_json_file(main_files, scanner.CLOSED_GIST_FILE, []))
    chain = load_json_file(main_files, scanner.ARCHIVE_CHAIN_FILE, [])
    for gist_id in chain:
        archive_files = main_files if gist_id == scanner.GIST_ID else scanner._gist_get_all_files_for(gist_id)
        archive_names = sorted(fn for fn in archive_files if fn.startswith(scanner.ARCHIVE_PREFIX))
        for fn in archive_names:
            all_trades.extend(load_json_file(archive_files, fn, []))
    return all_trades


def net_pnl_pct(t):
    entry, exit_price = t.get("entry"), t.get("exit_price")
    if not entry or not exit_price:
        return None
    raw_pct = (exit_price - entry) / entry * 100
    return raw_pct - scanner.TRADING_FEE_PCT


def outcome_label(pnl):
    """1 = فوز، 0 = خسارة، None = محايدة (تُستبعد من الـdataset)."""
    if pnl is None:
        return None
    if pnl > scanner.BREAKEVEN_BAND_PCT:
        return 1
    if pnl < -scanner.BREAKEVEN_BAND_PCT:
        return 0
    return None


def build_row(t, pnl, outcome):
    entry = t.get("entry")
    initial_risk = t.get("initial_risk")
    initial_risk_pct = (initial_risk / entry * 100) if (initial_risk and entry) else None
    row = {
        "symbol": t.get("symbol"),
        "opened_at": t.get("opened_at"),
        "closed_at": t.get("closed_at"),
        "closed_reason": t.get("closed_reason"),
        "net_pnl_pct": round(pnl, 4) if pnl is not None else None,
        "outcome": outcome,
        "score": t.get("score"),
        "initial_risk_pct": round(initial_risk_pct, 4) if initial_risk_pct is not None else None,
        "rsi_state": t.get("rsi_state"),
        "macd_bull": t.get("macd_bull"),
        "bb_state": t.get("bb_state"),
        "vol_confirm": t.get("vol_confirm"),
        "ranging": t.get("ranging"),
        "near_resistance": t.get("near_resistance"),
        "obv_confirm": t.get("obv_confirm"),
        "htf_aligned": t.get("htf_aligned"),
        "trend_up": t.get("trend_up"),
        "squeeze": t.get("squeeze"),
        "accumulation": t.get("accumulation"),
        "divergence": t.get("divergence"),
        "extended": t.get("extended"),
        "market_regime": t.get("market_regime"),
    }
    return row


def main():
    print("📥 تحميل الأرشيف الكامل (الملف النشط + كل ملفات الأرشيف) ...")
    all_trades = load_full_archive()
    print(f"✅ {len(all_trades)} صفقة إجمالاً")

    official = [t for t in all_trades if t.get("type") == "official"]
    print(f"🔴 {len(official)} صفقة من نوع 'رسمية' (official)")

    rows = []
    skipped_no_pnl = skipped_neutral = 0
    for t in official:
        pnl = net_pnl_pct(t)
        if pnl is None:
            skipped_no_pnl += 1
            continue
        outcome = outcome_label(pnl)
        if outcome is None:
            skipped_neutral += 1
            continue
        rows.append(build_row(t, pnl, outcome))

    print(f"📊 صفوف الـdataset النهائية: {len(rows)}")
    print(f"   مستبعدة (بدون entry/exit صالح): {skipped_no_pnl}")
    print(f"   مستبعدة (محايدة ضمن BREAKEVEN_BAND_PCT): {skipped_neutral}")

    wins = sum(1 for r in rows if r["outcome"] == 1)
    losses = len(rows) - wins
    print(f"   توزيع الـtarget: فوز={wins} ({wins/len(rows)*100:.1f}%) | خسارة={losses} ({losses/len(rows)*100:.1f}%)" if rows else "   لا صفوف!")

    missing_regime = sum(1 for r in rows if r["market_regime"] is None)
    if missing_regime:
        print(f"   ⚠️ {missing_regime} صفقة بدون market_regime (أقدم من تاريخ إضافة الفيتشر أو تعذّر حسابه)")

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    print(f"\n💾 تم الحفظ: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
