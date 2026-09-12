#!/usr/bin/env python3
"""
tp1_vs_sl_report.py
--------------------
يحسب من ملف closed_trades.json (+ أرشيفاته إن وُجدت):

1) عدد الصفقات "الناجحة" التي وصلت إلى الهدف الأول (TP1) على الأقل قبل أي
   إغلاق بالستوب لوس (بغض النظر عمّا حدث بعد ذلك).
2) عدد الصفقات "الخاسرة المباشرة" التي أُغلقت بالستوب لوس (SL) دون أن
   تصل إلى الهدف الأول إطلاقاً.

يدعم أيضاً تصنيف النتائج حسب نوع الإشارة (رسمية / مبكرة / انفجار / تجريبية)
إن كان الحقل موجوداً في بيانات الصفقة، ويرسل تقريراً على Telegram اختيارياً.

متغيرات البيئة المستخدمة:
  GIST_RAW_URL          رابط raw لملف closed_trades.json الرئيسي (إلزامي)
  ARCHIVE_CHAIN_URL     رابط raw لملف archive_gists_chain.json (اختياري)
                        هذا الملف عبارة عن قائمة Gist IDs، كل Gist منها
                        يحتوي على عدة ملفات أرشيف (closed_trades_archive_*.json)
                        يجلبها السكربت تلقائياً عبر GitHub API.
  GITHUB_TOKEN          توكن GitHub (اختياري - يرفع حد الطلبات لو عندك Gists
                        سرّية كثيرة أو أرشيف كبير؛ غير إلزامي للقراءة العامة)
  TELEGRAM_BOT_TOKEN    توكن بوت تيليجرام (اختياري - لإرسال التقرير)
  TELEGRAM_CHAT_ID      معرف الشات (اختياري - لإرسال التقرير)

الاستخدام المحلي:
  export GIST_RAW_URL="https://gist.githubusercontent.com/.../raw/closed_trades.json"
  python3 tp1_vs_sl_report.py
"""

import os
import sys
import json
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# إعدادات
# ---------------------------------------------------------------------------
GIST_RAW_URL = os.environ.get("GIST_RAW_URL", "").strip()
ARCHIVE_CHAIN_URL = os.environ.get("ARCHIVE_CHAIN_URL", "").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

GITHUB_API_BASE = "https://api.github.com/gists"

# أسماء بديلة محتملة للحقول (لتفادي فروقات بسيطة بين نسخ scanner.py المختلفة)
HIT_TPS_KEYS = ["hit_tps", "hit_targets", "tps_hit", "targets_hit"]
CLOSE_REASON_KEYS = ["close_reason", "reason", "closed_reason"]
TYPE_KEYS = ["type", "signal_type", "trade_type"]

SL_REASONS = {"SL", "sl", "stop_loss", "STOP_LOSS"}


def fetch_json(url: str, headers: dict = None):
    """يجلب ويحلل JSON من رابط. يعيد None عند أي خطأ (مع رسالة تحذير)."""
    if not url:
        return None
    req_headers = {"User-Agent": "tp1-vs-sl-report"}
    if headers:
        req_headers.update(headers)
    try:
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read().decode("utf-8")
        return json.loads(data)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
        print(f"⚠️ تعذر جلب/تحليل: {url} -> {e}", file=sys.stderr)
        return None


def github_api_headers():
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def fetch_gist_archive_trades(gist_id: str):
    """
    يجلب Gist أرشيف عبر GitHub API (api.github.com/gists/{id})، يستخرج كل
    ملفاته (عادة closed_trades_archive_NNNN.json)، ويرجع كل الصفقات مجمّعة.
    """
    trades = []
    url = f"{GITHUB_API_BASE}/{gist_id}"
    gist_data = fetch_json(url, headers=github_api_headers())
    if not gist_data or "files" not in gist_data:
        print(f"⚠️ تعذر جلب Gist الأرشيف: {gist_id}", file=sys.stderr)
        return trades

    files = gist_data["files"]
    for filename, file_info in files.items():
        raw_url = file_info.get("raw_url")
        if not raw_url:
            continue
        content = fetch_json(raw_url)
        if isinstance(content, list):
            trades.extend(content)
        elif isinstance(content, dict) and "trades" in content:
            trades.extend(content["trades"])

    return trades


def get_first(d: dict, keys, default=None):
    for k in keys:
        if k in d:
            return d[k]
    return default


def load_all_closed_trades():
    """يجمع صفقات closed_trades.json الرئيسي + كل ملفات الأرشيف المرتبطة."""
    all_trades = []

    main_data = fetch_json(GIST_RAW_URL)
    if main_data is None:
        print("❌ لم أستطع تحميل closed_trades.json الرئيسي. تحقق من GIST_RAW_URL.")
        sys.exit(1)

    if isinstance(main_data, list):
        all_trades.extend(main_data)
    elif isinstance(main_data, dict) and "trades" in main_data:
        all_trades.extend(main_data["trades"])
    else:
        print("⚠️ شكل closed_trades.json غير متوقع، سأحاول استخدامه كما هو إن كان قائمة.")

    # ملف سلسلة الأرشيف (اختياري): قائمة Gist IDs، كل واحد فيه عدة ملفات أرشيف
    # مثال شكل الملف: ["1ab799eca28ba222bba65e7d749016bd", "..."]
    if ARCHIVE_CHAIN_URL:
        chain = fetch_json(ARCHIVE_CHAIN_URL)
        gist_ids = []

        if isinstance(chain, list):
            for item in chain:
                if isinstance(item, str):
                    gist_ids.append(item)
                elif isinstance(item, dict):
                    # احتياطاً لو الشكل تغيّر يوماً إلى قائمة كائنات فيها id
                    for key in ("id", "gist_id"):
                        if key in item:
                            gist_ids.append(item[key])
                            break
        elif isinstance(chain, dict):
            # احتياطاً لو الشكل أصبح قاموساً (id -> معلومات) بدل قائمة
            gist_ids.extend(chain.keys())

        print(f"ℹ️ عدد Gists الأرشيف الموجودة في السلسلة: {len(gist_ids)}")

        for gist_id in gist_ids:
            archived_trades = fetch_gist_archive_trades(gist_id)
            print(f"  - Gist {gist_id}: {len(archived_trades)} صفقة")
            all_trades.extend(archived_trades)

    return all_trades


def classify_trade(trade: dict):
    """
    يعيد إحدى القيم:
      'reached_tp1'   -> وصلت إلى الهدف الأول (بغض النظر عن الإغلاق النهائي)
      'direct_sl'     -> ضربت الستوب لوس مباشرة دون الوصول لأي هدف
      'other'         -> لا هذا ولا ذاك (مثال: EXPIRED بدون أي هدف وبدون SL)
    """
    hit_tps = get_first(trade, HIT_TPS_KEYS, default=[]) or []
    close_reason = str(get_first(trade, CLOSE_REASON_KEYS, default="") or "")

    reached_tp1 = False
    if isinstance(hit_tps, list) and len(hit_tps) > 0:
        reached_tp1 = True
    elif isinstance(hit_tps, dict) and any(bool(v) for v in hit_tps.values()):
        reached_tp1 = True

    if reached_tp1:
        return "reached_tp1"

    if close_reason in SL_REASONS or close_reason.upper() == "SL":
        return "direct_sl"

    return "other"


def build_report(trades):
    total = len(trades)
    reached_tp1 = 0
    direct_sl = 0
    other = 0

    by_type = {}  # نوع الإشارة -> {reached_tp1, direct_sl, other}

    for trade in trades:
        category = classify_trade(trade)
        ttype = str(get_first(trade, TYPE_KEYS, default="غير محدد") or "غير محدد")

        by_type.setdefault(ttype, {"reached_tp1": 0, "direct_sl": 0, "other": 0})
        by_type[ttype][category] += 1

        if category == "reached_tp1":
            reached_tp1 += 1
        elif category == "direct_sl":
            direct_sl += 1
        else:
            other += 1

    return {
        "total": total,
        "reached_tp1": reached_tp1,
        "direct_sl": direct_sl,
        "other": other,
        "by_type": by_type,
    }


def pct(part, whole):
    return (part / whole * 100) if whole else 0.0


def format_report_text(stats: dict) -> str:
    total = stats["total"]
    reached = stats["reached_tp1"]
    direct_sl = stats["direct_sl"]
    other = stats["other"]

    lines = []
    lines.append("📊 تقرير: الهدف الأول مقابل الستوب لوس المباشر")
    lines.append("")
    lines.append(f"إجمالي الصفقات المغلقة: {total}")
    lines.append(
        f"✅ وصلت للهدف الأول (على الأقل) قبل أي ستوب: {reached} "
        f"({pct(reached, total):.1f}%)"
    )
    lines.append(
        f"❌ خسرت مباشرة (ضربت الستوب قبل أي هدف): {direct_sl} "
        f"({pct(direct_sl, total):.1f}%)"
    )
    if other:
        lines.append(
            f"⚪ حالات أخرى (مثال: انتهاء صلاحية بدون هدف ولا ستوب): {other} "
            f"({pct(other, total):.1f}%)"
        )

    if stats["by_type"]:
        lines.append("")
        lines.append("— تفصيل حسب نوع الإشارة —")
        for ttype, counts in stats["by_type"].items():
            sub_total = sum(counts.values())
            lines.append(
                f"• {ttype}: إجمالي {sub_total} | "
                f"✅ هدف أول {counts['reached_tp1']} ({pct(counts['reached_tp1'], sub_total):.1f}%) | "
                f"❌ ستوب مباشر {counts['direct_sl']} ({pct(counts['direct_sl'], sub_total):.1f}%)"
            )

    return "\n".join(lines)


def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ℹ️ لم يتم ضبط TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID، سيتم فقط طباعة التقرير محلياً.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        print("✅ تم إرسال التقرير على Telegram.")
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"⚠️ فشل إرسال التقرير على Telegram: {e}", file=sys.stderr)


def main():
    trades = load_all_closed_trades()
    if not trades:
        print("لا توجد صفقات مغلقة لتحليلها.")
        return

    stats = build_report(trades)
    text = format_report_text(stats)
    print(text)
    send_telegram(text)


if __name__ == "__main__":
    main()
