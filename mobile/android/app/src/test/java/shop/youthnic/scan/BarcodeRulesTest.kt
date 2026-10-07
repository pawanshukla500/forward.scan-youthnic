package shop.youthnic.scan

import com.google.mlkit.vision.barcode.common.Barcode
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import shop.youthnic.scan.data.BarcodeRules
import shop.youthnic.scan.data.DuplicateGuard

class BarcodeRulesTest {

    @Test
    fun testValidAwbBarcodesPassQrCheck() {
        assertFalse(BarcodeRules.looksLikeQr("FMPP0982341278"))
        assertFalse(BarcodeRules.looksLikeQr("DELHIVERY12345678"))
        assertFalse(BarcodeRules.looksLikeQr("143245678901"))
        assertFalse(BarcodeRules.looksLikeQr("ECOM9911223344"))
        assertFalse(BarcodeRules.looksLikeQr("BLUEDART-88990011"))
    }

    @Test
    fun testQrAndUrlsAreRejected() {
        assertTrue(BarcodeRules.looksLikeQr("https://scan.youthnic.shop/api"))
        assertTrue(BarcodeRules.looksLikeQr("http://example.com"))
        assertTrue(BarcodeRules.looksLikeQr("upi://pay?pa=shop@upi&pn=Store"))
        assertTrue(BarcodeRules.looksLikeQr("WIFI:S:MyNetwork;T:WPA;P:secret;;"))
        assertTrue(BarcodeRules.looksLikeQr("VCARD:FN:John Doe;TEL:1234567890;;"))
        assertTrue(BarcodeRules.looksLikeQr("hello world with spaces"))
        assertTrue(BarcodeRules.looksLikeQr(""))
        assertTrue(BarcodeRules.looksLikeQr("   "))
    }

    @Test
    fun testLengthBounds() {
        // Too short (<6 chars)
        assertTrue(BarcodeRules.looksLikeQr("123"))
        assertTrue(BarcodeRules.looksLikeQr("AWB"))

        // Too long (>60 chars)
        val veryLong = "A".repeat(65)
        assertTrue(BarcodeRules.looksLikeQr(veryLong))
    }

    @Test
    fun testBarcodeFormatsFilter() {
        // 1-D Formats allowed
        assertTrue(BarcodeRules.isAllowed1DFormat(Barcode.FORMAT_CODE_128))
        assertTrue(BarcodeRules.isAllowed1DFormat(Barcode.FORMAT_CODE_39))
        assertTrue(BarcodeRules.isAllowed1DFormat(Barcode.FORMAT_CODE_93))
        assertTrue(BarcodeRules.isAllowed1DFormat(Barcode.FORMAT_CODABAR))
        assertTrue(BarcodeRules.isAllowed1DFormat(Barcode.FORMAT_ITF))
        assertTrue(BarcodeRules.isAllowed1DFormat(Barcode.FORMAT_EAN_13))
        assertTrue(BarcodeRules.isAllowed1DFormat(Barcode.FORMAT_EAN_8))
        assertTrue(BarcodeRules.isAllowed1DFormat(Barcode.FORMAT_UPC_A))
        assertTrue(BarcodeRules.isAllowed1DFormat(Barcode.FORMAT_UPC_E))

        // 2-D Formats strictly rejected
        assertFalse(BarcodeRules.isAllowed1DFormat(Barcode.FORMAT_QR_CODE))
        assertFalse(BarcodeRules.isAllowed1DFormat(Barcode.FORMAT_DATA_MATRIX))
        assertFalse(BarcodeRules.isAllowed1DFormat(Barcode.FORMAT_PDF417))
        assertFalse(BarcodeRules.isAllowed1DFormat(Barcode.FORMAT_AZTEC))
    }

    @Test
    fun testDuplicateGuardSameCodeSuppression() {
        val guard = DuplicateGuard(sameCodeSuppressMs = 8000L, anyCodeCooldownMs = 2200L)
        val awb = "FMPP12345678"
        val t0 = 100000L

        // First scan allowed
        assertTrue(guard.shouldProcess(awb, t0))
        guard.markEmitted(awb, t0)

        // Rescanned immediately (500ms later): suppressed
        assertFalse(guard.shouldProcess(awb, t0 + 500))

        // Rescanned 3 seconds later: still suppressed (< 8000ms)
        assertFalse(guard.shouldProcess(awb, t0 + 3000))

        // Rescanned 8.5 seconds later: allowed
        assertTrue(guard.shouldProcess(awb, t0 + 8500))
    }

    @Test
    fun testDuplicateGuardAnyCodeCooldown() {
        val guard = DuplicateGuard(sameCodeSuppressMs = 8000L, anyCodeCooldownMs = 2200L)
        val awb1 = "AWB1111111"
        val awb2 = "AWB2222222"
        val t0 = 100000L

        // First scan allowed
        assertTrue(guard.shouldProcess(awb1, t0))
        guard.markEmitted(awb1, t0)

        // Different barcode right after (500ms): cooldown blocked
        assertFalse(guard.shouldProcess(awb2, t0 + 500))

        // Different barcode after cooldown (2500ms): allowed
        assertTrue(guard.shouldProcess(awb2, t0 + 2500))
    }
}
