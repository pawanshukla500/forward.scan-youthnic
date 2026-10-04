import type { CapacitorConfig } from "@capacitor/cli";

// Installed Android app wrapper (APK built by .github/workflows/mobile-apk.yml).
// The bundled web UI talks to the server via VITE_API_URL + Bearer token (see src/api.ts),
// so no cookies or same-origin assumptions are needed inside the WebView.
const config: CapacitorConfig = {
  appId: "shop.youthnic.scan",
  appName: "Forward Scan",
  webDir: "dist",
};

export default config;
