#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_app_data.py — يجمع نتائج سكربتات التقارير في ملف واحد app_reports.json داخل الـ Gist الرئيسي،
ليقرأه تطبيق Android ويعرضه. للقراءة من السكربتات فقط: لا يعدّل أي ملف آخر في الـ Gist.

يستدعي دوال السكربتات الموجودة مباشرة (نفس الأرقام تمامًا، بلا إعادة كتابة منطق الحساب):
  trade_stats.py          -> قسم "trade_stats" (تقرير موحّد لكل نوع + التفصيل + الخلاصة)
  market_regime_report.py -> قسم "market_regime" (نظام السوق × نوع الإشارة)
  trade_simulator.py      -> قسم "simulator" (إعدادات جاهزة: 7/10/30 يوم + منذ بداية السجل)
  paper_stats.py          -> قسم "paper" (مقارنة المحافظ الخمس + التفاصيل)

ضمانات السلامة:
  - الأرشيف الكامل دائمًا (السجل النشط + كل Gists الأرشيف عبر archive_gists_chain.json)؛
    أي فشل بجلب جزء منه يوقف ذلك القسم بدل نشر أرقام ناقصة (كما تفعل السكربتات الأصلية).
  - كل قسم معزول: فشل قسم لا يمنع بقية الأقسام. القسم الفاشل يُبقي آخر نسخة ناجحة موسومة stale
    مع سبب الفشل، فلا يرى التطبيق صفحة فارغة.
  - لا يكتب شيئًا إذا لم تتغير النتائج (إلا نبضة تحديث كل APP_EXPORT_HEARTBEAT ثانية).
  - لا يكتب إلا ملف app_reports.json، فلا يتعارض مع كتابات scanner.py.

المتغيرات (Secrets): GIST_TOKEN, GIST_ID  (+ اختياري PAPER_GIST_ID, PAPER_GIST_TOKEN)
اختياري: APP_SIM_CAPITAL (400)، APP_SIM_SINCE ('YYYY-MM-DD HH:MM:SS' لحظة Reset)،
         APP_EXPORT_HEARTBEAT (600)

الاستخدام:
    python export_app_data.py
    python export_app_data.py --dry-run     # يبني ويطبع الحجم بدون كتابة
"""

import os
import sys
import json
import time
import datetime as dt

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")
APP_FILE = "app_reports.json"
SCHEMA = 1
HEARTBEAT_SECONDS = int(os.environ.get("APP_EXPORT_HEARTBEAT", "600"))
SIM_CAPITAL = float(os.environ.get("APP_SIM_CAPITAL", "400"))
SIM_SINCE = os.environ.get("APP_SIM_SINCE", "").strip()
SIM_DAYS = (7, 10, 30)

# متغير بيئة فارغ (Secret غير معرّف في workflow) يجب ألا يطغى على القيمة الافتراضية داخل paper_stats
for _k in ("PAPER_GIST_ID", "PAPER_GIST_TOKEN"):
    if not os.environ.get(_k):
        os.environ.pop(_k, None)


# ---------------------------------------------------------------- أدوات عامة
def _headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s):
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def _num(v, default=0.0):
    try:
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return default
        return f
    except (TypeError, ValueError):
        return default


def read_previous():
    """يقرأ app_reports.json السابق. فشل الشبكة يرفع استثناءً (لا نكتب فوق شيء لا نراه)."""
    r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=_headers(), timeout=20)
    r.raise_for_status()
    f = (r.json().get("files") or {}).get(APP_FILE)
    if not f:
        return {}
    content = f.get("content")
    if f.get("truncated") and f.get("raw_url"):
        rr = requests.get(f["raw_url"], timeout=30)
        rr.raise_for_status()
        content = rr.text
    try:
        return json.loads(content) if content else {}
    except Exception:
        return {}


def write_file(text):
    """PATCH لملف واحد فقط مع إعادة محاولة (409/422/5xx/شبكة)."""
    body = {"files": {APP_FILE: {"content": text}}}
    last = None
    for attempt in range(1, 5):
        try:
            r = requests.patch(f"https://api.github.com/gists/{GIST_ID}", headers=_headers(),
                               data=json.dumps(body), timeout=30)
            if r.status_code == 200:
                return True
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code in (401, 403, 404):
                break
        except requests.RequestException as e:
            last = str(e)
        time.sleep(2 ** attempt)
    raise RuntimeError(f"فشل كتابة {APP_FILE}: {last}")


# ---------------------------------------------------------------- لقطة الصفقات (مرة واحدة)
_cache = {}


def _stats_snapshot():
    """السجل النشط + كل الأرشيف (نفس load_closed_trades في trade_stats.py)."""
    if "err" in _cache:
        raise RuntimeError(_cache["err"])
    if "snap" not in _cache:
        try:
            import trade_stats
            trades, notes = trade_stats.load_closed_trades()
            open_positions = trade_stats.load_open_positions()
            _cache["snap"] = (trades, notes, open_positions)
        except BaseException as e:
            _cache["err"] = _err_text(e)
            raise
    return _cache["snap"]


def _err_text(e):
    text = str(e).strip()
    if isinstance(e, SystemExit):
        text = text or "توقّف السكربت (SystemExit)"
    return (text or e.__class__.__name__)[:400]


# ---------------------------------------------------------------- الأقسام
def build_trade_stats():
    import trade_stats
    trades, notes, open_positions = _stats_snapshot()
    messages, _per_trade = trade_stats.build_messages(trades, open_count=len(open_positions))
    return {"messages": messages, "notes": notes, "trades_count": len(trades)}


def build_market_regime():
    import market_regime_report as mrr
    trades, _notes, _open = _stats_snapshot()
    eligible = [t for t in trades if t.get("market_regime") in mrr.REGIME_LABELS]

    head = [f"📊 حسب نظام السوق — كل الأنواع مجتمعة",
            f"{len(eligible)} صفقة تملك market_regime من أصل {len(trades)}"]
    for regime, label in mrr.REGIME_LABELS.items():
        subset = [t for t in eligible if t.get("market_regime") == regime]
        head.append(f"{label}: {mrr.format_summary(mrr.summarize(subset))}")
    messages = ["\n".join(head)]

    for ttype, tlabel in mrr.TYPE_LABELS.items():
        type_trades = [t for t in eligible if t.get("type") == ttype]
        lines = [f"{tlabel} — إجمالي {len(type_trades)} صفقة مؤهلة"]
        for regime, rlabel in mrr.REGIME_LABELS.items():
            subset = [t for t in type_trades if t.get("market_regime") == regime]
            lines.append(f"{rlabel}: {mrr.format_summary(mrr.summarize(subset))}")
        messages.append("\n".join(lines))

    messages.append("ملاحظة: PF والصافي محسوبان من النسب المئوية للصفقة (وليس من مبالغ $) — تقريبي.")
    return {"messages": messages, "eligible": len(eligible)}


def _pack_preset(key, label, text, res):
    by_type = {}
    for k, v in (res.get("by_type") or {}).items():
        by_type[k] = {"count": v["count"], "profit": round(v["profit"], 2),
                      "wins": v["wins"], "losses": v["losses"]}
    return {
        "key": key, "label": label, "text": text,
        "res": {
            "n": res["n"], "skipped": res["skipped"], "wins": res["wins"], "losses": res["losses"],
            "win_rate": res["win_rate"], "capital": res["capital"], "slot_amount": res["slot_amount"],
            "max_concurrent": res["max_concurrent"], "total_profit": res["total_profit"],
            "final_balance": res["final_balance"], "by_type": by_type,
        },
    }


def build_simulator():
    import trade_simulator as ts
    trades, archives_found, fetch_error = ts.fetch_all_trades()
    if fetch_error:
        raise RuntimeError("تعذّر جلب جزء من الأرشيف — لن تُنشر محاكاة ناقصة")

    presets = []
    for d in SIM_DAYS:
        res = ts.simulate(trades, d, SIM_CAPITAL, ts.MAX_CONCURRENT, "all", False, "tp1",
                          debug=False, window_by="closed", since=None, since_mode="open")
        presets.append(_pack_preset(f"d{d}", f"{d} أيام", ts.format_message(d, res, archives_found), res))

    since, label = None, "منذ بداية السجل"
    if SIM_SINCE:
        try:
            since, label = ts.parse_dt(SIM_SINCE), "منذ آخر Reset"
        except Exception:
            since = None
    if since is None:
        firsts = []
        for t in trades:
            try:
                firsts.append(ts.parse_dt(t["opened_at"]))
            except Exception:
                pass
        since = min(firsts) if firsts else None
    if since is not None:
        res = ts.simulate(trades, 10, SIM_CAPITAL, ts.MAX_CONCURRENT, "all", False, "tp1",
                          debug=False, window_by="closed", since=since, since_mode="open")
        presets.append(_pack_preset("since", label, ts.format_message(10, res, archives_found), res))

    return {"presets": presets, "capital": SIM_CAPITAL, "archives_found": archives_found}


def build_paper():
    import paper_stats as ps
    trades, notes, balance, positions, sub_state = ps.load_all()
    messages = ps.build_messages(trades, notes, balance, positions, None, sub_state, None)

    valid = ps.prep(trades, None)
    by_pf = {k: [t for t in valid if t["_pf"] == k] for k in ps.PORTFOLIO_KEYS}
    compare = []
    for key, label in ps.PORTFOLIOS:
        r = ps.analyze(by_pf[key])
        if key == "main":
            bal = balance or {}
        else:
            bal = ((sub_state or {}).get(key[4:]) or {}).get("balance") or {}
        cap = ps._num(bal.get("initial_capital")) or ps.DEFAULT_CAPITAL
        compare.append({
            "key": key, "label": label, "n": int(r.get("n", 0)),
            "wr": _num(r.get("wr")), "mean": _num(r.get("mean")),
            "total_usd": _num(r.get("total_usd")), "cap": _num(cap),
        })

    bal = balance or {}
    return {
        "messages": messages, "compare": compare,
        "balance": {"available": _num(bal.get("available")),
                    "initial_capital": _num(bal.get("initial_capital")),
                    "realized_pnl_usd": _num(bal.get("realized_pnl_usd")),
                    "open_count": len(positions or [])},
    }


def build_market_now():
    """وضع السوق الحالي (BTCUSDT) بنفس منطق scanner.py، ليعرضه التطبيق في خانة الأداء."""
    import scanner
    regime = scanner.fetch_market_regime()
    return {"regime": regime or "unknown", "interval": scanner.INTERVAL}


BUILDERS = [
    ("trade_stats", build_trade_stats),
    ("market_regime", build_market_regime),
    ("simulator", build_simulator),
    ("paper", build_paper),
    ("market_now", build_market_now),
]


# ---------------------------------------------------------------- التشغيل
def _strip(sections):
    return {k: {kk: vv for kk, vv in (v or {}).items() if kk != "updated_at"} for k, v in sections.items()}


def main():
    dry = "--dry-run" in sys.argv
    if not GIST_TOKEN or not GIST_ID:
        sys.exit("❌ GIST_TOKEN أو GIST_ID غير موجودين.")

    t0 = time.time()
    prev = read_previous()
    prev_sections = prev.get("sections") or {}
    stamp = _iso(_now())

    sections, failed = {}, 0
    for name, fn in BUILDERS:
        try:
            data = fn()
            data.update({"ok": True, "stale": False, "error": "", "updated_at": stamp})
            sections[name] = data
            print(f"✅ {name}")
        except KeyboardInterrupt:
            raise
        except BaseException as e:   # يشمل SystemExit الصادر من السكربتات عند الفشل
            failed += 1
            msg = _err_text(e)
            print(f"⚠️ {name}: {msg}")
            old = prev_sections.get(name)
            if isinstance(old, dict) and old.get("ok"):
                old = dict(old)
                old.update({"stale": True, "error": msg})
                sections[name] = old
            else:
                sections[name] = {"ok": False, "stale": False, "error": msg, "updated_at": ""}

    if failed == len(BUILDERS):
        sys.exit("❌ فشلت كل الأقسام — لم يُكتب شيء.")

    new_core = json.dumps(_strip(sections), sort_keys=True, ensure_ascii=False)
    old_core = json.dumps(_strip(prev_sections), sort_keys=True, ensure_ascii=False)
    prev_t = _parse_iso(prev.get("generated_at", ""))
    age = (_now() - prev_t).total_seconds() if prev_t else None
    if new_core == old_core and age is not None and age < HEARTBEAT_SECONDS:
        print(f"⏭️ لا تغيير (عمر النسخة الحالية {age:.0f}ث) — تخطّي الكتابة.")
        return

    payload = {"schema": SCHEMA, "generated_at": stamp,
               "duration_s": round(time.time() - t0, 1), "sections": sections}
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    print(f"📦 حجم {APP_FILE}: {len(text):,} بايت | أقسام فاشلة: {failed}")
    if dry:
        print("(dry-run) لم يُكتب شيء.")
        return
    write_file(text)
    print("💾 تم النشر.")


if __name__ == "__main__":
    main()
