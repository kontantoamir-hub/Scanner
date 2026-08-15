"""
trade_stats.py — تقرير مستقل لإحصائيات الصفقات المغلقة

يقرأ closed_trades.json من نفس الـ Gist المستخدم في scanner.py، ويحسب:
- عدد الصفقات المنجزة: رابحة / خاسرة / مغلقة بشكل محايد (بدون ربح ولا خسارة فعلية)
- نسبة الربح ونسبة الخسارة لكل فئة
- لكل صفقة: نوع المؤشر (المؤشرات) التي كانت حاضرة وقت الدخول والنتيجة
- نسبة نجاح كل مؤشر على حدة (squeeze / accumulation / divergence / extended) عبر كل الصفقات

لا يُعدّل أي شيء في منطق البوت أو ملفاته — قراءة وعرض فقط (يمكن تشغيله يدويًا
عبر workflow_dispatch أو محليًا بدون أي تأثير على عمل scanner.py).

المتغيرات المطلوبة (نفس Secrets المستخدمة في scanner.py):
  GIST_TOKEN, GIST_ID
اختياري لإرسال التقرير عبر تيليجرام بدل الطباعة فقط:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""

import os
import json
import datetime as dt
import requests

GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")
CLOSED_GIST_FILE = "closed_trades.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

INDICATOR_KEYS = ["squeeze", "accumulation", "divergence", "extended"]
INDICATOR_LABELS = {
    "squeeze": "انضغاط تقلب (Squeeze)",
    "accumulation": "تراكم صامت (Accumulation)",
    "divergence": "دايفرجنس (Divergence)",
    "extended": "امتداد زائد (Overextension)",
}


def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def load_closed_trades():
    if not GIST_TOKEN or not GIST_ID:
        raise SystemExit("❌ GIST_TOKEN أو GIST_ID غير موجودين في متغيرات البيئة.")
    r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=_gist_headers(), timeout=15)
    r.raise_for_status()
    files = r.json().get("files", {})
    if CLOSED_GIST_FILE not in files:
        return []
    content = files[CLOSED_GIST_FILE]["content"]
    try:
        return json.loads(content)
    except Exception as e:
        raise SystemExit(f"❌ تعذّر قراءة {CLOSED_GIST_FILE}: {e}")


def classify(trade):
    """يحدد نتيجة الصفقة: win / loss / neutral، بناءً على سبب الإغلاق وعدد الأهداف المتحققة."""
    reason = trade.get("closed_reason", "UNKNOWN")
    hit = len(trade.get("hit_tps") or [])
    if reason == "ALL_TP" or hit > 0:
        return "win"
    if reason == "SL" and hit == 0:
        return "loss"
    return "neutral"  # INVALIDATED أو EXPIRED بدون أي هدف محقق


def pnl_pct(trade):
    entry, exit_price = trade.get("entry"), trade.get("exit_price")
    if entry and exit_price:
        return (exit_price - entry) / entry * 100
    return None


def duration_hours(trade):
    try:
        t0 = dt.datetime.strptime(trade["opened_at"], "%Y-%m-%d %H:%M:%S")
        t1 = dt.datetime.strptime(trade["closed_at"], "%Y-%m-%d %H:%M:%S")
        return (t1 - t0).total_seconds() / 3600
    except Exception:
        return None


def active_indicators(trade):
    """يرجع قائمة أسماء المؤشرات/العوامل التي كانت True وقت فتح هذه الصفقة."""
    return [k for k in INDICATOR_KEYS if trade.get(k) is True]


def build_report(trades):
    if not trades:
        return "لا توجد صفقات مغلقة بعد في السجل.", []

    total = len(trades)
    wins = [t for t in trades if classify(t) == "win"]
    losses = [t for t in trades if classify(t) == "loss"]
    neutral = [t for t in trades if classify(t) == "neutral"]

    win_rate = len(wins) / total * 100
    loss_rate = len(losses) / total * 100
    neutral_rate = len(neutral) / total * 100

    # --- نجاح كل مؤشر على حدة ---
    indicator_stats = {k: {"total": 0, "win": 0, "loss": 0, "neutral": 0} for k in INDICATOR_KEYS}
    no_indicator_stats = {"total": 0, "win": 0, "loss": 0, "neutral": 0}

    per_trade_lines = []
    for t in trades:
        outcome = classify(t)
        inds = active_indicators(t)
        pnl = pnl_pct(t)
        dur = duration_hours(t)

        if inds:
            for k in inds:
                indicator_stats[k]["total"] += 1
                indicator_stats[k][outcome] += 1
        else:
            no_indicator_stats["total"] += 1
            no_indicator_stats[outcome] += 1

        outcome_ar = {"win": "✅ ربح", "loss": "❌ خسارة", "neutral": "⚪ محايد"}[outcome]
        inds_ar = "، ".join(INDICATOR_LABELS[k] for k in inds) if inds else "بدون مؤشر إضافي (إشارة رسمية عادية)"
        pnl_txt = f"{pnl:+.2f}%" if pnl is not None else "—"
        dur_txt = f"{dur:.0f}س" if dur is not None else "—"
        per_trade_lines.append(
            f"{t.get('symbol','?')} | نوع: {t.get('type','official')} | score: {t.get('score','?')} | "
            f"{outcome_ar} | عائد: {pnl_txt} | مدة: {dur_txt} | المؤشرات: {inds_ar}"
        )

    # --- بناء نص التقرير المختصر ---
    lines = [
        "📊 تقرير الصفقات المنجزة",
        f"الإجمالي: {total} صفقة",
        f"✅ رابحة: {len(wins)} ({win_rate:.1f}%)",
        f"❌ خاسرة: {len(losses)} ({loss_rate:.1f}%)",
        f"⚪ محايدة (مغلقة بدون ربح/خسارة فعلية): {len(neutral)} ({neutral_rate:.1f}%)",
        "",
        "— نسبة النجاح حسب المؤشر —",
    ]
    for k in INDICATOR_KEYS:
        s = indicator_stats[k]
        if s["total"] == 0:
            continue
        wr = s["win"] / s["total"] * 100
        lines.append(f"{INDICATOR_LABELS[k]}: {s['total']} صفقة | نجاح {wr:.0f}% (رابحة {s['win']} / خاسرة {s['loss']})")

    if no_indicator_stats["total"] > 0:
        s = no_indicator_stats
        wr = s["win"] / s["total"] * 100
        lines.append(f"بدون مؤشر إضافي: {s['total']} صفقة | نجاح {wr:.0f}% (رابحة {s['win']} / خاسرة {s['loss']})")

    return "\n".join(lines), per_trade_lines


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    # تيليجرام يحدد طول الرسالة بـ 4096 حرف تقريبًا — نقسم لو تجاوز
    chunk = 3800
    for i in range(0, len(text), chunk):
        try:
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text[i:i + chunk]}, timeout=15)
        except Exception as e:
            print("تعذّر إرسال التقرير عبر تيليجرام:", e)


def main():
    trades = load_closed_trades()
    summary, per_trade_lines = build_report(trades)

    print(summary)
    print("\n— تفصيل كل صفقة —")
    for line in per_trade_lines:
        print(line)

    # يُرسل الملخص فقط عبر تيليجرام (التفصيل الكامل لكل صفقة يبقى في سجل التشغيل GitHub Actions
    # تجنبًا لإغراق المحادثة برسالة طويلة جدًا)
    send_telegram(summary)


if __name__ == "__main__":
    main()
