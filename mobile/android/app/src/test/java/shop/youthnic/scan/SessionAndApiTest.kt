package shop.youthnic.scan

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import shop.youthnic.scan.data.AuthExpiredException
import shop.youthnic.scan.data.Channel
import shop.youthnic.scan.data.NetworkException

class SessionAndApiTest {

    @Test
    fun testNetworkExceptionFormatting() {
        val ex = NetworkException("No connection — scan not submitted: timeout")
        assertTrue(ex.isOffline)
        assertTrue(ex.message?.startsWith("No connection — scan not submitted") == true)
    }

    @Test
    fun testAuthExpiredException() {
        val ex = AuthExpiredException()
        assertEquals("Session expired", ex.message)
    }

    @Test
    fun testChannelSortingOrder() {
        val c1 = Channel(id = 1, name = "C1", marketplace = "", company = "", color = "", scanEnabled = true, sortOrder = 30, todayScans = 0, pendingScans = 0)
        val c2 = Channel(id = 2, name = "C2", marketplace = "", company = "", color = "", scanEnabled = true, sortOrder = 10, todayScans = 0, pendingScans = 0)
        val c3 = Channel(id = 3, name = "C3", marketplace = "", company = "", color = "", scanEnabled = true, sortOrder = 20, todayScans = 0, pendingScans = 0)

        val list = listOf(c1, c2, c3).sortedBy { it.sortOrder }
        assertEquals(2, list[0].id)
        assertEquals(3, list[1].id)
        assertEquals(1, list[2].id)
    }

    @Test
    fun testServerUrlNormalization() {
        val raw1 = "https://scan.youthnic.shop/"
        val raw2 = "https://scan.youthnic.shop///"
        assertEquals("https://scan.youthnic.shop", raw1.trim().removeSuffix("/"))
        assertEquals("https://scan.youthnic.shop", raw2.trim().replace(Regex("/+$"), ""))
    }
}
