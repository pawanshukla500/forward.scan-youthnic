package shop.youthnic.scan.util

import shop.youthnic.scan.data.VerdictType
import kotlin.math.abs
import kotlin.math.roundToInt

/**
 * What a packer sees, hears and feels after every scan - one signal per result, the same as the web scan page
 * (frontend/src/sound.ts, design-system DECISIONS.md: green OK / blue duplicate / purple not found / amber check /
 * red stop, no voice). The number of blinks matches the number of beeps, so the result is clear from a metre
 * away by colour, by count, or by the vibration alone.
 *
 *   OK         1 short high beep     1 green blink     verified - put it in the bag
 *   DUPLICATE  3 quick high beeps    3 blue blinks     already scanned - set it aside
 *   NOT_FOUND  beep-boop (high-low)  2 purple blinks   not in OMSGuru yet - saved as unverified
 *   CHECK      2 medium beeps        2 amber blinks    saved - look at the packet
 *   STOP       low buzzer            3 red blinks      not saved - put it aside
 */
enum class Cue { OK, DUPLICATE, NOT_FOUND, CHECK, STOP }

enum class Wave { SQUARE, TRIANGLE, SAWTOOTH }

data class Tone(val freqHz: Double, val startMs: Int, val durMs: Int, val wave: Wave, val gain: Double)

/** One screen blink: fully on for [onMs], then fading out and dark for [offMs]. */
data class Blink(val onMs: Int, val offMs: Int)

object ScanSignals {

    /** Overlay opacity at the peak of a blink: the camera and the card stay readable underneath. */
    const val FLASH_MAX_ALPHA = 0.62f

    private const val FADE_MS = 110
    private const val ATTACK_MS = 6.0
    private const val RELEASE_MS = 18.0

    fun cueFor(verdict: VerdictType): Cue = when (verdict) {
        VerdictType.OK -> Cue.OK
        VerdictType.DUPLICATE -> Cue.DUPLICATE
        VerdictType.NOT_IN_OMS -> Cue.NOT_FOUND
        VerdictType.CHECK -> Cue.CHECK
        VerdictType.STOP, VerdictType.ERROR -> Cue.STOP
    }

    // Every blink cycle is >= 340 ms, so never more than three flashes in one second (photosensitivity limit).
    fun blinks(cue: Cue): List<Blink> = when (cue) {
        Cue.OK -> listOf(Blink(170, 200))
        Cue.DUPLICATE -> List(3) { Blink(120, 220) }
        Cue.NOT_FOUND -> listOf(Blink(220, 200), Blink(300, 200))
        Cue.CHECK -> List(2) { Blink(150, 220) }
        Cue.STOP -> List(3) { Blink(260, 180) }
    }

    fun flashDurationMs(cue: Cue): Int = blinks(cue).sumOf { it.onMs + it.offMs }

    /** Overlay strength 0..1 at [tMs] after the scan: on, then a quick fade into the dark gap. */
    fun flashLevel(cue: Cue, tMs: Int): Float {
        if (tMs < 0) return 0f
        var start = 0
        for (b in blinks(cue)) {
            val t = tMs - start
            if (t < b.onMs) return 1f
            val fade = minOf(FADE_MS, b.offMs)
            if (t < b.onMs + fade) return 1f - (t - b.onMs).toFloat() / fade
            start += b.onMs + b.offMs
            if (tMs < start) return 0f
        }
        return 0f
    }

    /** Vibration waveform (off, on, off, on ... ms), felt in a pocket or a glove. */
    fun vibration(cue: Cue): LongArray = when (cue) {
        Cue.OK -> longArrayOf(0, 70)
        Cue.DUPLICATE -> longArrayOf(0, 70, 80, 70, 80, 70)
        Cue.NOT_FOUND -> longArrayOf(0, 180, 90, 320)
        Cue.CHECK -> longArrayOf(0, 120, 100, 120)
        Cue.STOP -> longArrayOf(0, 250, 100, 250, 100, 450)
    }

    /** Same notes as the web page (frontend/src/sound.ts), a little louder for a phone speaker in a warehouse. */
    fun tones(cue: Cue): List<Tone> = when (cue) {
        Cue.OK -> listOf(Tone(1568.0, 0, 90, Wave.SQUARE, 0.42))
        Cue.DUPLICATE -> List(3) { Tone(1319.0, it * 130, 70, Wave.SQUARE, 0.48) }
        Cue.NOT_FOUND -> listOf(Tone(988.0, 0, 200, Wave.TRIANGLE, 0.9), Tone(494.0, 240, 340, Wave.TRIANGLE, 0.9))
        Cue.CHECK -> listOf(Tone(880.0, 0, 130, Wave.SQUARE, 0.48), Tone(880.0, 200, 130, Wave.SQUARE, 0.48))
        Cue.STOP -> listOf(
            Tone(196.0, 0, 320, Wave.SAWTOOTH, 0.75),
            Tone(196.0, 420, 320, Wave.SAWTOOTH, 0.75),
            Tone(147.0, 840, 500, Wave.SAWTOOTH, 0.75)
        )
    }

    /** 16-bit mono PCM of [cue] at [sampleRate], ready for a static AudioTrack. */
    fun pcm(cue: Cue, sampleRate: Int): ShortArray {
        val tones = tones(cue)
        val endMs = tones.maxOf { it.startMs + it.durMs } + 20
        val mix = DoubleArray((endMs * sampleRate / 1000.0).roundToInt())
        for (tone in tones) {
            val first = (tone.startMs * sampleRate / 1000.0).roundToInt()
            val count = (tone.durMs * sampleRate / 1000.0).roundToInt()
            for (i in 0 until count) {
                val idx = first + i
                if (idx >= mix.size) break
                val tMs = i * 1000.0 / sampleRate
                val envelope = minOf(1.0, tMs / ATTACK_MS, (tone.durMs - tMs) / RELEASE_MS).coerceAtLeast(0.0)
                val phase = (i.toDouble() * tone.freqHz / sampleRate) % 1.0
                mix[idx] += wave(tone.wave, phase) * tone.gain * envelope
            }
        }
        return ShortArray(mix.size) { (mix[it].coerceIn(-1.0, 1.0) * Short.MAX_VALUE).roundToInt().toShort() }
    }

    private fun wave(wave: Wave, phase: Double): Double = when (wave) {
        Wave.SQUARE -> if (phase < 0.5) 1.0 else -1.0
        Wave.TRIANGLE -> 4.0 * abs(phase - 0.5) - 1.0
        Wave.SAWTOOTH -> 2.0 * phase - 1.0
    }
}
