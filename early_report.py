"""
تقرير الإشارات المبكرة فقط — early_report.py
=================================================
سكربت مستقل تمامًا عن trade_stats.py، يقرأ نفس الـGist (بنفس أسرار GIST_TOKEN/
GIST_ID الموجودة أصلاً — بدون أي أسرار جديدة) ويُخرج تقريرًا مفصّلًا خاصًا حصرًا
بصفقات type="early"، دون أي خلط مع الرسمية أو الانفجار.

ما يعرضه التقرير:
  1) العدد الكلي للصفقات المبكرة المكتملة (نجاح/خسارة) + عدد الصفقات المفتوحة
     (لسه ما اتقفلت).
  2) تفصيل حسب تركيبة المؤشرات الأساسية الفعلية (squeeze/accumulation/
     divergence/momentum) المتحققة بكل صفقة: مؤشر واحد لوحده، مؤشرين سوا،
     ثلاثة، أو حتى الأربعة — كل تركيبة فئة مستقلة.
  3) كل تركيبة تُقسَم بدورها حسب حضور الفيبوناتشي (fib_extension_used) أو غيابه،
     كإضافة فوق التركيبة الأساسية (مو بديل عنها).
  4) أي فئة (تركيبة ± فيبوناتشي) عدد صفقاتها أقل من MIN_SAMPLE_SIZE (افتراضيًا 10)
     تُجمع تلقائيًا تحت "تركيبات أخرى (عينات صغيرة)" بدل ما تشوّش التقرير بأرقام
     غير موثوقة إحصائيًا — وتخرج من هالسلة لوحدها أوتوماتيكيًا لما تكبر عينتها
     بمرور الوقت، بدون أي تعديل إضافي على هذا السكربت.
  5) الصفقات القديمة (قبل إضافة حقل "factors" لسجل الصفقة) تُعزل بفئة خاصة
     "بيانات قديمة (بدون تفصيل العوامل)" بدل حذفها أو خلطها بالتحليل الحديث.

هذا السكربت للقراءة والتحليل فقط — لا يعدّل أي بيانات بالـGist ولا يؤثر على
منطق scanner.py بأي شكل.
"""

import os
import json
import requests

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")

CLOSED_GIST_FILE = "closed_trades.json"
POSITIONS_GIST_FILE = "open_positions.json"
ARCHIVE_PREFIX = "closed_trades_archive_"

MIN_SAMPLE_SIZE = int(os.environ.get("MIN_SAMPLE_SIZE", "10"))

CORE_FACTORS = ("accumulation", "squeeze", "divergence", "momentum")

FACTOR_LABELS = {
    "accumulation": "تراكم صامت",
    "squeeze": "انضغاط تقلب",
    "divergence": "انحراف صعودي",
    "momentum": "زخم",
}


# ---------------- جلب البيانات من الـGist (قراءة فقط) ----------------

def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def _fetch_gist_meta():
    if not GIST_TOKEN or not GIST_ID:
        raise SystemExit("❌ GIST_TOKEN أو GIST_ID غير موجودين بمتغيرات البيئة.")
    r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=_gist_headers(), timeout=20)
    r.raise_for_status()
    return r.json().get("files", {})


def _read_file_content(file_meta):
    """يرجع محتوى الملف كنص، ويجلب raw_url لو كان المحتوى مقطوعًا (truncated) بردّ الـAPI."""
    if not file_meta:
        return None
    if file_meta.get("truncated"):
        r = requests.get(file_meta["raw_url"], timeout=30)
        r.raise_for_status()
        return r.text
    return file_meta.get("content")


def load_all_closed_trades():
    """يجمع السجل النشط + كل ملفات الأرشيف المرقّمة سوا (نفس آلية الأرشفة في scanner.py)."""
    files = _fetch_gist_meta()
    all_trades = []

    active_content = _read_file_content(files.get(CLOSED_GIST_FILE))
    if active_content:
        try:
            all_trades.extend(json.loads(active_content))
        except Exception:
            pass

    archive_keys = sorted(k for k in files if k.startswith(ARCHIVE_PREFIX))
    for key in archive_keys:
        content = _read_file_content(files[key])
        if content:
            try:
                all_trades.extend(json.loads(content))
            except Exception:
                pass

    open_content = _read_file_content(files.get(POSITIONS_GIST_FILE))
    open_positions = []
    if open_content:
        try:
            open_positions = json.loads(open_content)
        except Exception:
            pass

    return all_trades, open_positions


# ---------------- تصنيف نجاح/خسارة (نفس منطق compute_stats بscanner.py) ----------------

def classify_outcome(t):
    reason = t.get("closed_reason", "UNKNOWN")
    hit = len(t.get("hit_tps") or [])
    if reason == "ALL_TP" or hit > 0:
        return "win"
    if reason == "SL" and hit == 0:
        return "loss"
    return "neutral"


def combo_key(trade):
    """يبني مفتاح التركيبة من العوامل الأساسية الأربعة فقط (بدون فيبوناتشي)، مرتبة أبجديًا."""
    factors = trade.get("factors")
    if factors is None:
        return None  # صفقة قديمة بدون حقل factors إطلاقًا
    core = sorted(f for f in factors if f in CORE_FACTORS)
    if not core:
        return None
    return "+".join(core)


def combo_label(key):
    parts = key.split("+")
    return " + ".join(FACTOR_LABELS.get(p, p) for p in parts)


# ---------------- بناء التقرير ----------------

def build_report():
    all_trades, open_positions = load_all_closed_trades()

    early_closed = [t for t in all_trades if t.get("type") == "early"]
    early_open = [p for p in open_positions if p.get("type") == "early"]

    total = len(early_closed)
    wins = sum(1 for t in early_closed if classify_outcome(t) == "win")
    losses = sum(1 for t in early_closed if classify_outcome(t) == "loss")

    lines = []
    lines.append("📊 تقرير الإشارات المبكرة فقط (early)")
    lines.append("=" * 40)
    lines.append(f"الصفقات المكتملة: {total} | ✅ رابحة: {wins} ({wins/total*100:.1f}%)"
                 f" | ❌ خاسرة: {losses} ({losses/total*100:.1f}%)" if total else
                 "لا توجد صفقات مبكرة مكتملة بعد.")
    lines.append(f"الصفقات المفتوحة حاليًا (لسه ما اتقفلت): {len(early_open)}")
    lines.append("")

    # --- تجميع حسب التركيبة الأساسية + الفيبوناتشي ---
    groups = {}   # key: (combo_key أو None, fib: bool) -> {"total":.., "win":.., "loss":..}
    legacy_count = {"total": 0, "win": 0, "loss": 0}

    for t in early_closed:
        outcome = classify_outcome(t)
        key = combo_key(t)
        if key is None:
            legacy_count["total"] += 1
            if outcome == "win":
                legacy_count["win"] += 1
            elif outcome == "loss":
                legacy_count["loss"] += 1
            continue
        fib = bool(t.get("fib_extension_used"))
        gkey = (key, fib)
        g = groups.setdefault(gkey, {"total": 0, "win": 0, "loss": 0})
        g["total"] += 1
        if outcome == "win":
            g["win"] += 1
        elif outcome == "loss":
            g["loss"] += 1

    # فصل الفئات الكبيرة (>= MIN_SAMPLE_SIZE) عن الصغيرة
    big_groups = {k: v for k, v in groups.items() if v["total"] >= MIN_SAMPLE_SIZE}
    small_groups = {k: v for k, v in groups.items() if v["total"] < MIN_SAMPLE_SIZE}

    def fmt_row(label, stats):
        t, w, l = stats["total"], stats["win"], stats["loss"]
        wr = f"{w/t*100:.1f}%" if t else "—"
        return f"  • {label}: {t} صفقة | نجاح {wr} (رابحة {w} / خاسرة {l})"

    lines.append(f"— تفصيل حسب تركيبة المؤشرات (فئات بعينة ≥ {MIN_SAMPLE_SIZE} صفقة) —")
    if big_groups:
        # ترتيب: عدد المؤشرات بالتركيبة أولًا، بعدين حسب عدد الصفقات تنازليًا
        for (key, fib), stats in sorted(
            big_groups.items(),
            key=lambda kv: (kv[0][0].count("+"), -kv[1]["total"])
        ):
            label = combo_label(key) + (" + فيبوناتشي" if fib else "")
            lines.append(fmt_row(label, stats))
    else:
        lines.append("  (لا توجد فئات وصلت للحد الأدنى من العينة بعد)")

    if small_groups:
        merged = {"total": 0, "win": 0, "loss": 0}
        for stats in small_groups.values():
            merged["total"] += stats["total"]
            merged["win"] += stats["win"]
            merged["loss"] += stats["loss"]
        lines.append("")
        lines.append(fmt_row(f"تركيبات أخرى (عينات صغيرة، أقل من {MIN_SAMPLE_SIZE} لكل فئة"
                              f" — {len(small_groups)} فئة مختلفة)", merged))
        # تفصيل تشخيصي مختصر بالـlogs فقط، لمعرفة أي تركيبات تحديدًا داخل السلة
        lines.append("    تفاصيل الفئات الصغيرة (للمراجعة فقط، عينة غير كافية للحكم):")
        for (key, fib), stats in sorted(small_groups.items(), key=lambda kv: -kv[1]["total"]):
            label = combo_label(key) + (" + فيبوناتشي" if fib else "")
            lines.append(f"      - {label}: {stats['total']} صفقة")

    if legacy_count["total"]:
        lines.append("")
        lines.append("— بيانات قديمة (صفقات بدون حقل factors، قبل هذا التتبع) —")
        lines.append(fmt_row("غير مصنّفة", legacy_count))

    return "\n".join(lines)


def main():
    report = build_report()
    print(report)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("# 📊 تقرير الإشارات المبكرة فقط\n\n```\n" + report + "\n```\n")


if __name__ == "__main__":
    main()
