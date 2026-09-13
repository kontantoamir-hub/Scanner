"""
symbol_stats.py — تحليل الإشارات المكررة لكل عملة على حدة

الفكرة: عملة معينة ممكن تطلق إشارة (رسمية/مبكرة/انفجار/تجريبية) أكثر من مرة عبر
الوقت. لو هالعملة بطبيعتها فاشلة مع منطق الإستراتيجية (أو رابحة بشكل غير ممثّل)،
فكل تكرار لها يُحسب كصفقة مستقلة بالإحصائيات العامة (trade_stats.py) — وهذا ممكن
يشوّه نسبة الرابحة/الخاسرة الكلية: عدد قليل من العملات "الثقيلة" (بعدد تكرار عالٍ)
قد يكون هو المتحكم الفعلي بمعظم الربح أو الخسارة الكلية، بينما باقي العملات (كل
وحدة منها بإشارة واحدة أو اثنتين) أداؤها مختلف تمامًا.

هذا السكربت يقرأ نفس سجل الصفقات المغلقة (السجل النشط + كل ملفات الأرشيف عبر كل
Gists السلسلة، بنفس منطق trade_stats.py) ويجمّعه حسب الرمز (symbol)، بصرف النظر
عن نوع الإشارة (رسمية/مبكرة/انفجار/تجريبية) — كل تكرار لنفس الرمز يُحسب ضمن نفس
المجموعة. لكل عملة يُحسب:
  - عدد مرات ظهورها كصفقة مغلقة (رابحة + خاسرة، بدون المحايدة — نفس استبعاد
    trade_stats.py لأن إشارة الإغلاق المحايد أُزيلت من البوت)
  - عدد المرات الرابحة ونسبة نجاحها الخاصة
  - عدد المرات الخاسرة ونسبة فشلها الخاصة
  - مجموع نسب الربح لكل صفقاتها الرابحة (وليس المتوسط)
  - مجموع نسب الخسارة لكل صفقاتها الخاسرة (وليس المتوسط)
  - مساهمتها٪ من إجمالي مجموع الربح الكلي (لكل العملات) ومن إجمالي مجموع الخسارة
    الكلي — هذا الرقم تحديدًا هو اللي يجاوب: "هل عدد قليل من العملات يتحكم فعليًا
    بمعظم نسبة الربح/الخسارة بالتقرير العام؟"

لا يُعدّل أي شيء بمنطق البوت أو ملفاته — قراءة وعرض فقط.

المتغيرات المطلوبة (نفس Secrets المستخدمة في scanner.py و trade_stats.py):
  GIST_TOKEN, GIST_ID
اختياري لإرسال التقرير عبر تيليجرام بدل الطباعة فقط:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
اختياري لضبط الحد الأدنى لعدد التكرار كي تظهر العملة ضمن قسم "العملات المكررة"
(الافتراضي: عملة واحدة تكفي لتظهر بالجدول الكامل، لكن قسم "المكررة فعليًا" يشترط
تكرار حقيقي، انظر SYMBOL_REPEAT_MIN أدناه):
  SYMBOL_REPEAT_MIN
"""

import os
import json
import requests

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")
CLOSED_GIST_FILE = "closed_trades.json"          # السجل النشط: أحدث الصفقات فقط
ARCHIVE_PREFIX = "closed_trades_archive_"        # ملفات الأرشيف المرقّمة
ARCHIVE_CHAIN_FILE = "archive_gists_chain.json"  # سلسلة معرّفات Gists الأرشيف (نفس ملف scanner.py)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# الحد الأدنى لعدد مرات ظهور نفس الرمز كي يُحتسب "مكررًا" بقسم التقرير المخصص لذلك
# (القسم الكامل بالأسفل يعرض كل العملات بصرف النظر عن هذا الحد)
SYMBOL_REPEAT_MIN = int(os.environ.get("SYMBOL_REPEAT_MIN", "2"))

TYPE_LABELS = {
    "official": "رسمية",
    "early": "مبكرة",
    "breakout": "انفجار",
    "experimental": "تجريبية",
}


def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def _read_json_file(file_entry, filename):
    """يقرأ محتوى ملف من استجابة Gist، مع الجلب من raw_url لو كان الملف مبتورًا (truncated)."""
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
    try:
        return json.loads(content)
    except Exception as e:
        print(f"⚠️ تعذّر تحليل {filename}: {e}")
        return []


def _gist_get_all_files(gist_id):
    """يقرأ كل ملفات أي Gist (الرئيسي أو أي Gist أرشيف بالسلسلة) دفعة واحدة."""
    r = requests.get(f"https://api.github.com/gists/{gist_id}", headers=_gist_headers(), timeout=15)
    r.raise_for_status()
    return r.json().get("files", {})


def load_closed_trades():
    """
    يجمع السجل النشط مع كل ملفات الأرشيف المرقّمة، عابرًا كل سلسلة archive_gists_chain.json
    (قد تمتد لأكثر من Gist واحد) — نفس منطق trade_stats.py بالضبط، لضمان أن هذا السكربت
    يرى نفس مجموع الصفقات المغلقة تمامًا بلا أي فرق أو نقص.
    """
    if not GIST_TOKEN or not GIST_ID:
        raise SystemExit("❌ GIST_TOKEN أو GIST_ID غير موجودين في متغيرات البيئة.")

    main_files = _gist_get_all_files(GIST_ID)
    all_trades = []

    chain_raw = main_files.get(ARCHIVE_CHAIN_FILE, {}).get("content")
    try:
        chain = json.loads(chain_raw) if chain_raw else []
    except Exception as e:
        print(f"⚠️ تعذّر تحليل {ARCHIVE_CHAIN_FILE}: {e}")
        chain = []

    gist_ids_to_scan = []
    for gid in [GIST_ID] + chain:
        if gid not in gist_ids_to_scan:
            gist_ids_to_scan.append(gid)

    archive_trades_count = 0
    for gid in gist_ids_to_scan:
        files = main_files if gid == GIST_ID else _gist_get_all_files(gid)
        archive_names = sorted(fn for fn in files if fn.startswith(ARCHIVE_PREFIX))
        for name in archive_names:
            chunk = _read_json_file(files[name], f"{name} (Gist {gid})")
            all_trades.extend(chunk)
            archive_trades_count += len(chunk)

    active_trades = []
    if CLOSED_GIST_FILE in main_files:
        active_trades = _read_json_file(main_files[CLOSED_GIST_FILE], CLOSED_GIST_FILE)
    all_trades.extend(active_trades)

    print(
        f"📦 إجمالي الصفقات المغلقة المجمّعة: {len(all_trades)} "
        f"(من الأرشيف عبر {len(gist_ids_to_scan)} Gist: {archive_trades_count}, "
        f"من السجل النشط: {len(active_trades)})"
    )
    return all_trades


def classify(trade):
    """win / loss / neutral — نفس منطق trade_stats.py بالضبط، لضمان اتساق التصنيف بين التقريرين."""
    reason = trade.get("closed_reason", "UNKNOWN")
    hit = len(trade.get("hit_tps") or [])
    if reason == "ALL_TP" or hit > 0:
        return "win"
    if reason == "SL" and hit == 0:
        return "loss"
    return "neutral"


def pnl_pct(trade):
    entry, exit_price = trade.get("entry"), trade.get("exit_price")
    if entry and exit_price:
        return (exit_price - entry) / entry * 100
    return None


def build_symbol_stats(trades):
    """
    يجمّع الصفقات حسب الرمز (symbol) بصرف النظر عن نوع الإشارة. لكل رمز:
    total / win / loss / win_pnl_sum / loss_pnl_sum / types (تنويع الأنواع اللي
    ظهرت لهذا الرمز، مفيد لمعرفة هل التكرار جاي من نوع إشارة واحد بالذات أو أكثر).
    صفقات "neutral" تُستبعد بالكامل، نفس trade_stats.py.
    """
    per_symbol = {}
    total_win_pnl_all = 0.0
    total_loss_pnl_all = 0.0

    for t in trades:
        outcome = classify(t)
        if outcome == "neutral":
            continue

        symbol = t.get("symbol", "?")
        ttype = t.get("type", "official")
        pnl = pnl_pct(t)

        s = per_symbol.setdefault(symbol, {
            "total": 0, "win": 0, "loss": 0,
            "win_pnl_sum": 0.0, "loss_pnl_sum": 0.0,
            "types": {},
        })
        s["total"] += 1
        s[outcome] += 1
        s["types"][ttype] = s["types"].get(ttype, 0) + 1

        if pnl is not None:
            if outcome == "win":
                s["win_pnl_sum"] += pnl
                total_win_pnl_all += pnl
            else:
                s["loss_pnl_sum"] += pnl
                total_loss_pnl_all += pnl

    return per_symbol, total_win_pnl_all, total_loss_pnl_all


def build_report(trades):
    if not trades:
        return "لا توجد صفقات مغلقة بعد في السجل."

    per_symbol, total_win_pnl_all, total_loss_pnl_all = build_symbol_stats(trades)

    if not per_symbol:
        return "لا توجد صفقات (رابحة/خاسرة) مؤهلة للتحليل — كلها محايدة أو بلا سجل صالح."

    total_symbols = len(per_symbol)
    repeated = {sym: s for sym, s in per_symbol.items() if s["total"] >= SYMBOL_REPEAT_MIN}

    lines = [
        "📊 تحليل الإشارات المكررة حسب العملة",
        f"إجمالي العملات التي ظهرت بصفقات مغلقة: {total_symbols}",
        f"عملات مكررة (≥ {SYMBOL_REPEAT_MIN} إشارة): {len(repeated)}",
        f"مجموع ربح كل الرابحة (كل العملات): {total_win_pnl_all:+.2f}%",
        f"مجموع خسارة كل الخاسرة (كل العملات): {total_loss_pnl_all:+.2f}%",
        "",
    ]

    if repeated:
        # ترتيب تنازليًا حسب عدد التكرار — العملات الأكثر تكرارًا هي الأهم لفحص
        # هل هي المتحكمة الفعلية بنسبة الربح/الخسارة العامة بالتقرير الكلي
        sorted_repeated = sorted(repeated.items(), key=lambda kv: kv[1]["total"], reverse=True)

        lines.append("— العملات المكررة (مرتبة حسب عدد التكرار) —")
        for symbol, s in sorted_repeated:
            wr = (s["win"] / s["total"] * 100) if s["total"] else 0
            types_str = "، ".join(
                f"{TYPE_LABELS.get(tt, tt)}×{cnt}" for tt, cnt in sorted(s["types"].items(), key=lambda x: -x[1])
            )
            win_contrib = (s["win_pnl_sum"] / total_win_pnl_all * 100) if total_win_pnl_all else 0
            loss_contrib = (s["loss_pnl_sum"] / total_loss_pnl_all * 100) if total_loss_pnl_all else 0

            lines.append(
                f"{symbol.replace('USDT', '/USDT')}: تكررت {s['total']} مرة | "
                f"نجاح {wr:.0f}% (رابحة {s['win']} / خاسرة {s['loss']}) | "
                f"أنواع: {types_str}"
            )
            if s["win"] > 0:
                lines.append(
                    f"    مجموع ربحها: {s['win_pnl_sum']:+.2f}% "
                    f"(تساهم بـ{win_contrib:.1f}% من إجمالي ربح كل العملات)"
                )
            if s["loss"] > 0:
                lines.append(
                    f"    مجموع خسارتها: {s['loss_pnl_sum']:+.2f}% "
                    f"(تساهم بـ{loss_contrib:.1f}% من إجمالي خسارة كل العملات)"
                )
        lines.append("")
    else:
        lines.append(f"لا توجد عملات تكررت {SYMBOL_REPEAT_MIN} مرة أو أكثر بعد.")
        lines.append("")

    # جدول كامل لكل العملات (حتى غير المكررة) — مرتب حسب مجموع مساهمتها بالخسارة تنازليًا،
    # عشان أول ما تشوف التقرير تعرف فورًا أي عملة (مكررة أو لا) هي أكبر مصدر خسارة إجمالية
    all_sorted = sorted(
        per_symbol.items(),
        key=lambda kv: kv[1]["loss_pnl_sum"],
    )
    lines.append("— كل العملات مرتبة حسب حجم مساهمتها بالخسارة الإجمالية (الأكبر خسارة أولًا) —")
    for symbol, s in all_sorted:
        wr = (s["win"] / s["total"] * 100) if s["total"] else 0
        lines.append(
            f"{symbol.replace('USDT', '/USDT')}: {s['total']} صفقة | نجاح {wr:.0f}% "
            f"(رابحة {s['win']} / خاسرة {s['loss']}) | "
            f"ربح: {s['win_pnl_sum']:+.2f}% | خسارة: {s['loss_pnl_sum']:+.2f}%"
        )

    return "\n".join(lines)


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    chunk = 3800
    for i in range(0, len(text), chunk):
        try:
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text[i:i + chunk]}, timeout=15)
        except Exception as e:
            print("تعذّر إرسال التقرير عبر تيليجرام:", e)


def main():
    trades = load_closed_trades()
    report = build_report(trades)
    print(report)
    send_telegram(report)


if __name__ == "__main__":
    main()
