package shop.youthnic.scan.util

import android.content.Context
import android.content.res.ColorStateList
import android.graphics.Color
import android.view.View
import android.widget.TextView
import androidx.annotation.ColorInt
import androidx.annotation.ColorRes
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import java.text.NumberFormat
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import kotlin.math.roundToInt

/** Small view helpers shared by the screens. */
object Ui {

    private val indian = Locale("en", "IN")

    /**
     * Colours a shaped background (bg_pill, bg_marker, bg_round_card) without losing its rounded corners.
     * Never use setBackgroundColor on those views: it replaces the shape with a plain rectangle.
     */
    fun tint(view: View, @ColorInt color: Int) {
        ViewCompat.setBackgroundTintList(view, ColorStateList.valueOf(color))
    }

    fun pill(view: TextView, @ColorInt text: Int, @ColorInt background: Int) {
        view.setTextColor(text)
        tint(view, background)
    }

    fun pillRes(view: TextView, @ColorRes text: Int, @ColorRes background: Int) {
        val c = view.context
        pill(view, ContextCompat.getColor(c, text), ContextCompat.getColor(c, background))
    }

    @ColorInt
    fun parseColor(hex: String?, @ColorInt fallback: Int): Int {
        if (hex.isNullOrBlank()) return fallback
        return try {
            Color.parseColor(hex.trim())
        } catch (_: IllegalArgumentException) {
            fallback
        }
    }

    fun dp(context: Context, value: Int): Int = (value * context.resources.displayMetrics.density).roundToInt()

    /** 1,23,456 style counts, like the web app. */
    fun count(n: Int): String = NumberFormat.getIntegerInstance(indian).format(n)

    fun megabytes(bytes: Long): String = String.format(Locale.US, "%.1f MB", bytes / 1048576.0)

    /** The server's ISO UTC timestamps ("2026-10-07T10:00:00.123Z"), or null. */
    fun parseIsoUtc(iso: String?): Date? {
        if (iso.isNullOrBlank() || iso.length < 19) return null
        return try {
            SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US).apply {
                timeZone = TimeZone.getTimeZone("UTC")
            }.parse(iso.substring(0, 19))
        } catch (_: Exception) {
            null
        }
    }

    /** "7 Oct, 5:30 pm" in the phone's time zone. */
    fun shortDateTime(date: Date): String = SimpleDateFormat("d MMM, h:mm a", indian).format(date)

    fun shortDate(date: Date): String = SimpleDateFormat("d MMM", indian).format(date)
}
