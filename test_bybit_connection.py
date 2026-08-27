"""
test_bybit_connection.py — اختبار اتصال فقط، بدون أي منطق تنفيذ صفقات

الهدف الوحيد: التأكد إن GitHub Actions يقدر يوصل لـBybit Testnet API
بدون حظر جغرافي (زي مشكلة الخطأ 451 اللي واجهناها مع Binance).

المتغيرات البيئية المطلوبة:
  BYBIT_TESTNET_API_KEY
  BYBIT_TESTNET_API_SECRET
"""

import os
import sys

try:
    from pybit.unified_trading import HTTP
except ImportError:
    print("❌ مكتبة pybit غير مثبتة. أضف 'pip install pybit' لخطوة Install dependencies بالـworkflow")
    sys.exit(1)

API_KEY = os.environ.get("BYBIT_TESTNET_API_KEY")
API_SECRET = os.environ.get("BYBIT_TESTNET_API_SECRET")


def main():
    if not API_KEY or not API_SECRET:
        print("❌ لازم تحدد BYBIT_TESTNET_API_KEY و BYBIT_TESTNET_API_SECRET كمتغيرات بيئة")
        sys.exit(1)

    print("جاري الاتصال بـ Bybit Testnet...")

    session = HTTP(
        testnet=True,
        api_key=API_KEY,
        api_secret=API_SECRET,
    )

    # اختبار 1: طلب عام بدون توقيع (public endpoint) — يتأكد بس إن الشبكة نفسها ما محظورة
    try:
        ticker = session.get_tickers(category="spot", symbol="BTCUSDT")
        price = ticker["result"]["list"][0]["lastPrice"]
        print(f"✅ نجح الاتصال بالـ API العام (public). سعر BTC/USDT الحالي على testnet: {price}")
    except Exception as e:
        print(f"❌ فشل الاتصال بالـ API العام. التفاصيل: {e}")
        print("   لو كان الخطأ متعلق بحظر جغرافي (403/451) أو timeout، فهذا يعني نفس مشكلة Binance موجودة هون كمان.")
        sys.exit(1)

    # اختبار 2: طلب موقّع (private endpoint) — يتأكد إن الـAPI Key والـSecret صحيحين وعندهم صلاحية القراءة
    try:
        balance = session.get_wallet_balance(accountType="UNIFIED")
        print("✅ نجح الاتصال بالـ API الخاص (private/authenticated). المفتاح والصلاحيات صحيحة.")
        coins = balance["result"]["list"][0].get("coin", [])
        if coins:
            print("   الأرصدة الوهمية المتوفرة بالحساب:")
            for c in coins:
                wallet_balance = c.get("walletBalance", "0")
                if float(wallet_balance or 0) > 0:
                    print(f"     - {c.get('coin')}: {wallet_balance}")
        else:
            print("   ⚠️ ما فيه رصيد وهمي بالحساب حاليًا — راجع صفحة Assets/Faucet بـ testnet.bybit.com لطلب رصيد تجريبي")
    except Exception as e:
        print(f"❌ فشل الاتصال بالـ API الخاص. التفاصيل: {e}")
        print("   تأكد إن الـAPI Key والـSecret صحيحين، وإن صلاحية Trade/Read مفعّلة على المفتاح.")
        sys.exit(1)

    print("\n🎉 الاتصال بـ Bybit Testnet يعمل بشكل كامل من GitHub Actions — بدون أي حظر جغرافي ظاهر.")


if __name__ == "__main__":
    main()
