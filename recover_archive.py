#!/usr/bin/env python3
"""
استرجاع الصفقات التي ضاعت من Gist الأرشيف (بسبب إعادة كتابة archive_0008 بصفقات جديدة فقط).

الفكرة: يمرّ على كل تعديلات (revisions) كل Gist أرشيف، ويجمع كل صفقة ظهرت يومًا في أي ملف أرشيف،
ثم يطرح منها ما هو موجود الآن فعليًا. الباقي = الصفقات الضائعة.

  python recover_archive.py            # تجربة فقط: يطبع ما سيُسترجع بدون أي كتابة
  python recover_archive.py --apply    # يكتب الصفقات الضائعة كملفات أرشيف جديدة داخل نفس الـ Gist

المتغيرات: GIST_TOKEN, GIST_ID  |  اختياري: MAX_REVISIONS (افتراضي 150 تعديل لكل Gist)
ملاحظة: التطابق بين الصفقات بالسجل الكامل (JSON) مع مراعاة التكرار، فلا تُضاف صفقة موجودة أصلًا.
"""
import json
import os
import sys
from collections import Counter

import requests

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")
MAX_REVISIONS = int(os.environ.get("MAX_REVISIONS", "150"))
ARCHIVE_PREFIX = "closed_trades_archive_"
ARCHIVE_CHAIN_FILE = "archive_gists_chain.json"
CHUNK = 150
API = "https://api.github.com"
H = {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}
_raw_cache = {}


def get_json(url, **params):
    r = requests.get(url, headers=H, params=params or None, timeout=30)
    r.raise_for_status()
    return r.json()


def read_list(entry, name):
    """قراءة كاملة وسليمة لملف JSON (مع raw_url لو الـAPI اقتطع المحتوى)."""
    content = entry.get("content")
    if not entry.get("truncated") and (content or (entry.get("size") or 0) <= 2):
        try:
            data = json.loads(content or "[]")
            if isinstance(data, list):
                return data
        except Exception:
            pass
    raw = entry.get("raw_url")
    if not raw:
        raise RuntimeError(f"{name}: لا يوجد raw_url")
    if raw not in _raw_cache:
        r = requests.get(raw, headers=H, timeout=30)
        r.raise_for_status()
        r.encoding = "utf-8"
        _raw_cache[raw] = json.loads(r.text)
    return _raw_cache[raw]


def archive_gist_ids():
    main = get_json(f"{API}/gists/{GIST_ID}")["files"]
    ids = []
    if ARCHIVE_CHAIN_FILE in main:
        ids = json.loads(main[ARCHIVE_CHAIN_FILE].get("content") or "[]")
    # أرشيف داخل الـGist الرئيسي نفسه (إن وُجد)
    if any(n.startswith(ARCHIVE_PREFIX) for n in main) and GIST_ID not in ids:
        ids.append(GIST_ID)
    return ids


def list_commits(gid):
    out, page = [], 1
    while len(out) < MAX_REVISIONS:
        chunk = get_json(f"{API}/gists/{gid}/commits", per_page=100, page=page)
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return out[:MAX_REVISIONS]


def key_of(t):
    return json.dumps(t, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def snapshot(files):
    """كل صفقات ملفات الأرشيف في تعديل واحد: (Counter بالمفاتيح، ترتيب ظهور)."""
    cnt, order = Counter(), []
    for name in sorted(n for n in files if n.startswith(ARCHIVE_PREFIX)):
        for t in read_list(files[name], name):
            k = key_of(t)
            cnt[k] += 1
            order.append(k)
    return cnt, order


def next_index(files):
    nums = [int(n[len(ARCHIVE_PREFIX):].replace(".json", ""))
            for n in files if n.startswith(ARCHIVE_PREFIX)]
    return (max(nums) if nums else 0) + 1


def main():
    if not GIST_TOKEN or not GIST_ID:
        sys.exit("❌ GIST_TOKEN أو GIST_ID غير موجودين.")
    apply = "--apply" in sys.argv
    print(f"🔎 وضع: {'كتابة فعلية (--apply)' if apply else 'تجربة فقط (بدون كتابة)'}\n")

    total_missing = 0
    for gid in archive_gist_ids():
        commits = list_commits(gid)
        commits.reverse()  # الأقدم أولًا
        union, first_seen = Counter(), {}
        for c in commits:
            files = get_json(f"{API}/gists/{gid}/{c['version']}")["files"]
            cnt, order = snapshot(files)
            for k, n in cnt.items():
                union[k] = max(union[k], n)
            for k in order:
                first_seen.setdefault(k, len(first_seen))

        cur_files = get_json(f"{API}/gists/{gid}")["files"]
        current, _ = snapshot(cur_files)
        missing = union - current
        miss_keys = sorted(missing.elements(), key=lambda k: first_seen.get(k, 0))
        total_missing += len(miss_keys)

        print(f"═══ Gist {gid[:8]}… ═══")
        print(f"تعديلات مفحوصة: {len(commits)} | صفقات حالية: {sum(current.values())} "
              f"| كل ما ظهر يومًا: {sum(union.values())} | ضائعة: {len(miss_keys)}")
        for k in miss_keys[:5]:
            t = json.loads(k)
            print("  •", t.get("symbol"), t.get("type"), t.get("closed_at") or t.get("opened_at"))
        if len(miss_keys) > 5:
            print(f"  … و{len(miss_keys) - 5} أخرى")

        if apply and miss_keys:
            idx = next_index(cur_files)
            new_files = {}
            for i in range(0, len(miss_keys), CHUNK):
                part = [json.loads(k) for k in miss_keys[i:i + CHUNK]]
                new_files[f"{ARCHIVE_PREFIX}{idx:04d}.json"] = {
                    "content": json.dumps(part, ensure_ascii=False, separators=(",", ":"))}
                idx += 1
            r = requests.patch(f"{API}/gists/{gid}", headers=H, json={"files": new_files}, timeout=60)
            r.raise_for_status()
            print(f"✅ كُتبت {len(miss_keys)} صفقة في الملفات: {list(new_files)}")
        print()

    if not apply and total_missing:
        print("لم يُكتب شيء. أعد التشغيل مع --apply لاسترجاع الصفقات الضائعة.")


if __name__ == "__main__":
    main()
