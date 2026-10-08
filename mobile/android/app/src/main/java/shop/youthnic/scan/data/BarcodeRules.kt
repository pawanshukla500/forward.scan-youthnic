package shop.youthnic.scan.data

import com.google.mlkit.vision.barcode.common.Barcode

object BarcodeRules {

    private val IGNORED_PREFIXES = listOf(
        "http:", "https:", "www.", "upi:", "mailto:", "tel:",
        "smsto:", "sms:", "geo:", "wifi:", "begin:", "mecard", "vcard"
    )

    private val ALLOWED_1D_FORMATS = setOf(
        Barcode.FORMAT_CODE_128,
        Barcode.FORMAT_CODE_39,
        Barcode.FORMAT_CODE_93,
        Barcode.FORMAT_CODABAR,
        Barcode.FORMAT_ITF,
        Barcode.FORMAT_EAN_13,
        Barcode.FORMAT_EAN_8,
        Barcode.FORMAT_UPC_A,
        Barcode.FORMAT_UPC_E
    )

    private val REJECTED_2D_FORMATS = setOf(
        Barcode.FORMAT_QR_CODE,
        Barcode.FORMAT_DATA_MATRIX,
        Barcode.FORMAT_PDF417,
        Barcode.FORMAT_AZTEC
    )

    /**
     * Rejects square QR / 2D / URL codes that appear on labels, matching backend looks_like_qr
     * and frontend CameraScanner.tsx.
     */
    fun looksLikeQr(raw: String): Boolean {
        val v = raw.trim()
        if (v.isEmpty()) return true
        if (v.any { it.isWhitespace() }) return true
        val low = v.lowercase()
        if (low.contains("://")) return true
        if (IGNORED_PREFIXES.any { low.startsWith(it) }) return true
        if (v.length > 60) return true

        val norm = normalizeTracking(v)
        if (norm.length < 6 || norm.length > 40) return true
        val normUpper = norm.uppercase()
        if (listOf("HTTP", "WWW", "UPI", "VCARD", "MECARD", "WIFI").any { normUpper.startsWith(it) }) {
            return true
        }
        return false
    }

    fun isAllowed1DFormat(format: Int): Boolean {
        if (format in REJECTED_2D_FORMATS) return false
        return format in ALLOWED_1D_FORMATS
    }

    fun normalizeTracking(raw: String): String {
        return raw.trim().replace(Regex("[^A-Za-z0-9_-]"), "").uppercase()
    }
}

class DuplicateGuard(
    private val sameCodeSuppressMs: Long = 8000L,
    private val anyCodeCooldownMs: Long = 2200L
) {
    private var lastCode: String = ""
    private var lastSeenAt: Long = 0L
    private var lastEmittedAt: Long = 0L

    @Synchronized
    fun shouldProcess(code: String, now: Long = System.currentTimeMillis()): Boolean {
        val clean = code.trim()
        if (clean.isEmpty()) return false

        if (clean == lastCode) {
            val stale = (now - lastSeenAt) > sameCodeSuppressMs
            lastSeenAt = now
            if (!stale) return false
        }

        if (now >= lastEmittedAt && now - lastEmittedAt < anyCodeCooldownMs) {
            return false  // (a clock that jumped back is not a cooldown)
        }

        return true
    }

    /** Right after a scan every code is ignored anyway, so the camera skips reading frames meanwhile. */
    @Synchronized
    fun inCooldown(now: Long = System.currentTimeMillis()): Boolean =
        lastEmittedAt > 0L && now >= lastEmittedAt && now - lastEmittedAt < anyCodeCooldownMs

    /** The send of [code] failed: aiming at it again sends it again (no 8 s wait). */
    @Synchronized
    fun forget(code: String) {
        if (lastCode == code.trim()) {
            lastCode = ""
            lastSeenAt = 0L
        }
    }

    @Synchronized
    fun markEmitted(code: String, now: Long = System.currentTimeMillis()) {
        val clean = code.trim()
        lastCode = clean
        lastSeenAt = now
        lastEmittedAt = now
    }

    @Synchronized
    fun reset() {
        lastCode = ""
        lastSeenAt = 0L
        lastEmittedAt = 0L
    }
}
