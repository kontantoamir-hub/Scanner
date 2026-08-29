"""
train_model.py — تجريبي فقط
يدرّب نموذج تصنيف بسيط (Random Forest) على dataset.csv الناتج من build_dataset.py،
ويعرض دقة النموذج + أهمية كل مؤشر تشخيصي، بدون أي حفظ رسمي للنموذج.

تعديلات هذه النسخة:
- فصل التدريب حسب نوع الصفقة (--type all/official/early) لأن سلوك الرسمية والمبكرة مختلف جدًا
- استبدال التقسيم الواحد 80/20 بـ TimeSeriesSplit (cross-validation زمني) لتقليل تأثير الحظ
  بعينة صغيرة، مع الحفاظ على الترتيب الزمني بكل طية (fold)
- إبقاء هولدأوت نهائي (آخر 20% من البيانات) كـ"اختبار تقدّمي" (forward-test) منفصل عن الـCV،
  يُقيَّم مرة واحدة فقط بعد اختيار الإعدادات، وليس جزءًا من عملية الاختيار نفسها
- إضافة market_regime كـfeature (لو موجود بـdataset.csv) — محاولة لتفسير التذبذب الكبير
  بالدقة بين الطيات اللي ظهر بالتجربة الأولى (احتمال أن قواعد النجاح تختلف حسب حالة السوق)
- مقارنة صريحة بين class_weight="balanced" وبدونه بمرحلة الهولدأوت، لأن "balanced" اشتبهنا
  إنه يخرّب precision فئة WIN النادرة (خصوصًا بالرسمية) بدل ما يفيد

تنبيه: العدد الحالي من الصفقات صغير نسبيًا خصوصًا بعد الفصل حسب النوع —
هذا فقط لأخذ فكرة أولية عن الاتجاه، وليس للاعتماد عليه بقرار فعلي.
"""

import argparse

import pandas as pd
from sklearn.model_selection import TimeSeriesSplit
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, accuracy_score
from sklearn.preprocessing import LabelEncoder

# استُبعد symbol: تجربة أولى أظهرت هيمنته (0.417) على الأداء المُحتمل تحفظًا لعملات معينة لا تعميمًا حقيقيًا
CATEGORICAL_FIELDS = ["type", "early_source", "timeframe", "market_regime"]
DIAGNOSTIC_FIELDS = [
    "rsi_state", "macd_bull", "bb_state", "vol_confirm",
    "ranging", "near_resistance", "obv_confirm", "htf_aligned",
]


def prepare_features(df, drop_type_column):
    df = df.copy()

    categorical_fields = [c for c in CATEGORICAL_FIELDS if not (drop_type_column and c == "type")]

    # تشفير الحقول الفئوية (categorical) لأرقام
    encoders = {}
    for col in categorical_fields:
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

    feature_cols = [c for c in DIAGNOSTIC_FIELDS + categorical_fields + ["score"] if c in df.columns]

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


def make_model(balanced):
    return RandomForestClassifier(
        n_estimators=200,
        max_depth=5,          # عمق محدود لتفادي overfitting على عينة صغيرة
        min_samples_leaf=5,
        class_weight="balanced" if balanced else None,
        random_state=42,
    )


def run_time_series_cv(X, y, balanced, n_splits=5):
    """Cross-validation زمني: كل طية تدرّب على الماضي وتختبر على المستقبل مباشرة بعده."""
    n_splits = min(n_splits, max(2, len(X) // 30))  # يمنع طيات صغيرة جدًا على عينات قليلة
    tscv = TimeSeriesSplit(n_splits=n_splits)

    accuracies = []
    label = "balanced" if balanced else "غير موزون"
    print(f"\n=== Cross-Validation زمني ({n_splits} طيات) — class_weight={label} ===")
    for fold, (train_idx, test_idx) in enumerate(tscv.split(X), start=1):
        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]

        model = make_model(balanced)
        model.fit(X_train, y_train)
        y_pred = model.predict(X_test)
        acc = accuracy_score(y_test, y_pred)
        accuracies.append(acc)
        print(f"  طية {fold}: تدريب={len(X_train)} صفقة، اختبار={len(X_test)} صفقة، دقة={acc:.2%}")

    print(f"  متوسط الدقة عبر الطيات: {sum(accuracies)/len(accuracies):.2%}")
    return accuracies


def run_holdout_test(X, y, feature_cols, balanced):
    """هولدأوت نهائي: آخر 20% من البيانات، يُقيَّم مرة واحدة فقط."""
    split_idx = int(len(X) * 0.8)
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]

    if len(X_test) < 5:
        print("⚠️ بيانات الاختبار قليلة جدًا (أقل من 5 صفوف)، النتيجة مجرد إشارة أولية لا أكثر")

    model = make_model(balanced)
    model.fit(X_train, y_train)
    y_pred = model.predict(X_test)

    label = "balanced" if balanced else "غير موزون"
    print(f"\n=== هولدأوت نهائي (آخر 20% زمنيًا) — class_weight={label} ===")
    print(f"Accuracy: {accuracy_score(y_test, y_pred):.2%}")
    print(classification_report(y_test, y_pred, target_names=["LOSS", "WIN"]))

    if not balanced:
        print("=== نسبة WIN بالبيانات كاملة (baseline) ===")
        print(f"{y.mean():.2%}")

        print("\n=== أهمية كل ميزة (Feature Importance) ===")
        importance = pd.Series(model.feature_importances_, index=feature_cols).sort_values(ascending=False)
        print(importance)


def run_for_subset(df, label, drop_type_column):
    print(f"\n{'='*50}\nالفئة: {label} — عدد الصفقات: {len(df)}\n{'='*50}")

    if len(df) < 50:
        print("⚠️ العدد صغير جدًا (أقل من 50 صفقة) — النتائج ستكون غير موثوقة إطلاقًا، فقط للتجربة")
        return

    df, feature_cols, _ = prepare_features(df, drop_type_column=drop_type_column)
    X = df[feature_cols]
    y = df["label"]

    # نقارن بالطريقتين: balanced (كانت الافتراضية سابقًا) وبدون موازنة
    for balanced in (True, False):
        run_time_series_cv(X, y, balanced=balanced)
        run_holdout_test(X, y, feature_cols, balanced=balanced)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["all", "official", "early", "breakout", "by_type"],
                         default="by_type",
                         help="all=تدريب موحد بكل الأنواع، by_type=تدريب منفصل لكل نوع (افتراضي)، "
                              "أو حدد نوعًا واحدًا مباشرة")
    args = parser.parse_args()

    df = pd.read_csv("dataset.csv")
    print(f"عدد الصفوف الكلي: {len(df)}")

    if args.type == "all":
        run_for_subset(df, "الكل (نوع كـfeature)", drop_type_column=False)
    elif args.type == "by_type":
        for t in df["type"].unique():
            run_for_subset(df[df["type"] == t], t, drop_type_column=True)
    else:
        run_for_subset(df[df["type"] == args.type], args.type, drop_type_column=True)


if __name__ == "__main__":
    main()
