"""
reset_state.py
---------------
سكربت إعادة ضبط كاملة (Factory Reset) لبوت Scanner.
يستخدم نفس متغيرات البيئة GIST_TOKEN و GIST_ID التي يستخدمها scanner.py،
ويعيد تصفير كل ملفات الحالة داخل الـ Gist:

  - alerted_state.json  -> الإشارات المُرسلة وذاكرة BTC Dominance
  - open_positions.json -> الصفقات المفتوحة (كلها تُغلق/تُمسح بدون تسجيلها كمكاسب/خسائر)
  - closed_trades.json  -> سجل الصفقات المغلقة (السجل التاريخي)
  - stats.json          -> الإحصائيات المحسوبة

بعد تشغيل هذا السكربت مرة واحدة، البوت (scanner.py) سيبدأ من الصفر تمامًا
في التشغيلة القادمة، دون أي تعديل على scanner.py نفسه.

تشغيل:
    GIST_TOKEN=xxx GIST_ID=yyy python reset_state.py

أو إذا كانت المتغيرات محفوظة أصلًا في بيئة التشغيل (مثل GitHub Actions secrets)
يمكن تشغيله مباشرة: python reset_state.py
"""

import os
import json
import sys
import requests

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")

GIST_FILENAME = "alerted_state.json"
POSITIONS_GIST_FILE = "open_positions.json"
CLOSED_GIST_FILE = "closed_trades.json"
STATS_GIST_FILE = "stats.json"


def confirm():
    print("⚠️  جارٍ تنفيذ إعادة الضبط الكاملة:")
    print("   - كل الصفقات المفتوحة حاليًا (بدون تسجيلها كمكسب أو خسارة)")
    print("   - كل الإشارات المحفوظة (ذاكرة alerted)")
    print("   - كل سجل الصفقات المغلقة السابق")
    print("   - كل الإحصائيات المحسوبة")
    print()
    print("(الحماية اليدوية معطّلة — التشغيل عبر GitHub Actions workflow_dispatch يُعتبر تأكيدًا كافيًا)")


def reset_gist():
    if not GIST_TOKEN or not GIST_ID:
        print("❌ GIST_TOKEN أو GIST_ID غير موجودين في متغيرات البيئة. لم يتم تنفيذ أي شيء.")
        sys.exit(1)

    empty_alerted = json.dumps({"alerted": [], "btc_dominance_prev": None}, ensure_ascii=False, indent=2)
    empty_positions = json.dumps([], ensure_ascii=False, indent=2)
    empty_closed = json.dumps([], ensure_ascii=False, indent=2)
    empty_stats = json.dumps({}, ensure_ascii=False, indent=2)

    payload = {
        "files": {
            GIST_FILENAME: {"content": empty_alerted},
            POSITIONS_GIST_FILE: {"content": empty_positions},
            CLOSED_GIST_FILE: {"content": empty_closed},
            STATS_GIST_FILE: {"content": empty_stats},
        }
    }

    headers = {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}
    r = requests.patch(f"https://api.github.com/gists/{GIST_ID}", headers=headers, json=payload, timeout=15)

    if r.ok:
        print("✅ تم إعادة ضبط الحالة بالكامل. البوت سيبدأ من الصفر في التشغيلة القادمة.")
    else:
        print("❌ فشل إعادة الضبط:", r.status_code, r.text)
        sys.exit(1)


if __name__ == "__main__":
    confirm()
    reset_gist()
