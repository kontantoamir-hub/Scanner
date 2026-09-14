#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
إعادة ضبط شاملة (بدون نسخة احتياطية) - مخصص للتشغيل من GitHub Actions.
يفرّغ كل شيء بالـ Gist الرئيسي (بما فيه سلسلة/فهرس أرشيف الـGists المنفصلة)
ويقفل كل الصفقات المفتوحة بدون استثناء.
"""

import os
import sys
import json
import requests

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")

GIST_FILENAME = "alerted_state.json"
POSITIONS_GIST_FILE = "open_positions.json"
CLOSED_GIST_FILE = "closed_trades.json"
STATS_GIST_FILE = "stats.json"
ARCHIVE_PREFIX = "closed_trades_archive_"
ARCHIVE_CHAIN_FILE = "archive_gists_chain.json"   # ← جديد: لائحة معرّفات Gists الأرشيف
ARCHIVE_INDEX_FILE = "archive_index.json"          # ← جديد: فهرس تواريخ كل Gist أرشيف

API_URL = f"https://api.github.com/gists/{GIST_ID}"


def _headers():
    return {
        "Authorization": f"token {GIST_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


def main():
    if not GIST_TOKEN or not GIST_ID:
        print("❌ GIST_TOKEN أو GIST_ID غير موجودين.")
        sys.exit(1)

    r = requests.get(API_URL, headers=_headers(), timeout=20)
    r.raise_for_status()
    gist_files = r.json().get("files", {})

    open_positions = []
    try:
        open_positions = json.loads(gist_files.get(POSITIONS_GIST_FILE, {}).get("content") or "[]")
    except json.JSONDecodeError:
        pass

    # قراءة سلسلة الأرشيف قبل تصفيرها (باش نعرفو شحال من Gist أرشيف منفصل كاين)
    archive_gist_ids = []
    try:
        chain_raw = gist_files.get(ARCHIVE_CHAIN_FILE, {}).get("content")
        chain = json.loads(chain_raw) if chain_raw else []
        archive_gist_ids = [gid for gid in chain if gid != GIST_ID]
    except json.JSONDecodeError:
        pass

    archive_files = sorted(fn for fn in gist_files if fn.startswith(ARCHIVE_PREFIX))

    print(f"📊 قبل إعادة الضبط: {len(open_positions)} صفقة مفتوحة، "
          f"{len(archive_files)} ملف أرشيف داخل الـGist الرئيسي، "
          f"{len(archive_gist_ids)} Gist أرشيف منفصل مرتبط بالسلسلة.")
    print("🚨 جاري تنفيذ إعادة الضبط الشاملة (بدون نسخة احتياطية)...")

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

    body = {"files": files_payload}
    patch = requests.patch(API_URL, headers=_headers(), data=json.dumps(body), timeout=30)
    if patch.status_code != 200:
        print(f"❌ فشل التحديث: {patch.status_code} - {patch.text[:500]}")
        sys.exit(1)

    print("✅ تم تصفير الـGist الرئيسي بالكامل (بما فيه سلسلة/فهرس الأرشيف).")

    # حذف نهائي لكل Gist أرشيف منفصل كان مرتبط بالسلسلة
    for gid in archive_gist_ids:
        del_url = f"https://api.github.com/gists/{gid}"
        resp = requests.delete(del_url, headers=_headers(), timeout=20)
        if resp.status_code == 204:
            print(f"🗑️ تم حذف Gist الأرشيف المنفصل: {gid}")
        else:
            print(f"⚠️ تعذّر حذف Gist الأرشيف {gid}: {resp.status_code} - {resp.text[:200]}")

    print("✅ تم! البوت الآن بدون صفقات مفتوحة، بدون سجل، بدون أرشيف — يبدأ من الصفر تمامًا.")


if __name__ == "__main__":
    main()
