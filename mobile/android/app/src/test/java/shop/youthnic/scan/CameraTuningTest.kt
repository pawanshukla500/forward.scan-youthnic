package shop.youthnic.scan

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import shop.youthnic.scan.data.DuplicateGuard
import shop.youthnic.scan.util.CameraTuning

class CameraTuningTest {

    @Test
    fun cameraIsCappedAt720pUnless1080pIsChosen() {
        assertEquals(1280 to 720, CameraTuning.analysisSize(sharp = false))
        assertEquals(1920 to 1080, CameraTuning.analysisSize(sharp = true))
        assertEquals(1280 to 720, CameraTuning.PREVIEW)
        // Landscape sensor orientation, as CameraX ResolutionStrategy expects.
        assertTrue(CameraTuning.HD.first > CameraTuning.HD.second)
        assertTrue(CameraTuning.FULL_HD.first > CameraTuning.FULL_HD.second)
    }

    @Test
    fun barcodeReadingIsThrottled() {
        assertTrue(CameraTuning.ANALYZE_EVERY_MS >= 80)
        assertEquals(CameraTuning.IDLE_PAUSE_MINUTES * 60_000L, CameraTuning.IDLE_PAUSE_MS)
    }

    @Test
    fun fpsRangeTypicalSamsung() {
        val ranges = listOf(15 to 15, 24 to 24, 10 to 30, 15 to 30, 30 to 30)
        assertEquals(24 to 24, CameraTuning.pickFpsRange(ranges))
    }

    @Test
    fun fpsRangePrefersVariableUnderTheCap() {
        val ranges = listOf(15 to 15, 15 to 24, 24 to 24, 15 to 30, 30 to 30, 7 to 30)
        assertEquals(15 to 24, CameraTuning.pickFpsRange(ranges))
    }

    @Test
    fun fpsRangeLowEndPhone() {
        val ranges = listOf(5 to 15, 15 to 15, 5 to 20, 20 to 20, 5 to 30, 30 to 30, 15 to 30)
        assertEquals(20 to 20, CameraTuning.pickFpsRange(ranges))
    }

    @Test
    fun fpsRangeLeftAloneWhenNothingFits() {
        assertNull(CameraTuning.pickFpsRange(listOf(15 to 30, 30 to 30, 7 to 30)))
        assertNull(CameraTuning.pickFpsRange(emptyList()))
        // Too slow on top, or too long exposures at the bottom: not used.
        assertNull(CameraTuning.pickFpsRange(listOf(5 to 10, 8 to 12, 5 to 24)))
    }

    @Test
    fun describeShowsLandscapeSizeAndFps() {
        assertEquals("1280×720 · up to 24 fps", CameraTuning.describe(720, 1280, 24 to 24))
        assertEquals("1920×1080", CameraTuning.describe(1920, 1080, null))
    }

    @Test
    fun duplicateGuardCooldownWindow() {
        val guard = DuplicateGuard(sameCodeSuppressMs = 8000L, anyCodeCooldownMs = 2200L)
        val t0 = 100_000L
        assertFalse("nothing scanned yet", guard.inCooldown(t0))
        guard.markEmitted("AWB1111111", t0)
        assertTrue(guard.inCooldown(t0 + 500))
        assertTrue(guard.inCooldown(t0 + 2199))
        assertFalse(guard.inCooldown(t0 + 2200))
        assertTrue("a different code after the cooldown still scans", guard.shouldProcess("AWB2222222", t0 + 2300))
    }
}
