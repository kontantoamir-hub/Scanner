plugins {
    id("com.android.application") version "8.5.2"
    id("org.jetbrains.kotlin.android") version "2.0.20"
    id("org.jetbrains.kotlin.plugin.compose") version "2.0.20"
}

android {
    namespace = "com.scanner.dashboard"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.scanner.dashboard"
        minSdk = 26
        targetSdk = 34
        versionCode = 3
        versionName = "3.0"
    }

    // مفتاح توقيع ثابت: يسمح بتحديث التطبيق فوق النسخة القديمة بدون حذفها.
    // إذا لم يوجد ملف debug.keystore يُستخدم المفتاح الافتراضي (وعندها يلزم حذف النسخة القديمة قبل التثبيت).
    val ks = file("debug.keystore")
    if (ks.exists()) {
        signingConfigs.getByName("debug") {
            storeFile = ks
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
    }

    buildFeatures { compose = true }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }

    sourceSets["main"].manifest.srcFile("AndroidManifest.xml")
    sourceSets["main"].java.srcDirs("kotlin")
    sourceSets["main"].res.srcDirs("res")
}

dependencies {
    implementation(platform("androidx.compose:compose-bom:2024.09.00"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
}
