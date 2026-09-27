#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
moja_funded_simulator.py
=========================
محاكاة: "لو دخلت بصفقات بوت السكانر (scanner.py) على حساب MOJA Funded
الممول (Crypto Spot, 2-Step Challenge) — هل كنت راح أربح التحدي أو أخسره؟"

المصدر: يحمّل الأرشيف الكامل للصفقات المغلقة (journal النشط closed_trades.json
+ سلسلة أرشيف الصفقات القديمة closed_trades_archive_000N.json) بنفس طريقة
pnl_report.py الحالي عندك — أبدًا الـ journal النشط لوحده.

قواعد MOJA Funded (من https://mojafunded.com/precios/ بتاريخ 2026-09-27),
كلها ثابتة بغض النظر عن حجم الحساب ($5K...$200K):
    - Profit Target: Phase 1 = 8% | Phase 2 = 5%
    - Max Daily Loss = 5%   (من رصيد بداية اليوم)
    - Max Total Loss = 10%  (من رأس المال الابتدائي)
    - Min Trading Days = 7 أيام تداول فعلية (فيها صفقة واحدة على الأقل)
    - Trading Period = بدون حد زمني (Unlimited)

⚠️ افتراضات لازم تراجعها/تعدّلها حسب ما يناسبك (كلها في قسم CONFIG تحت):
  1) البوت الحالي يستخدم حجم صفقة ثابت (100$ أو 300$) — وهذا غير منطقي على
     حساب بقواعد drawdown بالنسبة المئوية. هنا افتراضيًا نستخدم:
     RISK_PER_TRADE_PCT = 1.0%  من الرصيد الحالي في كل صفقة (مخاطرة مركّبة).
  2) رسوم التداول TRADING_FEE_PCT = 0.2% (نفس قيمة scanner.py) تُطبّق على
     الدخول والخروج.
  3) الصفقات المفتوحة أكثر من 96 ساعة وتُغلق EXPIRED تُحتسب بربحها/خسارتها
     الفعلية عند سعر الإغلاق (نفس منطق scanner.py).
  4) الترتيب الزمني للصفقات = ترتيب الدخول (entry time)، وما فيه أكثر من
     صفقة مفتوحة بنفس الوقت (تبسيط — البوت الحقيقي قد يفتح عدة صفقات معًا).

الاستخدام:
    export GIST_TOKEN=xxxx
    export GIST_ID=xxxx                 # الـ Gist الرئيسي (نفس متغيرات scanner.py)
    export ARCHIVE_GIST_ID=xxxx         # (اختياري) لو الأرشيف بجست منفصل معروف
    python3 moja_funded_simulator.py --account 5000 --risk 1.0 --signal-type all
"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("يلزم تثبيت المكتبة: pip install requests", file=sys.stderr)
    sys.exit(1)

# =========================== CONFIG (عدّل هنا) ===========================

TRADING_FEE_PCT = 0.2          # نفس TRADING_FEE_PCT في scanner.py (نسبة مئوية)
DEFAULT_RISK_PER_TRADE_PCT = 1.0  # افتراض: 1% من الرصيد الحالي لكل صفقة
DEFAULT_ACCOUNT_SIZE = 5000.0

# قواعد MOJA Funded (ثابتة لكل الأحجام حسب صفحة /precios/)
PHASE1_TARGET_PCT = 8.0
PHASE2_TARGET_PCT = 5.0
MAX_DAILY_LOSS_PCT = 5.0
MAX_TOTAL_LOSS_PCT = 10.0
MIN_TRADING_DAYS = 7

GIST_API = "https://api.github.com/gists/{gist_id}"

# ===========================================================================


def gh_headers():
    token = os.environ.get("GIST_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def fetch_gist_files(gist_id):
    """يرجع dict: filename -> parsed json content لكل ملفات .json داخل Gist معيّن."""
    r = requests.get(GIST_API.format(gist_id=gist_id), headers=gh_headers(), timeout=30)
    r.raise_for_status()
    data = r.json()
    out = {}
    for fname, meta in data.get("files", {}).items():
        if not fname.endswith(".json"):
            continue
        content = meta.get("content")
        if meta.get("truncated"):
            raw = requests.get(meta["raw_url"], headers=gh_headers(), timeout=30).text
            content = raw
        try:
            out[fname] = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def load_full_trade_archive():
    """
    يحمّل الأرشيف الكامل:
      1) closed_trades.json من الـ Gist الرئيسي (GIST_ID) — الجورنال النشط (حتى 150 صفقة)
      2) archive_index.json / archive_gists_chain.json من نفس الـ Gist الرئيسي،
         لمعرفة أي Gist(s) أرشيف يجب قراءتها
      3) كل ملفات closed_trades_archive_000N.json من جست/جسوت الأرشيف
    ثم يدمج الكل ويرتبها زمنيًا.
    """
    main_gist_id = os.environ.get("GIST_ID")
    if not main_gist_id:
        print("خطأ: لازم تحدد GIST_ID (نفس Gist scanner.py الرئيسي) كمتغير بيئة.",
              file=sys.stderr)
        sys.exit(1)

    print(f"[1/3] تحميل الـ Gist الرئيسي ({main_gist_id}) ...")
    main_files = fetch_gist_files(main_gist_id)

    active_trades = main_files.get("closed_trades.json", [])
    if isinstance(active_trades, dict):
        active_trades = active_trades.get("trades", [])
    print(f"      journal نشط: {len(active_trades)} صفقة")

    archive_gist_ids = set()
    env_archive = os.environ.get("ARCHIVE_GIST_ID")
    if env_archive:
        archive_gist_ids.add(env_archive)

    for key in ("archive_gists_chain.json", "archive_index.json"):
        chain = main_files.get(key)
        if not chain:
            continue
        if isinstance(chain, list):
            for item in chain:
                if isinstance(item, str):
                    archive_gist_ids.add(item)
                elif isinstance(item, dict):
                    gid = item.get("gist_id") or item.get("id")
                    if gid:
                        archive_gist_ids.add(gid)
        elif isinstance(chain, dict):
            for v in chain.values():
                if isinstance(v, str):
                    archive_gist_ids.add(v)
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, str):
                            archive_gist_ids.add(item)
                        elif isinstance(item, dict):
                            gid = item.get("gist_id") or item.get("id")
                            if gid:
                                archive_gist_ids.add(gid)

    archived_trades = []
    if archive_gist_ids:
        print(f"[2/3] تحميل {len(archive_gist_ids)} جست أرشيف ...")
        for gid in archive_gist_ids:
            try:
                afiles = fetch_gist_files(gid)
            except requests.HTTPError as e:
                print(f"      تحذير: تعذّر قراءة الجست {gid}: {e}", file=sys.stderr)
                continue
            for fname, content in afiles.items():
                if not fname.startswith("closed_trades_archive_"):
                    continue
                trades = content.get("trades") if isinstance(content, dict) else content
                if isinstance(trades, list):
                    archived_trades.extend(trades)
        print(f"      صفقات مؤرشفة: {len(archived_trades)} صفقة")
    else:
        print("      لم يُحدَّد/يُعثر على جست أرشيف — تحقق من ARCHIVE_GIST_ID "
              "أو من مفتاح السلسلة داخل الـ Gist الرئيسي (اسم الحقل قد يختلف "
              "عن archive_gists_chain.json / archive_index.json عندك، عدّله أعلى الدالة).")

    all_trades = archived_trades + active_trades
    print(f"[3/3] الإجمالي بعد الدمج: {len(all_trades)} صفقة")
    return all_trades


def get_field(trade, *candidates, default=None):
    for c in candidates:
        if c in trade and trade[c] is not None:
            return trade[c]
    return default


def compute_trade_return_pct(trade):
    """
    يحسب نسبة الربح/الخسارة الصافية للصفقة من entry/exit price (السكربت لا
    يخزّن pnl% كما هو موثّق) — نفس منطق pnl_report.py: صافي بعد خصم الرسوم
    على الدخول والخروج.
    """
    entry = get_field(trade, "entry_price", "entry")
    exitp = get_field(trade, "exit_price", "exit", "close_price")
    if entry is None or exitp is None:
        return None
    try:
        entry = float(entry)
        exitp = float(exitp)
    except (TypeError, ValueError):
        return None
    if entry == 0:
        return None
    gross_pct = (exitp / entry - 1.0) * 100.0
    net_pct = gross_pct - TRADING_FEE_PCT  # رسوم دخول+خروج مقدّرة كإجمالي ثابت
    return net_pct


def get_trade_datetime(trade):
    ts = get_field(trade, "exit_time", "close_time", "closed_at",
                    "entry_time", "open_time", "opened_at")
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            # يدعم ثواني أو ميلي ثانية
            if ts > 10_000_000_000:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError, OSError):
        return None


def simulate(trades, account_size, risk_pct, signal_type_filter):
    # فلترة نوع الإشارة إذا طُلب (officielle/رسمية/precoce/breakout/experimental/all)
    if signal_type_filter and signal_type_filter != "all":
        trades = [t for t in trades
                  if str(get_field(t, "signal_type", "type", default="")).lower()
                  == signal_type_filter.lower()]

    # ترتيب زمني
    dated = [(get_trade_datetime(t), t) for t in trades]
    dated = [d for d in dated if d[0] is not None]
    dated.sort(key=lambda x: x[0])

    if not dated:
        print("لا توجد صفقات صالحة (تحقق من أسماء الحقول entry_price/exit_price/"
              "entry_time في بياناتك — عدّل دالة get_field إذا كانت مختلفة).")
        return

    balance = account_size
    initial_balance = account_size
    phase = 1
    phase_start_balance = account_size
    day_start_balance = account_size
    current_day = None
    trading_days = set()
    peak_balance = account_size

    print("\n" + "=" * 70)
    print(f"محاكاة تحدي MOJA Funded — حساب ${account_size:,.0f} | "
          f"مخاطرة/صفقة: {risk_pct}% | نوع الإشارة: {signal_type_filter or 'all'}")
    print("=" * 70)

    for dt, trade in dated:
        day_key = dt.date()
        if current_day is None:
            current_day = day_key
            day_start_balance = balance
        elif day_key != current_day:
            current_day = day_key
            day_start_balance = balance

        ret_pct = compute_trade_return_pct(trade)
        if ret_pct is None:
            continue

        trading_days.add(day_key)
        # نطبّق ret_pct% (العائد الصافي الفعلي للصفقة، محسوب من entry/exit price)
        # مباشرة على قيمة المخاطرة المخصصة لهذه الصفقة كنسبة من الرصيد الحالي
        risk_amount = balance * (risk_pct / 100.0)
        pnl_amount = risk_amount * (ret_pct / 100.0)

        balance += pnl_amount
        peak_balance = max(peak_balance, balance)

        daily_loss_pct = (day_start_balance - balance) / day_start_balance * 100.0
        total_loss_pct = (initial_balance - balance) / initial_balance * 100.0
        phase_gain_pct = (balance - phase_start_balance) / phase_start_balance * 100.0

        # فشل: تجاوز الخسارة اليومية أو الخسارة الكلية
        if daily_loss_pct >= MAX_DAILY_LOSS_PCT:
            print(f"\n❌ فشل التحدي (Phase {phase}) بتاريخ {dt.date()} — "
                  f"تجاوز Max Daily Loss ({daily_loss_pct:.2f}% ≥ {MAX_DAILY_LOSS_PCT}%)")
            print(f"   الرصيد عند الفشل: ${balance:,.2f}")
            return

        if total_loss_pct >= MAX_TOTAL_LOSS_PCT:
            print(f"\n❌ فشل التحدي (Phase {phase}) بتاريخ {dt.date()} — "
                  f"تجاوز Max Total Loss ({total_loss_pct:.2f}% ≥ {MAX_TOTAL_LOSS_PCT}%)")
            print(f"   الرصيد عند الفشل: ${balance:,.2f}")
            return

        target_pct = PHASE1_TARGET_PCT if phase == 1 else PHASE2_TARGET_PCT
        if phase_gain_pct >= target_pct and len(trading_days) >= MIN_TRADING_DAYS:
            if phase == 1:
                print(f"\n✅ نجاح Phase 1 بتاريخ {dt.date()} — ربح {phase_gain_pct:.2f}% "
                      f"(الهدف {target_pct}%) بعد {len(trading_days)} أيام تداول")
                phase = 2
                phase_start_balance = balance
                trading_days = set()  # كل Phase يحتاج 7 أيام تداول من جديد (حسب قواعد MOJA)
            else:
                print(f"\n🏆 نجاح Phase 2 — التحدي بالكامل ناجح بتاريخ {dt.date()} — "
                      f"ربح {phase_gain_pct:.2f}% (الهدف {target_pct}%) بعد "
                      f"{len(trading_days)} أيام تداول")
                print(f"   الرصيد النهائي: ${balance:,.2f}")
                print(f"   الحساب مؤهل للتمويل (funded) حسب قواعد MOJA Funded.")
                return

    # انتهت الصفقات بدون فشل ولا نجاح كامل
    print(f"\n⏳ انتهت بيانات الصفقات المتاحة والتحدي لم يُحسم بعد.")
    print(f"   المرحلة الحالية: Phase {phase} | الرصيد: ${balance:,.2f} "
          f"({(balance - initial_balance) / initial_balance * 100:.2f}% إجمالي)")
    print(f"   أيام التداول المحتسبة في هذه المرحلة: {len(trading_days)}/{MIN_TRADING_DAYS}")


def main():
    parser = argparse.ArgumentParser(description="محاكاة تحدي MOJA Funded بصفقات بوت السكانر")
    parser.add_argument("--account", type=float, default=DEFAULT_ACCOUNT_SIZE,
                         help="حجم الحساب الممول بالدولار (5000, 10000, 25000, 50000, 100000, 200000)")
    parser.add_argument("--risk", type=float, default=DEFAULT_RISK_PER_TRADE_PCT,
                         help="نسبة المخاطرة من الرصيد الحالي لكل صفقة (%%)")
    parser.add_argument("--signal-type", type=str, default="all",
                         help="فلترة حسب نوع الإشارة: all / رسمية / مبكرة / انفجار / تجريبية "
                              "(استخدم القيمة كما هي مخزّنة في حقل signal_type عندك)")
    args = parser.parse_args()

    trades = load_full_trade_archive()
    simulate(trades, args.account, args.risk, args.signal_type)


if __name__ == "__main__":
    main()
