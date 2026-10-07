package shop.youthnic.scan.util

import android.content.Context
import android.media.AudioManager
import android.media.ToneGenerator
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import shop.youthnic.scan.data.SessionManager
import shop.youthnic.scan.data.VerdictType

class SoundManager(
    private val context: Context,
    private val sessionManager: SessionManager
) {
    private var toneGenerator: ToneGenerator? = null
    private val vibrator: Vibrator? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
        val vm = context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager
        vm?.defaultVibrator
    } else {
        @Suppress("DEPRECATION")
        context.getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
    }

    init {
        try {
            toneGenerator = ToneGenerator(AudioManager.STREAM_NOTIFICATION, 80)
        } catch (_: Exception) {
            toneGenerator = null
        }
    }

    fun playFeedback(verdict: VerdictType) {
        val soundOn = sessionManager.isSoundEnabled
        val vibOn = sessionManager.isVibrationEnabled

        when (verdict) {
            VerdictType.OK -> {
                if (soundOn) toneGenerator?.startTone(ToneGenerator.TONE_PROP_BEEP, 100)
                if (vibOn) vibratePattern(longArrayOf(0, 60))
            }
            VerdictType.CHECK -> {
                if (soundOn) toneGenerator?.startTone(ToneGenerator.TONE_PROP_BEEP2, 200)
                if (vibOn) vibratePattern(longArrayOf(0, 120, 80, 120))
            }
            VerdictType.DUPLICATE -> {
                if (soundOn) toneGenerator?.startTone(ToneGenerator.TONE_CDMA_ALERT_CALL_GUARD, 250)
                if (vibOn) vibratePattern(longArrayOf(0, 80, 80, 80, 80, 80))
            }
            VerdictType.STOP -> {
                if (soundOn) toneGenerator?.startTone(ToneGenerator.TONE_SUP_ERROR, 500)
                // Matching web app: [200, 100, 200, 100, 400]
                if (vibOn) vibratePattern(longArrayOf(0, 200, 100, 200, 100, 400))
            }
            VerdictType.NOT_IN_OMS -> {
                if (soundOn) toneGenerator?.startTone(ToneGenerator.TONE_PROP_PROMPT, 300)
                if (vibOn) vibratePattern(longArrayOf(0, 150, 100, 200))
            }
            VerdictType.ERROR -> {
                if (soundOn) toneGenerator?.startTone(ToneGenerator.TONE_SUP_ERROR, 400)
                if (vibOn) vibratePattern(longArrayOf(0, 300))
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
        toneGenerator?.release()
        toneGenerator = null
    }
}
