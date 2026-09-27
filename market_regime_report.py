"""
market_regime_report.py
========================
يحمّل الأرشيف الكامل للصفقات المغلقة (الملف النشط closed_trades.json + كل ملفات
الأرشيف عبر archive_gists_chain.json -- أبدًا Gist واحد أو الملف النشط فقط)،
ويصنّف الصفقات التي تملك حقل market_regime (trending_up / trending_down / ranging)
حسب حالة السوق العام وقت فتح كل صفقة -- إجمالاً ولكل نوع إشارة (رسمية/مبكرة/انفجار/تجريبية).

منطق الربح/الخسارة الصافي مطابق تمامًا لـ compute_stats في scanner.py:
net_pnl_pct = (exit_price - entry) / entry * 100 - TRADING_FEE_PCT
والتصنيف فوز/خسارة/محايد حسب BREAKEVEN_BAND_PCT (نفس الثوابت المستوردة من scanner.py).

تشغيل: GIST_TOKEN و GIST_ID لازم يكونوا معرّفين.
    python3 market_regime_report.py
"""

import json
import scanner

TYPE_LABELS = {
    "official": "🔴 رسمية",
    "early": "🔵 مبكرة",
    "breakout": "🟠 انفجار",
    "experimental": "🟣 تجريبية",
}
REGIME_LABELS = {
    "trending_up": "📈 صاعد (trending_up)",
    "trending_down": "📉 نازل (trending_down)",
    "ranging": "➖ عرضي (ranging)",
}


def get_full_content(file_meta):
    """يرجع المحتوى الكامل لملف Gist، ويتجاوز حالة truncated عبر raw_url عند الحاجة."""
    if not file_meta:
        return None
    if file_meta.get("truncated") and file_meta.get("raw_url"):
        r = scanner.requests.get(file_meta["raw_url"], timeout=20)
        r.raise_for_status()
        return r.text
    return file_meta.get("content")


def load_json_file(files_dict, filename, default):
    meta = files_dict.get(filename)
    if meta is None:
        return default
    content = get_full_content(meta)
    if not content:
        return default
    return json.loads(content)


def load_full_archive():
    """يحمّل السجل النشط + كل ملفات الأرشيف عبر السلسلة الكاملة. يرجع قائمة واحدة موحّدة."""
    main_files = scanner._gist_get_all_files()
    all_trades = list(load_json_file(main_files, scanner.CLOSED_GIST_FILE, []))

    chain = load_json_file(main_files, scanner.ARCHIVE_CHAIN_FILE, [])
    for gist_id in chain:
        archive_files = main_files if gist_id == scanner.GIST_ID else scanner._gist_get_all_files_for(gist_id)
        archive_names = sorted(fn for fn in archive_files if fn.startswith(scanner.ARCHIVE_PREFIX))
        for fn in archive_names:
            all_trades.extend(load_json_file(archive_files, fn, []))

    return all_trades


def net_pnl_pct(t):
    entry, exit_price = t.get("entry"), t.get("exit_price")
    if not entry or not exit_price:
        return None
    raw_pct = (exit_price - entry) / entry * 100
    return raw_pct - scanner.TRADING_FEE_PCT


def classify(pnl):
    if pnl is None:
        return "neutral"
    if pnl > scanner.BREAKEVEN_BAND_PCT:
        return "win"
    if pnl < -scanner.BREAKEVEN_BAND_PCT:
        return "loss"
    return "neutral"


def summarize(trades):
    """يرجع dict فيه: العدد، نجاح%، متوسط ربح/خسارة، صافي/صفقة، الصافي الإجمالي، PF."""
    n = len(trades)
    if n == 0:
        return None
    wins, losses, neutral = [], [], 0
    for t in trades:
        pnl = net_pnl_pct(t)
        outcome = classify(pnl)
        if outcome == "win":
            wins.append(pnl)
        elif outcome == "loss":
            losses.append(pnl)
        else:
            neutral += 1

    gains_sum = sum(wins)
    losses_sum = sum(losses)  # سالب
    all_pnls = wins + losses
    net_per_trade = (gains_sum + losses_sum) / len(all_pnls) if all_pnls else None
    total_net = gains_sum + losses_sum
    pf = (gains_sum / abs(losses_sum)) if losses_sum < 0 else (float("inf") if gains_sum > 0 else None)
    success_rate = (len(wins) / (len(wins) + len(losses)) * 100) if (wins or losses) else None

    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "neutral": neutral,
        "avg_win": (sum(wins) / len(wins)) if wins else None,
        "avg_loss": (sum(losses) / len(losses)) if losses else None,
        "success_rate": success_rate,
        "net_per_trade": net_per_trade,
        "total_net": total_net,
        "pf": pf,
    }


def format_summary(s):
    if s is None or s["n"] < 5:
        n = s["n"] if s else 0
        return f"⚪ عيّنة صغيرة جدًا ({n} صفقة) — لا حكم"
    pf_txt = f"{s['pf']:.2f}" if s["pf"] not in (None, float("inf")) else ("∞" if s["pf"] == float("inf") else "—")
    succ = f"{s['success_rate']:.1f}%" if s["success_rate"] is not None else "—"
    net = f"{s['net_per_trade']:+.2f}%" if s["net_per_trade"] is not None else "—"
    total = f"{s['total_net']:+.2f}%" if s["total_net"] is not None else "—"
    avg_w = f"{s['avg_win']:+.2f}%" if s["avg_win"] is not None else "—"
    avg_l = f"{s['avg_loss']:+.2f}%" if s["avg_loss"] is not None else "—"
    return (f"{s['n']} صفقة | نجاح {succ} | صافي/صفقة {net} | الصافي الإجمالي {total} | "
            f"PF {pf_txt} | متوسط ربح {avg_w} / خسارة {avg_l} | محايدة {s['neutral']}")


def main():
    print("📥 تحميل الأرشيف الكامل (الملف النشط + كل ملفات الأرشيف) ...")
    all_trades = load_full_archive()
    print(f"✅ {len(all_trades)} صفقة إجمالاً")

    eligible = [t for t in all_trades if t.get("market_regime") in REGIME_LABELS]
    print(f"📋 {len(eligible)} صفقة تملك market_regime (من أصل {len(all_trades)})\n")

    print("═" * 45)
    print("📊 حسب نظام السوق -- كل الأنواع مجتمعة")
    print("═" * 45)
    for regime, label in REGIME_LABELS.items():
        subset = [t for t in eligible if t.get("market_regime") == regime]
        print(f"{label}: {format_summary(summarize(subset))}")

    print()
    print("═" * 45)
    print("📊 تفصيل حسب نوع الإشارة × نظام السوق")
    print("═" * 45)
    for ttype, tlabel in TYPE_LABELS.items():
        type_trades = [t for t in eligible if t.get("type") == ttype]
        print(f"\n{tlabel} -- إجمالي {len(type_trades)} صفقة مؤهلة")
        for regime, rlabel in REGIME_LABELS.items():
            subset = [t for t in type_trades if t.get("market_regime") == regime]
            print(f"   {rlabel}: {format_summary(summarize(subset))}")

    print("\n" + "═" * 45)
    print("ملاحظة: PF والصافي محسوبان من النسب المئوية للصفقة (وليس من مبالغ $)، "
          "بافتراض حجم متساوٍ تقريبي لكل صفقة -- تقريبي وليس دقيقًا 100%.")


if __name__ == "__main__":
    main()
