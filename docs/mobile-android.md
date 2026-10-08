# Forward Scan - Native Android Application

## Overview

The Forward Scan Android application is a lightweight native application located under `mobile/android/`. It is designed specifically for warehouse operations on inexpensive Android devices with ~2 GB RAM, replacing the legacy WebView/Capacitor bundle while keeping the React 18 web application for desktop/admin operations and retaining all server-side scan evaluation and OMS business rules.

Package ID: `shop.youthnic.scan`

---

## 1. Architecture

- **Language:** Kotlin (1.9.24)
- **UI Toolkit:** Android XML Views + ViewBinding
- **Architecture Pattern:** Clean Activity-based architecture with lightweight state management
- **Camera & Scanning:** Jetpack CameraX (1.3.4) + Google Play Services unbundled ML Kit Barcode Scanning (18.3.1)
- **Networking:** OkHttp (4.12.0) with automatic 401 token refresh retry and JSON deserialization via Android's built-in `org.json`
- **Crypto & Security:** Hardware-backed `AndroidKeyStore` AES-256-GCM encryption for persistent refresh tokens
- **Target OS:** `minSdk 23` (Android 6.0 Marshmallow) to `targetSdk 34` (Android 14)

---

## 2. Mobile Screens

The production mobile app contains only 5 primary operational screens:

1. **Startup / Session Restoration (`SplashActivity`)**
   - Automatically checks stored cryptographic session on app launch.
   - Refreshes the short-lived access token or calls `/api/auth/me` without requiring re-login.
   - Survives app process kills and device reboots.

2. **Login Screen (`LoginActivity`)**
   - Simple warehouse login: Forward Scan logo/title, username/email, password, sign-in button.
   - Zero marketing banners, zero large hero artwork.
   - Passwords are never saved locally.

3. **Marketplace / Channel Selection (`ChannelActivity`)**
   - Minimal header with signed-in employee name, station name, and settings shortcut.
   - Prominent "Continue [Last Used Marketplace]" quick action.
   - Large vertical marketplace cards displaying today's scan progress and pending counts.

4. **Continuous Scanner Screen (`ScannerActivity`)**
   - Portrait-first warehouse layout:
     - Top: Channel name, connection state (Online/Offline), scanned/pending progress bar, back button.
     - Middle (45% screen height): Always-open CameraX preview with high-contrast scanning reticle. Camera does NOT repeatedly open and close per scan.
     - Result area: Unmistakable color-coded verdicts:
       - **GREEN:** Verified / OK (`#15803D`)
       - **AMBER:** Check (`#B45309`) with clear "What to check" reason
       - **RED:** Stop / Rejected (`#B91C1C`) (cancelled, wrong marketplace, blocked)
       - **ORANGE/RED:** Duplicate scan (`#C2410C`)
       - **AMBER:** Not found in OMS yet (`#D97706`)
     - Details: AWB (monospace bold), Order ID, Marketplace, Courier, SKUs & Units.
     - Bottom bar: Torch toggle, Manual entry button, Recent scans drawer.

5. **Settings Screen (`SettingsActivity`)**
   - Operator information (name, username, role).
   - Packing station identifier (e.g. "Station 1", saved locally).
   - Operational cues: Audio toggle, Vibration toggle.
   - Server URL configuration.
   - Version & build info.
   - Sign-out action (revokes device session server-side and clears cryptographic keystore).

---

## 3. Authentication & Session Lifecycle

### Backend Flow:
- `POST /api/auth/mobile/login`: Authenticates credentials and returns a short-lived access JWT plus a 90-day cryptographically random refresh token.
- `POST /api/auth/mobile/refresh`: Validates refresh token hash, rotates the refresh token (revoking the previous one), and issues a fresh access token.
- `POST /api/auth/mobile/logout`: Explicitly revokes the device session server-side.

### Invalidation Triggers:
A device refresh session becomes immediately invalid if:
1. The user account is deactivated (`is_active = False`).
2. The password is changed or reset (user's `token_version` increments).
3. The session is explicitly revoked on logout or via admin.
4. The 90-day expiry window passes without activity.

### Device Keystore Security:
- The refresh token is encrypted with an AES-256-GCM key generated inside the hardware-backed `AndroidKeyStore`.
- Only the encrypted ciphertext and initialization vector (IV) are persisted in `SharedPreferences`.
- Harmless preferences (station name, sound, vibration, last channel) are stored in standard preferences.

---

## 4. Camera & Scanner Performance

- **CameraX ImageAnalysis:** Configured with `STRATEGY_KEEP_ONLY_LATEST`; preview and analysis capped at `1280x720` with a `ResolutionSelector` (closest size at or below; `1920x1080` only with Settings -> *Sharper camera*). Limits live in `util/CameraTuning.kt`.
- **Battery:** ML Kit reads at most one frame every 100 ms (not during a scan request, the 2.2 s cooldown or while a dialog/sheet covers the camera); the sensor runs at most ~24 fps (picked from the phone's own AE frame-rate ranges, dropped automatically if the phone reports a camera error); SurfaceView (`PERFORMANCE`) preview; the camera closes after 2 minutes without a scan or touch (*Tap to scan* reopens it, and it reopens when the phone wakes); the screen is kept on only while the camera is open. Settings shows the size and fps the camera actually runs at.
- **Memory Safety:** Every `ImageProxy` instance is guaranteed closed in a `try/finally` or `addOnCompleteListener` block.
- **Format Filtering:** Restricts ML Kit recognition to 1-D barcode formats (`Code128`, `Code39`, `Code93`, `Codabar`, `ITF`, `EAN13`, `EAN8`, `UPC-A`, `UPC-E`).
- **2-D Rejection:** Explicitly rejects QR, DataMatrix, PDF417, Aztec and URLs via `BarcodeRules.looksLikeQr`.
- **Duplicate Guard:** Suppresses the same barcode held in view for 8,000 ms; enforces a 2,200 ms cooldown between distinct reads.
- **In-Flight Lock:** Scan submissions are locked while a network request is in progress to prevent double submissions.

---

## 5. Network & Offline Behavior

- Authoritative scan verification always takes place on the backend.
- If network connection fails:
  - The app displays `"No connection — scan not submitted"` in red.
  - The scanned AWB remains populated for immediate retry.
  - The shipment is never falsely marked as OK or recorded offline.

---

## 6. Audio and Vibration Feedback

- Built using native Android `ToneGenerator` and `Vibrator` with zero audio assets bundled.
- **OK:** Short high beep (`TONE_PROP_BEEP`) + 60ms vibration.
- **CHECK:** Double medium beep (`TONE_PROP_BEEP2`) + double vibration.
- **STOP:** Low buzzer (`TONE_SUP_ERROR`) + triple vibration pattern.
- **DUPLICATE:** Fast alert bursts + pulse vibration.
- Can be disabled in Settings.

---

## 7. Build Steps & Release Signing

### Local Build:
```bash
cd mobile/android
./gradlew testDebugUnitTest
./gradlew assembleDebug
```

### Monotonic Versioning:
- `versionCode` uses `10000 + GITHUB_RUN_NUMBER`.
- Guarantees seamless update installation over any existing phone installations.

### Signing Compatibility:
- Builds are signed using the fixed repository keystore supplied via GitHub Secret: `ANDROID_DEBUG_KEYSTORE_B64` (`alias: androiddebugkey`, `storepass: android`).
- Release CI validates the SHA-256 certificate fingerprint using `apksigner verify --print-certs`.

### APK Size Budget:
- **Target:** <= 15 MB
- **Hard Limit:** <= 20 MB
- R8 minification and resource shrinking enabled (`minifyEnabled true`, `shrinkResources true`).
- Unbundled ML Kit ensures total APK size stays within ~5 to 10 MB.

---

## 8. Debug-Only Preview Mode

In debug builds (`BuildConfig.DEBUG = true`), developers can open the **UI Preview Mode** from Settings to inspect all UI states with realistic Forward Scan mock data without connecting to a live backend:
- Idle State
- OK (Green)
- Check (Amber)
- Stop (Red)
- Duplicate (Orange)
- Offline Error (Red)

In release builds, this screen is completely stripped and hidden.
