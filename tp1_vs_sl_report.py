#!/usr/bin/env python3
"""
tp1_vs_sl_report.py
--------------------
يحسب من closed_trades.json + كامل أرشيفه (عبر سلسلة archive_gists_chain.json):

1) عدد الصفقات "الناجحة" التي وصلت إلى الهدف الأول (TP1) على الأقل قبل أي
   إغلاق بالستوب لوس (بغض النظر عمّا حدث بعد ذلك).
2) عدد الصفقات "الخاسرة المباشرة" التي أُغلقت بالستوب لوس (SL) دون أن
   تصل إلى الهدف الأول إطلاقاً.

يصنّف النتائج حسب نوع الإشارة الأربعة كلها دائمًا (حتى لو كان عددها صفراً):
  رسمية (official) / مبكرة (early) / انفجار (breakout) / تجريبية (experimental)
وأي نوع إضافي غير معروف يُعرض بعدها بقيمته الخام.

--------------------------------------------------------------------------
التعديل الأهم عن النسخة السابقة: طريقة جلب البيانات
--------------------------------------------------------------------------
النسخة القديمة كانت تعتمد على:
  - GIST_RAW_URL: رابط raw ثابت لـ closed_trades.json
  - ARCHIVE_CHAIN_URL: رابط raw ثابت لـ archive_gists_chain.json

المشكلة: روابط raw.githubusercontent.com التي تُنسخ من عرض تعديل/نسخة معينة
بالـ Gist (بها معرّف Revision طويل داخل الرابط) تبقى "مجمّدة" على تلك اللحظة
ولا تتحدث تلقائياً مع كل تعديل لاحق يجريه scanner.py على الـ Gist — فيبدو
التقرير وكأنه يفوّت صفقات جديدة أو أرشيف كامل رغم وجوده فعلياً بالـ Gist.
بالإضافة، GitHub API نفسه قد يُرجع محتوى الملف مقصوصاً (truncated) لو تجاوز
حجمه حداً معيناً، وكانت النسخة القديمة تتجاهل هذا الاحتمال فتقرأ جزءاً فقط
من ملف الأرشيف.

الحل هنا: نفس أسلوب scanner.py بالضبط — جلب كل الملفات دائماً عبر GitHub API
(api.github.com/gists/{id}) باستخدام GIST_ID + GIST_TOKEN (نفس المتغيرين
المستخدمين أصلاً في scan.yml/trade_stats.yml)، وهذا يضمن قراءة آخر نسخة من
كل ملف دائماً + معالجة صريحة لحالة truncated بجلب raw_url الكامل عند الحاجة.

رابطا GIST_RAW_URL / ARCHIVE_CHAIN_URL القديمان ما زالا مدعومين كخطة احتياطية
فقط لو لم يُضبط GIST_ID/GIST_TOKEN، حفاظاً على التوافق مع أي إعداد سابق.

متغيرات البيئة المستخدمة (بالأولوية):
  GIST_ID               معرّف الـ Gist الرئيسي (نفس القيمة المستخدمة في scanner.py)
  GIST_TOKEN             توكن GitHub بصلاحية gist (نفس القيمة المستخدمة في scanner.py)
                         — يُقبل أيضاً GITHUB_TOKEN كاسم بديل لو كان هذا هو المضبوط أصلاً
  --- احتياطي (فقط لو GIST_ID/GIST_TOKEN غير موجودين) ---
  GIST_RAW_URL           رابط raw لملف closed_trades.json الرئيسي
  ARCHIVE_CHAIN_URL      رابط raw لملف archive_gists_chain.json
  --- اختياري دائماً ---
  TELEGRAM_BOT_TOKEN     توكن بوت تيليجرام (لإرسال التقرير)
  TELEGRAM_CHAT_ID       معرف الشات (لإرسال التقرير)

الاستخدام المحلي (الطريقة الموصى بها):
  export GIST_ID="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
  export GIST_TOKEN="ghp_xxxxxxxxxxxxxxxxxxxx"
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
GIST_ID = os.environ.get("GIST_ID", "").strip()
GIST_TOKEN = (os.environ.get("GIST_TOKEN", "").strip()
              or os.environ.get("GITHUB_TOKEN", "").strip())

# احتياطي قديم (خطة بديلة فقط)
GIST_RAW_URL = os.environ.get("GIST_RAW_URL", "").strip()
ARCHIVE_CHAIN_URL = os.environ.get("ARCHIVE_CHAIN_URL", "").strip()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

GITHUB_API_BASE = "https://api.github.com/gists"

# --- نفس أسماء الملفات/الثوابت المستخدمة في scanner.py (لتفادي أي عدم توافق) ---
CLOSED_GIST_FILE = "closed_trades.json"
ARCHIVE_CHAIN_FILE = "archive_gists_chain.json"
ARCHIVE_PREFIX = "closed_trades_archive_"

# أسماء بديلة محتملة للحقول (لتفادي فروقات بسيطة بين نسخ scanner.py المختلفة)
HIT_TPS_KEYS = ["hit_tps", "hit_targets", "tps_hit", "targets_hit"]
CLOSE_REASON_KEYS = ["close_reason", "reason", "closed_reason"]
TYPE_KEYS = ["type", "signal_type", "trade_type"]

SL_REASONS = {"SL", "sl", "stop_loss", "STOP_LOSS"}

# ترتيب وتسمية الأنواع الأربعة المعروفة في scanner.py — تُعرض دائماً بهذا
# الترتيب في التقرير حتى لو كان عددها صفراً، حتى يتضح أن الانفجار/التجريبية
# مشمولان فعلاً بالحساب وليسا مهملين
TYPE_LABELS = {
    "official": "🟢 رسمية",
    "early": "🔵 مبكرة",
    "breakout": "🟠 انفجار",
    "experimental": "🟣 تجريبية",
}
KNOWN_TYPE_ORDER = ["official", "early", "breakout", "experimental"]


# ---------------------------------------------------------------------------
# أدوات جلب البيانات
# ---------------------------------------------------------------------------
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


def fetch_text(url: str, headers: dict = None):
    """مثل fetch_json لكن يرجع نصاً خاماً بدون محاولة تحليل JSON (يُستخدم لملفات raw_url)."""
    if not url:
        return None
    req_headers = {"User-Agent": "tp1-vs-sl-report"}
    if headers:
        req_headers.update(headers)
    try:
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8")
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"⚠️ تعذر جلب: {url} -> {e}", file=sys.stderr)
        return None


def github_api_headers():
    headers = {"Accept": "application/vnd.github+json"}
    if GIST_TOKEN:
        headers["Authorization"] = f"Bearer {GIST_TOKEN}"
    return headers


def fetch_gist_files(gist_id: str):
    """
    يجلب كل ملفات Gist معيّن دفعة واحدة عبر GitHub API (دائماً آخر نسخة، بلا
    أي تجميد على revision قديمة كما كان يحدث مع روابط raw الثابتة).
    يعيد dict: اسم الملف -> معلومات الملف (تتضمن content و raw_url و truncated).
    """
    url = f"{GITHUB_API_BASE}/{gist_id}"
    data = fetch_json(url, headers=github_api_headers())
    if not data or "files" not in data:
        print(f"⚠️ تعذر جلب ملفات Gist: {gist_id}", file=sys.stderr)
        return {}
    return data["files"]


def get_full_file_content(file_info: dict):
    """
    يرجع محتوى الملف كاملاً كنص. GitHub API يقصّ (truncated=True) محتوى
    الملفات الكبيرة نسبياً ضمن استجابة gists/{id} — في هذه الحالة يجب جلب
    raw_url للحصول على المحتوى الكامل بدل الاكتفاء بالجزء المقصوص (كانت هذه
    نقطة ضعف صامتة في النسخة القديمة يمكن أن تُفقد بها صفقات من الأرشيف).
    """
    if not file_info:
        return None
    if file_info.get("truncated"):
        return fetch_text(file_info.get("raw_url"))
    return file_info.get("content")


def extract_trades_from_content(raw_content):
    """يحوّل محتوى ملف (نص JSON) إلى قائمة صفقات، بصرف النظر عن الشكل
    (قائمة مباشرة أو {"trades": [...]})."""
    if not raw_content:
        return []
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and isinstance(parsed.get("trades"), list):
        return parsed["trades"]
    return []


# ---------------------------------------------------------------------------
# تجميع كل الصفقات المغلقة (الرئيسي + كامل سلسلة الأرشيف)
# ---------------------------------------------------------------------------
def load_all_closed_trades_via_api():
    """المسار الموصى به: GIST_ID + GIST_TOKEN، بنفس أسلوب scanner.py تماماً."""
    all_trades = []

    main_files = fetch_gist_files(GIST_ID)
    if not main_files:
        print("❌ لم أستطع تحميل ملفات الـ Gist الرئيسي. تحقق من GIST_ID/GIST_TOKEN.")
        sys.exit(1)

    main_trades = extract_trades_from_content(
        get_full_file_content(main_files.get(CLOSED_GIST_FILE))
    )
    all_trades.extend(main_trades)
    print(f"ℹ️ صفقات السجل النشط (closed_trades.json): {len(main_trades)}")

    chain_raw = get_full_file_content(main_files.get(ARCHIVE_CHAIN_FILE))
    try:
        chain = json.loads(chain_raw) if chain_raw else []
    except json.JSONDecodeError:
        chain = []
    if not isinstance(chain, list):
        chain = []

    print(f"ℹ️ عدد Gists الأرشيف الموجودة في السلسلة: {len(chain)}")

    for gist_id in chain:
        # حالة نظرية دفاعية: لو كان الأرشيف النشط هو نفس الـ Gist الرئيسي
        # فلا داعي لجلبه مرة ثانية عبر طلب شبكة إضافي
        files = main_files if gist_id == GIST_ID else fetch_gist_files(gist_id)
        if not files:
            continue

        archive_trade_count = 0
        for filename, file_info in files.items():
            if not filename.startswith(ARCHIVE_PREFIX):
                continue  # تجاهل README.json وأي ملف آخر غير ملفات الأرشيف الفعلية
            trades = extract_trades_from_content(get_full_file_content(file_info))
            all_trades.extend(trades)
            archive_trade_count += len(trades)
        print(f"  - Gist {gist_id}: {archive_trade_count} صفقة")

    return all_trades


def load_all_closed_trades_legacy():
    """المسار الاحتياطي القديم (روابط raw ثابتة) — يُستخدم فقط لو GIST_ID/GIST_TOKEN غير مضبوطين."""
    all_trades = []

    main_data = fetch_json(GIST_RAW_URL)
    if main_data is None:
        print("❌ لم أستطع تحميل closed_trades.json الرئيسي. تحقق من GIST_RAW_URL.")
        sys.exit(1)

    if isinstance(main_data, list):
        all_trades.extend(main_data)
    elif isinstance(main_data, dict) and isinstance(main_data.get("trades"), list):
        all_trades.extend(main_data["trades"])

    if ARCHIVE_CHAIN_URL:
        chain = fetch_json(ARCHIVE_CHAIN_URL)
        gist_ids = chain if isinstance(chain, list) else []
        print(f"ℹ️ عدد Gists الأرشيف الموجودة في السلسلة: {len(gist_ids)}")

        for gist_id in gist_ids:
            files = fetch_gist_files(gist_id)
            archive_trade_count = 0
            for filename, file_info in files.items():
                if not filename.startswith(ARCHIVE_PREFIX):
                    continue
                trades = extract_trades_from_content(get_full_file_content(file_info))
                all_trades.extend(trades)
                archive_trade_count += len(trades)
            print(f"  - Gist {gist_id}: {archive_trade_count} صفقة")

    return all_trades


def load_all_closed_trades():
    if GIST_ID and GIST_TOKEN:
        return load_all_closed_trades_via_api()

    print("⚠️ GIST_ID/GIST_TOKEN غير مضبوطين، سيُستخدم المسار الاحتياطي عبر "
          "GIST_RAW_URL/ARCHIVE_CHAIN_URL (أقل موثوقية: قد يتجمّد على نسخة قديمة "
          "لو كان الرابط يحوي معرّف revision).")
    if not GIST_RAW_URL:
        print("❌ لا يوجد GIST_ID/GIST_TOKEN ولا GIST_RAW_URL. لا يمكن المتابعة.")
        sys.exit(1)
    return load_all_closed_trades_legacy()


# ---------------------------------------------------------------------------
# التصنيف والتقرير
# ---------------------------------------------------------------------------
def get_first(d: dict, keys, default=None):
    for k in keys:
        if k in d:
            return d[k]
    return default


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

    # نبدأ دائماً بالأنواع الأربعة المعروفة (حتى لو صفر) ثم أي نوع إضافي يظهر لاحقاً
    by_type = {t: {"reached_tp1": 0, "direct_sl": 0, "other": 0} for t in KNOWN_TYPE_ORDER}

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

    by_type = stats["by_type"]
    if by_type:
        lines.append("")
        lines.append("— تفصيل حسب نوع الإشارة —")

        # الأنواع الأربعة المعروفة أولاً بترتيب ثابت (رسمية/مبكرة/انفجار/تجريبية)،
        # حتى لو عددها صفر — حتى يتضح أن الانفجار والتجريبية مُحتسبان فعلاً
        ordered_types = list(KNOWN_TYPE_ORDER)
        extra_types = sorted(t for t in by_type if t not in KNOWN_TYPE_ORDER)
        ordered_types.extend(extra_types)

        for ttype in ordered_types:
            counts = by_type.get(ttype, {"reached_tp1": 0, "direct_sl": 0, "other": 0})
            sub_total = sum(counts.values())
            label = TYPE_LABELS.get(ttype, ttype)
            lines.append(
                f"• {label}: إجمالي {sub_total} | "
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
