"""
train_model.py — تجريبي فقط
يدرّب نموذج تصنيف بسيط (Random Forest) على dataset.csv الناتج من build_dataset.py،
ويعرض دقة النموذج + أهمية كل مؤشر تشخيصي، بدون أي حفظ رسمي للنموذج.

تنبيه: العدد الحالي من الصفقات (أقل من 500) صغير جدًا لنتائج موثوقة —
هذا فقط لأخذ فكرة أولية عن الاتجاه، وليس للاعتماد عليه بقرار فعلي.
"""

import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, accuracy_score
from sklearn.preprocessing import LabelEncoder

CATEGORICAL_FIELDS = ["type", "early_source", "timeframe"]  # استُبعد symbol: تجربة أولى أظهرت هيمنته (0.417) على الأداء المُحتمل تحفظًا لعملات معينة لا تعميمًا حقيقيًا
DIAGNOSTIC_FIELDS = [
    "rsi_state", "macd_bull", "bb_state", "vol_confirm",
    "ranging", "near_resistance", "obv_confirm", "htf_aligned",
]


def prepare_features(df):
    df = df.copy()

    # تشفير الحقول الفئوية (categorical) لأرقام
    encoders = {}
    for col in CATEGORICAL_FIELDS:
        if col in df.columns:
            df[col] = df[col].fillna("unknown").astype(str)
            le = LabelEncoder()
            df[col] = le.fit_transform(df[col])
            encoders[col] = le

    # الحقول التشخيصية غالبًا Boolean أو نص بسيط (rsi_state ممكن يكون -1/0/1 مثلاً)
    for col in DIAGNOSTIC_FIELDS:
        if col in df.columns:
            df[col] = df[col].fillna(0)
            if df[col].dtype == bool:
                df[col] = df[col].astype(int)
            elif df[col].dtype == object:
                le = LabelEncoder()
                df[col] = le.fit_transform(df[col].astype(str))
                encoders[col] = le

    if "score" in df.columns:
        df["score"] = df["score"].fillna(0)

    feature_cols = [c for c in DIAGNOSTIC_FIELDS + CATEGORICAL_FIELDS + ["score"] if c in df.columns]

    # إسقاط أي ميزة بتباين صفري (قيمة واحدة ثابتة بكل الصفوف) — لا تفيد النموذج ولا تُحسب بالخطأ كمهمة
    dropped = []
    kept = []
    for col in feature_cols:
        if df[col].nunique(dropna=False) <= 1:
            dropped.append(col)
        else:
            kept.append(col)

    if dropped:
        print(f"⚠️ تم إسقاط ميزات بتباين صفري (قيمة ثابتة بكل البيانات): {dropped}")

    return df, kept, encoders


def main():
    df = pd.read_csv("dataset.csv")
    print(f"عدد الصفوف: {len(df)}")

    if len(df) < 50:
        print("⚠️ العدد صغير جدًا (أقل من 50 صفقة) — النتائج ستكون غير موثوقة إطلاقًا، فقط للتجربة")

    df, feature_cols, _ = prepare_features(df)

    X = df[feature_cols]
    y = df["label"]

    # تقسيم زمني تقريبي (نفترض أن الترتيب بالملف من الأقدم للأحدث)
    split_idx = int(len(df) * 0.8)
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]

    if len(X_test) < 5:
        print("⚠️ بيانات الاختبار قليلة جدًا (أقل من 5 صفوف)، النتيجة مجرد إشارة أولية لا أكثر")

    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=5,          # عمق محدود لتفادي overfitting على عينة صغيرة
        min_samples_leaf=5,
        class_weight="balanced",  # يعوّض عدم توازن الفئات (66.9% WIN مقابل 31.2% LOSS)
        random_state=42,
    )
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    print("\n=== دقة النموذج على بيانات الاختبار ===")
    print(f"Accuracy: {accuracy_score(y_test, y_pred):.2%}")
    print(classification_report(y_test, y_pred, target_names=["LOSS", "WIN"]))

    print("=== نسبة WIN بالبيانات كاملة (baseline) ===")
    print(f"{y.mean():.2%}")

    print("\n=== أهمية كل ميزة (Feature Importance) ===")
    importance = pd.Series(model.feature_importances_, index=feature_cols).sort_values(ascending=False)
    print(importance)


if __name__ == "__main__":
    main()
