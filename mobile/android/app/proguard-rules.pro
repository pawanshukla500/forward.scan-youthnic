# Forward Scan ProGuard / R8 Rules

# Preserve app data models
-keep class shop.youthnic.scan.data.** { *; }

# OkHttp & Okio
-dontwarn okhttp3.**
-dontwarn okio.**
-keepattributes Signature
-keepattributes *Annotation*

# CameraX
-keep class androidx.camera.core.** { *; }
-dontwarn androidx.camera.core.**

# ML Kit Barcode
-keep class com.google.mlkit.vision.barcode.** { *; }
-dontwarn com.google.mlkit.vision.barcode.**
-keep class com.google.android.gms.tasks.** { *; }
-dontwarn com.google.android.gms.tasks.**
