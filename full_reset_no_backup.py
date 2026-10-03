#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
إعادة ضبط شاملة (بدون نسخة احتياطية) - مخصص للتشغيل من GitHub Actions.

1) بوت السكانر: يفرّغ كل شيء بالـ Gist الرئيسي (بما فيه سلسلة/فهرس أرشيف الـGists المنفصلة)
   ويقفل كل الصفقات المفتوحة بدون استثناء.
2) بوت التداول الوهمي (paper_trading): يصفّر Gist المحفظة الوهمية بالكامل:
   المحفظة الرئيسية + المحافظ الفرعية الأربع + الصفقات المفتوحة + السجل النشط
   + كل الأرشيف (داخل الـGist وفي الـGists المنفصلة) — ويبدأ من رأس المال الأصلي.

متغيرات البيئة:
- GIST_TOKEN / GIST_ID                 : Gist السكانر (إلزامية)
- PAPER_GIST_TOKEN (أو GIST_TOKEN)     : توكن Gist المحفظة الوهمية
- PAPER_GIST_ID                        : معرّف Gist المحفظة الوهمية (نفس قيمة paper_trading.py افتراضيًا)
- PAPER_CAPITAL / PAPER_SUB_CAPITAL    : رأس المال (افتراضيًا 400 لكل منهما) — يجب أن تطابق
                                          القيم المستخدمة في تشغيل السكانر
"""

import os
import sys
import json
import time
import requests

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")

GIST_FILENAME = "alerted_state.json"
POSITIONS_GIST_FILE = "open_positions.json"
CLOSED_GIST_FILE = "closed_trades.json"
STATS_GIST_FILE = "stats.json"
ARCHIVE_PREFIX = "closed_trades_archive_"
ARCHIVE_CHAIN_FILE = "archive_gists_chain.json"   # لائحة معرّفات Gists الأرشيف
ARCHIVE_INDEX_FILE = "archive_index.json"          # فهرس تواريخ كل Gist أرشيف

# ---------------- المحفظة الوهمية (نفس الأسماء والقيم الافتراضية في paper_trading.py) ----------------
PAPER_GIST_TOKEN = os.environ.get("PAPER_GIST_TOKEN") or GIST_TOKEN
PAPER_GIST_ID = os.environ.get("PAPER_GIST_ID", "af82b35a4fde92f671d596bc6c18f4f2")
PAPER_CAPITAL = float(os.environ.get("PAPER_CAPITAL", "400"))
SUB_CAPITAL = float(os.environ.get("PAPER_SUB_CAPITAL", "400"))
SIGNAL_TYPES = ("official", "early", "breakout", "experimental")

PAPER_BALANCE_FILE = "paper_balance.json"
PAPER_POSITIONS_FILE = "paper_positions.json"
PAPER_CLOSED_FILE = "paper_closed_trades.json"
PAPER_SUB_STATE_FILE = "paper_sub_portfolios.json"
PAPER_ARCHIVE_PREFIX = "paper_closed_trades_archive_"
PAPER_ARCHIVE_CHAIN_FILE = "paper_archive_gists_chain.json"
PAPER_ARCHIVE_INDEX_FILE = "paper_archive_index.json"

API_URL = f"https://api.github.com/gists/{GIST_ID}"


def _headers(token=None):
    return {
        "Authorization": f"token {token or GIST_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


def _new_balance(capital):
    return {
        "initial_capital": capital,
        "available": capital,
        "realized_pnl_usd": 0.0,
        "wins": 0,
        "losses": 0,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _safe_json(text, default):
    try:
        return json.loads(text) if text else default
    except json.JSONDecodeError:
        return default


def _delete_gists(gist_ids, token, protected):
    """حذف نهائي لـ Gists الأرشيف المنفصلة (لا يلمس أبدًا الـGists المحمية)."""
    failed = 0
    for gid in gist_ids:
        if gid in protected:
            continue
        resp = requests.delete(f"https://api.github.com/gists/{gid}", headers=_headers(token), timeout=20)
        if resp.status_code == 204:
            print(f"🗑️ تم حذف Gist الأرشيف المنفصل: {gid}")
        else:
            failed += 1
            print(f"⚠️ تعذّر حذف Gist الأرشيف {gid}: {resp.status_code} - {resp.text[:200]}")
    return failed


def reset_scanner():
    """تصفير بوت السكانر. يرجع True عند النجاح."""
    r = requests.get(API_URL, headers=_headers(), timeout=20)
    r.raise_for_status()
    gist_files = r.json().get("files", {})

    open_positions = _safe_json(gist_files.get(POSITIONS_GIST_FILE, {}).get("content"), [])

    # قراءة سلسلة الأرشيف قبل تصفيرها (باش نعرفو شحال من Gist أرشيف منفصل كاين)
    chain = _safe_json(gist_files.get(ARCHIVE_CHAIN_FILE, {}).get("content"), [])
    archive_gist_ids = [gid for gid in chain if gid not in (GIST_ID, PAPER_GIST_ID)]

    archive_files = sorted(fn for fn in gist_files if fn.startswith(ARCHIVE_PREFIX))

    print(f"📊 [السكانر] قبل إعادة الضبط: {len(open_positions)} صفقة مفتوحة، "
          f"{len(archive_files)} ملف أرشيف داخل الـGist الرئيسي، "
          f"{len(archive_gist_ids)} Gist أرشيف منفصل مرتبط بالسلسلة.")
    print("🚨 [السكانر] جاري تنفيذ إعادة الضبط الشاملة (بدون نسخة احتياطية)...")

    files_payload = {
        GIST_FILENAME: {"content": json.dumps({}, ensure_ascii=False)},
        POSITIONS_GIST_FILE: {"content": json.dumps([], ensure_ascii=False)},
        CLOSED_GIST_FILE: {"content": json.dumps([], ensure_ascii=False)},
        STATS_GIST_FILE: {"content": json.dumps({}, ensure_ascii=False)},
        ARCHIVE_CHAIN_FILE: {"content": json.dumps([], ensure_ascii=False)},
        ARCHIVE_INDEX_FILE: {"content": json.dumps({}, ensure_ascii=False)},
    }
    for fname in archive_files:
        files_payload[fname] = None  # حذف نهائي (ملفات الأرشيف داخل الـGist الرئيسي)

    patch = requests.patch(API_URL, headers=_headers(), data=json.dumps({"files": files_payload}), timeout=30)
    if patch.status_code != 200:
        print(f"❌ [السكانر] فشل التحديث: {patch.status_code} - {patch.text[:500]}")
        return False

    print("✅ [السكانر] تم تصفير الـGist الرئيسي بالكامل (بما فيه سلسلة/فهرس الأرشيف).")
    failed = _delete_gists(archive_gist_ids, GIST_TOKEN, protected={GIST_ID, PAPER_GIST_ID})
    return failed == 0


def reset_paper_trading():
    """تصفير بوت التداول الوهمي بالكامل. يرجع True عند النجاح."""
    if not PAPER_GIST_TOKEN or not PAPER_GIST_ID:
        print("❌ [المحفظة الوهمية] PAPER_GIST_TOKEN أو PAPER_GIST_ID غير موجودين.")
        return False
    if PAPER_GIST_ID == GIST_ID:
        print("❌ [المحفظة الوهمية] PAPER_GIST_ID يساوي GIST_ID — توقّف احترازيًا.")
        return False

    url = f"https://api.github.com/gists/{PAPER_GIST_ID}"
    r = requests.get(url, headers=_headers(PAPER_GIST_TOKEN), timeout=20)
    r.raise_for_status()
    gist_files = r.json().get("files", {})

    paper_open = _safe_json(gist_files.get(PAPER_POSITIONS_FILE, {}).get("content"), [])
    sub_state = _safe_json(gist_files.get(PAPER_SUB_STATE_FILE, {}).get("content"), {})
    sub_open = sum(len((sub_state.get(t) or {}).get("positions") or []) for t in SIGNAL_TYPES)

    chain = _safe_json(gist_files.get(PAPER_ARCHIVE_CHAIN_FILE, {}).get("content"), [])
    archive_gist_ids = [gid for gid in chain if gid not in (GIST_ID, PAPER_GIST_ID)]
    archive_files = sorted(fn for fn in gist_files if fn.startswith(PAPER_ARCHIVE_PREFIX))

    print(f"📊 [المحفظة الوهمية] قبل إعادة الضبط: {len(paper_open)} صفقة مفتوحة بالرئيسية، "
          f"{sub_open} بالمحافظ الفرعية، {len(archive_files)} ملف أرشيف داخل الـGist، "
          f"{len(archive_gist_ids)} Gist أرشيف منفصل.")
    print("🚨 [المحفظة الوهمية] جاري التصفير الكامل...")

    fresh_sub = {t: {"balance": _new_balance(SUB_CAPITAL), "positions": []} for t in SIGNAL_TYPES}
    files_payload = {
        PAPER_BALANCE_FILE: {"content": json.dumps(_new_balance(PAPER_CAPITAL), ensure_ascii=False)},
        PAPER_POSITIONS_FILE: {"content": json.dumps([], ensure_ascii=False)},
        PAPER_CLOSED_FILE: {"content": json.dumps([], ensure_ascii=False)},
        PAPER_SUB_STATE_FILE: {"content": json.dumps(fresh_sub, ensure_ascii=False)},
        PAPER_ARCHIVE_CHAIN_FILE: {"content": json.dumps([], ensure_ascii=False)},
        PAPER_ARCHIVE_INDEX_FILE: {"content": json.dumps({}, ensure_ascii=False)},
    }
    for fname in archive_files:
        files_payload[fname] = None  # حذف نهائي لملفات أرشيف المحفظة الوهمية

    patch = requests.patch(url, headers=_headers(PAPER_GIST_TOKEN), data=json.dumps({"files": files_payload}), timeout=30)
    if patch.status_code != 200:
        print(f"❌ [المحفظة الوهمية] فشل التحديث: {patch.status_code} - {patch.text[:500]}")
        return False

    print(f"✅ [المحفظة الوهمية] تم التصفير: المحفظة الرئيسية = {PAPER_CAPITAL:g}$، "
          f"و{len(SIGNAL_TYPES)} محافظ فرعية × {SUB_CAPITAL:g}$، بدون صفقات ولا سجل ولا أرشيف.")
    failed = _delete_gists(archive_gist_ids, PAPER_GIST_TOKEN, protected={GIST_ID, PAPER_GIST_ID})
    return failed == 0


def main():
    if not GIST_TOKEN or not GIST_ID:
        print("❌ GIST_TOKEN أو GIST_ID غير موجودين.")
        sys.exit(1)

    scanner_ok = reset_scanner()
    paper_ok = reset_paper_trading()

    if scanner_ok and paper_ok:
        print("✅ تم! السكانر والمحفظة الوهمية معًا بدون صفقات، بدون سجل، بدون أرشيف — يبدآن من الصفر تمامًا.")
        return
    print(f"❌ إعادة الضبط لم تكتمل: السكانر={'✅' if scanner_ok else '❌'}، المحفظة الوهمية={'✅' if paper_ok else '❌'}")
    sys.exit(1)


if __name__ == "__main__":
    main()
