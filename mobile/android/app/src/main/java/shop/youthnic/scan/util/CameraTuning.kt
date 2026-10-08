package shop.youthnic.scan.util

/**
 * Battery limits for the always-open scanner camera (ScannerActivity). Where the battery went before:
 * CameraX picked the preview size itself (often 1080p+), ML Kit read every frame back-to-back (one CPU core
 * busy all the time), the sensor ran at up to 30 fps, and the camera never stopped while the scan screen
 * was open - also behind dialogs and when nobody was scanning.
 *
 * Sizes are width x height in the camera sensor's landscape orientation, as CameraX ResolutionStrategy expects.
 * Kept free of Android classes so the unit tests can run on the JVM.
 */
object CameraTuning {

    /** Default for preview and barcode reading: a 1-D AWB barcode reads easily at 720p. */
    val HD = 1280 to 720

    /** Settings -> "Sharper camera (1080p)": small / far / faded barcodes, costs more battery. */
    val FULL_HD = 1920 to 1080

    /** The preview fills about a third of the screen; more than 720p only costs power. */
    val PREVIEW = HD

    /** ML Kit reads at most ~10 frames a second (it ran on every frame before); a barcode still reads in < 0.2 s. */
    const val ANALYZE_EVERY_MS = 100L

    /** No scan and no touch for this long -> the camera closes until the packer taps "Tap to scan". */
    const val IDLE_PAUSE_MS = 2 * 60_000L
    const val IDLE_PAUSE_MINUTES = 2

    /** The sensor may run at most this fast (from the frame-rate ranges the phone supports). */
    const val MAX_FPS = 24
    const val MIN_UPPER_FPS = 15
    const val MIN_LOWER_FPS = 10

    fun analysisSize(sharp: Boolean): Pair<Int, Int> = if (sharp) FULL_HD else HD

    /**
     * The phone's auto-exposure frame-rate range to use, or null to leave the phone's default.
     * Highest top speed within [MIN_UPPER_FPS]..[MAX_FPS] (smooth enough to aim), then the lowest bottom speed
     * that is still >= [MIN_LOWER_FPS], so dim corners of the warehouse get as much light as with the default
     * (which also goes down to ~15 fps) without very long, blurry exposures.
     */
    fun pickFpsRange(ranges: List<Pair<Int, Int>>): Pair<Int, Int>? =
        ranges
            .filter { (lower, upper) -> upper in MIN_UPPER_FPS..MAX_FPS && lower >= MIN_LOWER_FPS && lower <= upper }
            .sortedWith(compareByDescending<Pair<Int, Int>> { it.second }.thenBy { it.first })
            .firstOrNull()

    /** "1280×720 · up to 24 fps" for Settings (what this phone's camera actually runs at). */
    fun describe(width: Int, height: Int, fps: Pair<Int, Int>?): String {
        val long = maxOf(width, height)
        val short = minOf(width, height)
        return if (fps != null) "$long×$short · up to ${fps.second} fps" else "$long×$short"
    }
}
