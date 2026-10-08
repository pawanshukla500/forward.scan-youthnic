package shop.youthnic.scan

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import shop.youthnic.scan.data.VerdictType
import shop.youthnic.scan.util.Cue
import shop.youthnic.scan.util.ScanSignals
import kotlin.math.abs

class ScanSignalsTest {

    @Test
    fun everyVerdictHasItsOwnSignal() {
        assertEquals(Cue.OK, ScanSignals.cueFor(VerdictType.OK))
        assertEquals(Cue.DUPLICATE, ScanSignals.cueFor(VerdictType.DUPLICATE))
        assertEquals(Cue.NOT_FOUND, ScanSignals.cueFor(VerdictType.NOT_IN_OMS))
        assertEquals(Cue.CHECK, ScanSignals.cueFor(VerdictType.CHECK))
        assertEquals(Cue.STOP, ScanSignals.cueFor(VerdictType.STOP))
        assertEquals(Cue.STOP, ScanSignals.cueFor(VerdictType.ERROR))
    }

    @Test
    fun blinksMatchBeeps() {
        assertEquals(1, ScanSignals.blinks(Cue.OK).size)
        assertEquals(3, ScanSignals.blinks(Cue.DUPLICATE).size)
        assertEquals(2, ScanSignals.blinks(Cue.NOT_FOUND).size)
        assertEquals(2, ScanSignals.blinks(Cue.CHECK).size)
        assertEquals(3, ScanSignals.blinks(Cue.STOP).size)
        for (cue in Cue.values()) {
            assertEquals("$cue: one blink per beep", ScanSignals.tones(cue).size, ScanSignals.blinks(cue).size)
        }
    }

    @Test
    fun neverMoreThanThreeFlashesPerSecond() {
        for (cue in Cue.values()) {
            for (b in ScanSignals.blinks(cue)) {
                assertTrue("$cue blink cycle ${b.onMs + b.offMs} ms", b.onMs + b.offMs >= 334)
            }
        }
    }

    @Test
    fun flashStartsOnAndEndsDark() {
        for (cue in Cue.values()) {
            val total = ScanSignals.flashDurationMs(cue)
            assertEquals(1f, ScanSignals.flashLevel(cue, 0), 0f)
            assertEquals(0f, ScanSignals.flashLevel(cue, -5), 0f)
            assertEquals(0f, ScanSignals.flashLevel(cue, total), 0f)
            for (t in 0..total step 5) {
                val level = ScanSignals.flashLevel(cue, t)
                assertTrue("$cue level $level at $t ms", level in 0f..1f)
            }
        }
        // Duplicate: on, dark gap, on again - three separate blinks.
        assertEquals(0f, ScanSignals.flashLevel(Cue.DUPLICATE, 300), 0f)
        assertEquals(1f, ScanSignals.flashLevel(Cue.DUPLICATE, 350), 0f)
        assertEquals(1f, ScanSignals.flashLevel(Cue.DUPLICATE, 690), 0f)
    }

    @Test
    fun signalsAreDistinct() {
        val vibrations = Cue.values().map { ScanSignals.vibration(it).toList() }.toSet()
        assertEquals(Cue.values().size, vibrations.size)
        val tones = Cue.values().map { ScanSignals.tones(it) }.toSet()
        assertEquals(Cue.values().size, tones.size)
    }

    @Test
    fun pcmIsAudibleAndUnclipped() {
        val rate = 22050
        for (cue in Cue.values()) {
            val pcm = ScanSignals.pcm(cue, rate)
            val endMs = ScanSignals.tones(cue).maxOf { it.startMs + it.durMs } + 20
            assertTrue("$cue length", abs(pcm.size - endMs * rate / 1000) <= 1)
            val peak = pcm.maxOf { abs(it.toInt()) }
            assertTrue("$cue peak $peak", peak > Short.MAX_VALUE / 4)
            assertEquals("$cue starts silent (no click)", 0, pcm.first().toInt())
        }
        // The buzzer is the longest sound, the OK beep the shortest.
        val lengths = Cue.values().associateWith { ScanSignals.pcm(it, rate).size }
        assertEquals(Cue.STOP, lengths.maxByOrNull { it.value }!!.key)
        assertEquals(Cue.OK, lengths.minByOrNull { it.value }!!.key)
    }
}
