package shop.youthnic.scan.util

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.media.ToneGenerator
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import shop.youthnic.scan.data.SessionManager

/**
 * Beep + vibration for every scan result (see [ScanSignals] for the full signal table).
 *
 * The beeps are synthesised once and played from static AudioTracks on the media stream, like a handheld
 * scanner: the notification stream used before is muted whenever the phone is on silent / vibrate.
 * ToneGenerator stays as a fallback for phones where an AudioTrack cannot be created.
 */
class SoundManager(
    private val context: Context,
    private val sessionManager: SessionManager
) {
    private val tracks = mutableMapOf<Cue, AudioTrack?>()
    private var fallbackTones: ToneGenerator? = null

    private val vibrator: Vibrator? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
        val vm = context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager
        vm?.defaultVibrator
    } else {
        @Suppress("DEPRECATION")
        context.getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
    }

    /** Builds the beeps up front (a few ms) so the first scan is not late. */
    fun prepare() {
        Cue.values().forEach { cue -> tracks.getOrPut(cue) { buildTrack(cue) } }
    }

    fun play(cue: Cue) {
        if (sessionManager.isSoundEnabled) beep(cue)
        if (sessionManager.isVibrationEnabled) vibratePattern(ScanSignals.vibration(cue))
    }

    private fun beep(cue: Cue) {
        // A new result cuts off the previous one: the packer always hears the latest scan.
        tracks.values.forEach { t -> runCatching { if (t?.playState == AudioTrack.PLAYSTATE_PLAYING) t.stop() } }
        val track = tracks.getOrPut(cue) { buildTrack(cue) }
        val played = track != null && runCatching {
            track.stop()
            track.reloadStaticData()
            track.play()
        }.isSuccess
        if (!played) fallbackBeep(cue)
    }

    private fun buildTrack(cue: Cue): AudioTrack? = try {
        val pcm = ScanSignals.pcm(cue, SAMPLE_RATE)
        val bytes = pcm.size * 2
        val attributes = AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_MEDIA)
            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
            .build()
        val format = AudioFormat.Builder()
            .setSampleRate(SAMPLE_RATE)
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
            .build()
        val track = AudioTrack.Builder()
            .setAudioAttributes(attributes)
            .setAudioFormat(format)
            .setTransferMode(AudioTrack.MODE_STATIC)
            .setBufferSizeInBytes(bytes)
            .build()
        if (track.write(pcm, 0, pcm.size) != pcm.size || track.state != AudioTrack.STATE_INITIALIZED) {
            track.release()
            null
        } else {
            track
        }
    } catch (_: Exception) {
        null
    }

    private fun fallbackBeep(cue: Cue) {
        val tones = fallbackTones ?: runCatching { ToneGenerator(AudioManager.STREAM_MUSIC, 90) }.getOrNull()
            ?.also { fallbackTones = it } ?: return
        runCatching {
            when (cue) {
                Cue.OK -> tones.startTone(ToneGenerator.TONE_PROP_BEEP, 100)
                Cue.DUPLICATE -> tones.startTone(ToneGenerator.TONE_CDMA_ALERT_CALL_GUARD, 400)
                Cue.NOT_FOUND -> tones.startTone(ToneGenerator.TONE_PROP_PROMPT, 400)
                Cue.CHECK -> tones.startTone(ToneGenerator.TONE_PROP_BEEP2, 300)
                Cue.STOP -> tones.startTone(ToneGenerator.TONE_SUP_ERROR, 800)
            }
        }
    }

    private fun vibratePattern(timings: LongArray) {
        if (vibrator == null || !vibrator.hasVibrator()) return
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                vibrator.vibrate(VibrationEffect.createWaveform(timings, -1))
            } else {
                @Suppress("DEPRECATION")
                vibrator.vibrate(timings, -1)
            }
        } catch (_: Exception) {}
    }

    fun release() {
        tracks.values.forEach { runCatching { it?.release() } }
        tracks.clear()
        fallbackTones?.release()
        fallbackTones = null
    }

    private companion object {
        const val SAMPLE_RATE = 22050
    }
}
