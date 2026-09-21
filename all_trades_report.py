#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
all_trades_report.py
--------------------
يجلب كل صفقات البوت كاملة من نفس الأماكن التي يكتب فيها scanner.py:

  • الصفقات المفتوحة حاليًا  ← open_positions.json (Gist الرئيسي GIST_ID)
  • الصفقات المغلقة (النشطة) ← closed_trades.json  (Gist الرئيسي)
  • الأرشيف الكامل           ← closed_trades_archive_NNNN.json داخل كل Gist في
                                 archive_gists_chain.json (+ archive_index.json كاحتياط)

ثم يحسب لكل نوع من الإشارات الأربع (رسمية / مبكرة / انفجار / تجريبية)
العدد الكامل للصفقات: الإجمالي، المفتوحة، المغلقة، الرابحة (TP1)، الخاسرة (SL)، المنتهية (EXPIRED).

متغيرات البيئة:
  GIST_ID, GIST_TOKEN            (مطلوبة — نفس أسرار scanner.py)
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID   (اختيارية — لإرسال الملخص إلى تيليجرام)
  ALLOW_PARTIAL=1                (اختياري — الاستمرار حتى لو فشل جلب Gist أرشيف)

الاستخدام:
  python all_trades_report.py                 # تقرير بالشاشة + ملفات all_trades.json/.csv
  python all_trades_report.py --telegram      # + إرسال الملخص لتيليجرام
  python all_trades_report.py --out reports   # مجلد حفظ الملفات
  python all_trades_report.py --list-open     # اطبع كل الصفقات المفتوحة بالتفصيل
"""

import os
import sys
import csv
import json
import time
import argparse
import datetime as dt

import requests

GIST_ID = os.environ.get("GIST_ID")
GIST_TOKEN = os.environ.get("GIST_TOKEN")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ALLOW_PARTIAL = os.environ.get("ALLOW_PARTIAL", "0") == "1"

# أسماء الملفات — مطابقة تمامًا لما في scanner.py
POSITIONS_FILE = "open_positions.json"
CLOSED_FILE = "closed_trades.json"
ARCHIVE_PREFIX = "closed_trades_archive_"
ARCHIVE_CHAIN_FILE = "archive_gists_chain.json"
ARCHIVE_INDEX_FILE = "archive_index.json"

# الأنواع الأربعة (القيمة في حقل type بالبوت) — الصفقة بلا type تُعتبر رسمية كما في scanner.py
TYPE_LABELS = {
    "official": "🟢 رسمية",
    "early": "🔵 مبكرة",
    "breakout": "💥 انفجار",
    "experimental": "🧪 تجريبية",
}
TYPE_ORDER = ["official", "early", "breakout", "experimental"]


class FetchError(Exception):
    pass


def _headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def _get(url, headers=None, tries=3):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise FetchError(f"{url} → {last}")


def fetch_gist_files(gist_id):
    """يرجع قاموس ملفات الـ Gist (اسم → بيانات الملف)."""
    return _get(f"https://api.github.com/gists/{gist_id}", headers=_headers()).json().get("files", {})


def file_content(files, name):
    """محتوى ملف نصي؛ يتعامل مع الملفات الكبيرة المقتطعة (truncated) عبر raw_url."""
    f = files.get(name)
    if not f:
        return None
    if f.get("truncated") and f.get("raw_url"):
        return _get(f["raw_url"], headers=_headers()).text
    return f.get("content")


def parse_json(text, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return default


def trade_type(t):
    return t.get("type") or "official"


def trade_key(t):
    # مفتاح فريد للتخلص من التكرار بين السجل النشط والأرشيف
    return (t.get("symbol"), trade_type(t), t.get("opened_at"), t.get("closed_at"))


def load_everything():
    """يجلب المفتوحة + المغلقة (نشط + كل الأرشيف). يرجع (open_list, closed_list, notes)."""
    if not GIST_ID or not GIST_TOKEN:
        raise SystemExit("❌ لازم تعيّن GIST_ID و GIST_TOKEN كمتغيرات بيئة.")

    notes = []
    main_files = fetch_gist_files(GIST_ID)

    open_list = parse_json(file_content(main_files, POSITIONS_FILE), [])
    active_closed = parse_json(file_content(main_files, CLOSED_FILE), [])

    # أي Gists يمكن أن تحتوي أرشيف: السلسلة + الفهرس + الـ Gist الرئيسي نفسه
    gist_ids = []
    chain = parse_json(file_content(main_files, ARCHIVE_CHAIN_FILE), [])
    index = parse_json(file_content(main_files, ARCHIVE_INDEX_FILE), {})
    for gid in list(chain) + list(index.keys()) + [GIST_ID]:
        if gid and gid not in gist_ids:
            gist_ids.append(gid)

    archived = []
    errors = []
    archive_files_count = 0
    for gid in gist_ids:
        try:
            files = main_files if gid == GIST_ID else fetch_gist_files(gid)
            names = sorted(n for n in files if n.startswith(ARCHIVE_PREFIX))
            for n in names:
                data = parse_json(file_content(files, n), None)
                if data is None:
                    raise FetchError(f"تعذّر تحليل {n} في Gist {gid}")
                archived.extend(data)
                archive_files_count += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"Gist {gid}: {e}")

    if errors:
        msg = "⚠️ فشل جلب جزء من الأرشيف:\n  - " + "\n  - ".join(errors)
        if not ALLOW_PARTIAL:
            raise SystemExit(msg + "\nالنتيجة ستكون ناقصة. أعد المحاولة أو فعّل ALLOW_PARTIAL=1 للمتابعة بنتيجة جزئية.")
        notes.append(msg + "\n(النتيجة جزئية!)")

    # دمج مع إزالة التكرار
    seen, closed = set(), []
    for t in archived + active_closed:
        k = trade_key(t)
        if k in seen:
            continue
        seen.add(k)
        closed.append(t)

    notes.append(
        f"مصادر القراءة: {len(gist_ids)} Gist | ملفات أرشيف: {archive_files_count} | "
        f"مغلقة نشطة: {len(active_closed)} | مغلقة أرشيف: {len(archived)}"
    )
    return open_list, closed, notes


def new_bucket():
    return {"total": 0, "open": 0, "closed": 0, "win": 0, "loss": 0, "expired": 0}


def compute(open_list, closed_list):
    by_type = {}
    for t in open_list:
        b = by_type.setdefault(trade_type(t), new_bucket())
        b["total"] += 1
        b["open"] += 1
    for t in closed_list:
        b = by_type.setdefault(trade_type(t), new_bucket())
        b["total"] += 1
        b["closed"] += 1
        reason = t.get("closed_reason")
        if reason == "SL":
            b["loss"] += 1
        elif reason == "EXPIRED":
            b["expired"] += 1
        else:  # TP1 (وأي TP آخر) = رابحة
            b["win"] += 1
    return by_type


def _pct(a, b):
    return f"{a / b * 100:.1f}%" if b else "—"


def build_report(open_list, closed_list, by_type, notes):
    total_open, total_closed = len(open_list), len(closed_list)
    lines = [
        "📊 تقرير كل صفقات البوت",
        f"🕒 {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"إجمالي الصفقات: {total_open + total_closed}",
        f"  • مفتوحة حاليًا: {total_open}",
        f"  • مغلقة: {total_closed}",
        "",
        "— حسب نوع الإشارة —",
    ]
    ordered = [k for k in TYPE_ORDER] + [k for k in by_type if k not in TYPE_ORDER]
    for k in ordered:
        b = by_type.get(k, new_bucket())
        label = TYPE_LABELS.get(k, f"❔ {k}")
        lines.append(
            f"{label}: {b['total']} صفقة\n"
            f"   مفتوحة {b['open']} | مغلقة {b['closed']}  "
            f"(✅ {b['win']} | ❌ {b['loss']} | ⏳ {b['expired']})  "
            f"نجاح: {_pct(b['win'], b['closed'])}"
        )
    lines.append("")
    lines.extend(notes)
    return "\n".join(lines)


def build_open_listing(open_list):
    if not open_list:
        return "لا توجد صفقات مفتوحة حاليًا."
    rows = sorted(open_list, key=lambda t: t.get("opened_at") or "")
    out = ["— الصفقات المفتوحة حاليًا —"]
    for t in rows:
        out.append(
            f"{TYPE_LABELS.get(trade_type(t), trade_type(t))} {str(t.get('symbol', '?')).replace('USDT', '/USDT')} | "
            f"دخول {t.get('entry')} | SL {t.get('sl')} | TP {t.get('tps')} | فُتحت {t.get('opened_at')}"
        )
    return "\n".join(out)


def save_outputs(out_dir, open_list, closed_list):
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    for t in open_list:
        rows.append({**t, "status": "OPEN", "type": trade_type(t)})
    for t in closed_list:
        rows.append({**t, "status": "CLOSED", "type": trade_type(t)})

    json_path = os.path.join(out_dir, "all_trades.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)

    cols = ["status", "type", "symbol", "entry", "sl", "tps", "score",
            "opened_at", "closed_at", "closed_reason", "exit_price"]
    csv_path = os.path.join(out_dir, "all_trades.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r = dict(r)
            if isinstance(r.get("tps"), list):
                r["tps"] = " / ".join(str(x) for x in r["tps"])
            w.writerow(r)
    return json_path, csv_path


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ TELEGRAM_TOKEN/TELEGRAM_CHAT_ID غير موجودين — تخطي الإرسال.")
        return
    for i in range(0, len(text), 4000):
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text[i:i + 4000]},
                timeout=20,
            ).raise_for_status()
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ فشل إرسال تيليجرام: {e}")
            return


def main():
    ap = argparse.ArgumentParser(description="تقرير كل صفقات البوت (مفتوحة + مغلقة + أرشيف)")
    ap.add_argument("--out", default=".", help="مجلد حفظ all_trades.json / all_trades.csv")
    ap.add_argument("--telegram", action="store_true", help="أرسل الملخص إلى تيليجرام")
    ap.add_argument("--list-open", action="store_true", help="اطبع كل الصفقات المفتوحة بالتفصيل")
    args = ap.parse_args()

    open_list, closed_list, notes = load_everything()
    by_type = compute(open_list, closed_list)
    report = build_report(open_list, closed_list, by_type, notes)

    print(report)
    if args.list_open:
        print()
        print(build_open_listing(open_list))

    json_path, csv_path = save_outputs(args.out, open_list, closed_list)
    print(f"\n💾 تم الحفظ: {json_path} , {csv_path}")

    if args.telegram:
        send_telegram(report)


if __name__ == "__main__":
    try:
        main()
    except FetchError as e:
        sys.exit(f"❌ {e}")
