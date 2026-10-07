package shop.youthnic.scan

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import shop.youthnic.scan.data.AuthResponse
import shop.youthnic.scan.data.Channel
import shop.youthnic.scan.data.Order
import shop.youthnic.scan.data.ScanResponse
import shop.youthnic.scan.data.User
import shop.youthnic.scan.data.VerdictType

class ModelsParsingTest {

    @Test
    fun testUserParsing() {
        val json = JSONObject("""
            {
                "id": 42,
                "username": "pawan.shukla",
                "full_name": "Pawan Shukla",
                "email": "returnorders@vbexports.co.in",
                "role": "admin",
                "must_change_password": false
            }
        """.trimIndent())

        val user = User.fromJson(json)
        assertEquals(42, user.id)
        assertEquals("pawan.shukla", user.username)
        assertEquals("Pawan Shukla", user.fullName)
        assertEquals("admin", user.role)
    }

    @Test
    fun testAuthResponseParsing() {
        val json = JSONObject("""
            {
                "user": {
                    "id": 1,
                    "username": "scanner1",
                    "full_name": "Operator One",
                    "email": "",
                    "role": "scanner",
                    "must_change_password": false
                },
                "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy_access_token",
                "refresh_token": "random_crypto_refresh_token_90_days",
                "expires_at": "2026-10-08T04:00:00Z",
                "refresh_expires_at": "2027-01-05T10:00:00Z"
            }
        """.trimIndent())

        val auth = AuthResponse.fromJson(json)
        assertEquals(1, auth.user.id)
        assertEquals("scanner1", auth.user.username)
        assertEquals("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy_access_token", auth.token)
        assertEquals("random_crypto_refresh_token_90_days", auth.refreshToken)
    }

    @Test
    fun testChannelParsing() {
        val json = JSONObject("""
            {
                "id": 101,
                "name": "VB EXPORT - Flipkart PPMP",
                "marketplace": "Flipkart PPMP",
                "company": "VB EXPORT",
                "color": "#126B4E",
                "scan_enabled": true,
                "sort_order": 1,
                "today": 125,
                "pending": 45
            }
        """.trimIndent())

        val channel = Channel.fromJson(json)
        assertEquals(101, channel.id)
        assertEquals("VB EXPORT - Flipkart PPMP", channel.name)
        assertEquals(125, channel.todayScans)
        assertEquals(45, channel.pendingScans)
    }

    @Test
    fun testScanResponseVerifiedOk() {
        val json = JSONObject("""
            {
                "severity": "success",
                "code": "OK",
                "message": "Verified",
                "scan": {
                    "id": 881,
                    "tracking": "FMPC99112233",
                    "tracking_norm": "FMPC99112233",
                    "result": "OK",
                    "flags": [],
                    "message": "Verified",
                    "scanned_at_local": "07-Oct-2026 15:30"
                },
                "order": {
                    "id": 501,
                    "channel_order_id": "OD42918374",
                    "courier": "Ekart Logistics",
                    "status_text": "Packed",
                    "status_group": "OPEN",
                    "item_count": 1,
                    "total_qty": 2,
                    "items": [
                        {
                            "sku": "KOTTY-JEANS-30",
                            "qty": 2,
                            "title": "High Rise Jeans",
                            "image_url": ""
                        }
                    ]
                }
            }
        """.trimIndent())

        val resp = ScanResponse.fromJson(json)
        assertEquals(VerdictType.OK, resp.verdictType)
        assertNotNull(resp.scan)
        assertNotNull(resp.order)
        assertEquals("FMPC99112233", resp.scan?.trackingRaw)
        assertEquals(2, resp.order?.totalQty)
    }

    @Test
    fun testScanResponseDuplicateVerdict() {
        val json = JSONObject("""
            {
                "severity": "error",
                "code": "DUPLICATE",
                "message": "DUPLICATE - already scanned on 07-Oct-2026 14:10 by Pawan Shukla (Flipkart)",
                "scan": {
                    "id": 880,
                    "tracking": "FMPC99112233",
                    "result": "OK"
                }
            }
        """.trimIndent())

        val resp = ScanResponse.fromJson(json)
        assertEquals(VerdictType.DUPLICATE, resp.verdictType)
        assertTrue(resp.message.contains("already scanned"))
    }

    @Test
    fun testScanResponseCheckVerdict() {
        val json = JSONObject("""
            {
                "severity": "warning",
                "code": "PARTIAL_CANCEL",
                "message": "Some items were cancelled - check the packet contents",
                "scan": {
                    "id": 882,
                    "tracking": "DEL123456",
                    "result": "WARN",
                    "flags": ["PARTIAL_CANCEL"]
                }
            }
        """.trimIndent())

        val resp = ScanResponse.fromJson(json)
        assertEquals(VerdictType.CHECK, resp.verdictType)
    }

    @Test
    fun testScanResponseStopVerdict() {
        val json = JSONObject("""
            {
                "severity": "error",
                "code": "CANCELLED",
                "message": "ORDER CANCELLED in OMS - do NOT dispatch, keep aside"
            }
        """.trimIndent())

        val resp = ScanResponse.fromJson(json)
        assertEquals(VerdictType.STOP, resp.verdictType)
    }

    @Test
    fun testScanResponseNotInOmsVerdict() {
        val json = JSONObject("""
            {
                "severity": "warning",
                "code": "NOT_IN_OMS",
                "message": "Not found in OMS yet - accepted as UNVERIFIED, will auto-verify on next sync"
            }
        """.trimIndent())

        val resp = ScanResponse.fromJson(json)
        assertEquals(VerdictType.NOT_IN_OMS, resp.verdictType)
    }
}
