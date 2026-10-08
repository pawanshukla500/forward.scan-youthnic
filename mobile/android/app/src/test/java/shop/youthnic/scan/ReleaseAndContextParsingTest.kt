package shop.youthnic.scan

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import shop.youthnic.scan.data.AppRelease
import shop.youthnic.scan.data.Channel
import shop.youthnic.scan.data.ScanContext

/** Parsing of the server answers behind the Pending button, the picker counts and the update check. */
class ReleaseAndContextParsingTest {

    @Test
    fun releaseIsParsedAndComparedByVersionCode() {
        val json = JSONObject(
            """
            {"available": true, "version_code": 10021, "version_name": "1.0.21", "notes": "Pending list",
             "size": 2754827, "sha256": "abc", "published_at": "2026-10-07T10:00:00Z", "min_version_code": 0,
             "download_url": "https://scan.youthnic.shop/download/forward-scan.apk",
             "page_url": "https://scan.youthnic.shop/download", "qr_url": "https://scan.youthnic.shop/download/qr.svg"}
            """.trimIndent()
        )
        val release = AppRelease.fromJson(json)
        assertNotNull(release)
        release!!
        assertEquals(10021, release.versionCode)
        assertEquals("1.0.21", release.versionName)
        assertEquals(2754827L, release.sizeBytes)
        assertTrue(release.isNewerThan(10016))
        assertFalse(release.isNewerThan(10021))
        assertFalse(release.isNewerThan(10030))
        assertFalse(release.isRequiredFor(10016))  // min_version_code 0: optional update
    }

    @Test
    fun requiredUpdateWhenBelowMinimum() {
        val json = JSONObject(
            """{"available": true, "version_code": 10030, "version_name": "1.0.30", "min_version_code": 10030,
               "download_url": "https://x/download/forward-scan.apk", "page_url": "https://x/download"}"""
        )
        val release = AppRelease.fromJson(json)!!
        assertTrue(release.isRequiredFor(10021))
        assertFalse(release.isRequiredFor(10030))
    }

    @Test
    fun nothingPublishedMeansNoRelease() {
        assertNull(AppRelease.fromJson(JSONObject("""{"available": false, "page_url": "https://x/download"}""")))
        assertNull(AppRelease.fromJson(JSONObject("""{"available": true, "version_code": 0, "download_url": ""}""")))
    }

    @Test
    fun scanContextGivesHeaderNumbersAndPendingQueue() {
        val json = JSONObject(
            """
            {"date": "2026-10-07",
             "stats": {"scanned": 4, "ok": 3, "flagged": 1},
             "awb": {"generated": 178, "scanned": 4, "pending": 174, "left_unscanned": 0, "cancelled": 0, "overdue": 12, "pct": 2},
             "queue": [
               {"awb": "SF123", "order_id": "OD1", "courier": "Delhivery", "skus": 2, "units": 3,
                "sla_date": null, "awb_generated_at": "2026-10-06T08:00:00Z", "age_days": 1, "priority": "Urgent"},
               {"awb": "SF124", "order_id": "OD2", "courier": "", "skus": 1, "units": 1,
                "sla_date": "2026-10-07T12:30:00Z", "awb_generated_at": "2026-10-07T05:00:00Z", "age_days": 0, "priority": "High"}
             ],
             "queue_total": 186}
            """.trimIndent()
        )
        val ctx = ScanContext.fromJson(json)
        assertEquals(4, ctx.scannedToday)
        assertEquals(174, ctx.awb.pending)
        assertEquals(12, ctx.awb.overdue)
        assertEquals(2, ctx.awb.pct)
        assertEquals(186, ctx.pendingTotal)
        assertEquals(2, ctx.queue.size)
        assertEquals("SF123", ctx.queue[0].awb)
        assertEquals("", ctx.queue[0].slaDate)
        assertEquals(1, ctx.queue[0].ageDays)
        assertEquals("High", ctx.queue[1].priority)
    }

    @Test
    fun noAwbTodayMeansNoPercentage() {
        val ctx = ScanContext.fromJson(
            JSONObject("""{"stats": {"scanned": 0}, "awb": {"generated": 0, "pending": 0, "overdue": 0, "pct": null}, "queue": [], "queue_total": 0}""")
        )
        assertNull(ctx.awb.pct)
        assertEquals(0, ctx.pendingTotal)
    }

    @Test
    fun pickerPendingIsTodayPlusOverdue() {
        val withCounts = Channel.fromJson(
            JSONObject(
                """{"id": 3, "name": "Tulip Prints - Meesho", "marketplace": "Meesho", "today": 4, "pending": 190,
                   "awb_today": {"generated": 178, "scanned": 4, "pending": 174, "overdue": 12, "cancelled": 0, "pct": 2}}"""
            )
        )
        assertEquals(186, withCounts.pendingTotal)

        val olderServer = Channel.fromJson(JSONObject("""{"id": 4, "name": "Old", "today": 1, "pending": 7}"""))
        assertNull(olderServer.awbToday)
        assertEquals(7, olderServer.pendingTotal)
    }

    @Test
    fun notFoundScansAreCountedApart() {
        val ctx = ScanContext.fromJson(
            JSONObject("""{"stats": {"scanned": 120, "not_found": 3}, "awb": {"generated": 200, "pending": 80, "overdue": 0, "pct": 60}, "queue": [], "queue_total": 80}""")
        )
        assertEquals(120, ctx.scannedToday)
        assertEquals(3, ctx.notFoundToday)
        // an older server without the field
        assertEquals(0, ScanContext.fromJson(JSONObject("""{"stats": {"scanned": 5}, "awb": {}, "queue": []}""")).notFoundToday)
    }
}
