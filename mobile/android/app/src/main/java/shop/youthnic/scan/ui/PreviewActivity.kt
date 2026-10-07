package shop.youthnic.scan.ui

import android.graphics.Color
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import shop.youthnic.scan.BuildConfig
import shop.youthnic.scan.R
import shop.youthnic.scan.databinding.ActivityPreviewBinding

class PreviewActivity : AppCompatActivity() {

    private lateinit var binding: ActivityPreviewBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!BuildConfig.DEBUG) {
            finish()
            return
        }

        binding = ActivityPreviewBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.btnBack.setOnClickListener { finish() }

        // Setup mock scanner preview header
        val sc = binding.previewScannerLayout
        sc.btnBack.visibility = View.GONE
        sc.tvScannerChannelName.text = "Flipkart PPMP (Preview Mode)"
        sc.viewChannelColor.setBackgroundColor(Color.parseColor("#126B4E"))
        sc.tvProgressScanned.text = "Scanned: 142"
        sc.tvProgressPending.text = "68 pending"
        sc.progressBarScans.progress = 67

        binding.btnStateIdle.setOnClickListener { showIdleState() }
        binding.btnStateOk.setOnClickListener { showOkState() }
        binding.btnStateCheck.setOnClickListener { showCheckState() }
        binding.btnStateStop.setOnClickListener { showStopState() }
        binding.btnStateDuplicate.setOnClickListener { showDuplicateState() }
        binding.btnStateOffline.setOnClickListener { showOfflineState() }

        showOkState()
    }

    private fun showIdleState() {
        val sc = binding.previewScannerLayout
        sc.cardIdleState.visibility = View.VISIBLE
        sc.cardResult.visibility = View.GONE
        sc.tvConnectionStatus.text = "Online"
        sc.tvConnectionStatus.setTextColor(ContextCompat.getColor(this, R.color.verdict_ok))
        sc.tvConnectionStatus.setBackgroundColor(ContextCompat.getColor(this, R.color.verdict_ok_bg))
    }

    private fun showOkState() {
        val sc = binding.previewScannerLayout
        sc.cardIdleState.visibility = View.GONE
        sc.cardResult.visibility = View.VISIBLE

        val green = ContextCompat.getColor(this, R.color.verdict_ok)
        sc.bannerVerdict.setBackgroundColor(green)
        sc.cardResult.strokeColor = green

        sc.tvVerdictTitle.text = getString(R.string.verdict_verified)
        sc.tvVerdictMessage.text = "Verified — place in dispatch container"
        sc.tvResultAwb.text = "FMPP0982341278"
        sc.tvResultOrderId.text = "OD4291837461928"
        sc.tvResultItemsCount.text = "1 SKU (2 Units)"

        sc.layoutItemsList.removeAllViews()
        val tv = TextView(this).apply {
            text = "• KOTTY-REG-JEANS-30 (x2) Women Solid Regular High Rise Jeans"
            textSize = 12f
            setTextColor(ContextCompat.getColor(context, R.color.text_secondary))
        }
        sc.layoutItemsList.addView(tv)

        sc.tvConnectionStatus.text = "Online"
        sc.tvConnectionStatus.setTextColor(green)
        sc.tvConnectionStatus.setBackgroundColor(ContextCompat.getColor(this, R.color.verdict_ok_bg))
    }

    private fun showCheckState() {
        val sc = binding.previewScannerLayout
        sc.cardIdleState.visibility = View.GONE
        sc.cardResult.visibility = View.VISIBLE

        val amber = ContextCompat.getColor(this, R.color.verdict_check)
        sc.bannerVerdict.setBackgroundColor(amber)
        sc.cardResult.strokeColor = amber

        sc.tvVerdictTitle.text = getString(R.string.verdict_check)
        sc.tvVerdictMessage.text = "What to check: Partial cancellation — 1 of 2 items cancelled in OMS"
        sc.tvResultAwb.text = "DELHIVERY89213894"
        sc.tvResultOrderId.text = "MYN-98439128"
        sc.tvResultItemsCount.text = "2 SKUs (1 Remaining Unit)"

        sc.layoutItemsList.removeAllViews()
        val tv1 = TextView(this).apply {
            text = "• KOTTY-TOP-BLK-M (x1) Women Solid Top [CANCELLED]"
            textSize = 12f
            setTextColor(ContextCompat.getColor(context, R.color.verdict_stop))
        }
        val tv2 = TextView(this).apply {
            text = "• KOTTY-JEANS-BLU-28 (x1) Regular Jeans [ACTIVE]"
            textSize = 12f
            setTextColor(ContextCompat.getColor(context, R.color.text_secondary))
        }
        sc.layoutItemsList.addView(tv1)
        sc.layoutItemsList.addView(tv2)
    }

    private fun showStopState() {
        val sc = binding.previewScannerLayout
        sc.cardIdleState.visibility = View.GONE
        sc.cardResult.visibility = View.VISIBLE

        val red = ContextCompat.getColor(this, R.color.verdict_stop)
        sc.bannerVerdict.setBackgroundColor(red)
        sc.cardResult.strokeColor = red

        sc.tvVerdictTitle.text = getString(R.string.verdict_stop)
        sc.tvVerdictMessage.text = "WRONG MARKETPLACE - this shipment belongs to Amazon Direct"
        sc.tvResultAwb.text = "AMZ789234128"
        sc.tvResultOrderId.text = "402-9812471-82910"
        sc.tvResultItemsCount.text = "-"
        sc.layoutItemsList.removeAllViews()
    }

    private fun showDuplicateState() {
        val sc = binding.previewScannerLayout
        sc.cardIdleState.visibility = View.GONE
        sc.cardResult.visibility = View.VISIBLE

        val orange = ContextCompat.getColor(this, R.color.verdict_duplicate)
        sc.bannerVerdict.setBackgroundColor(orange)
        sc.cardResult.strokeColor = orange

        sc.tvVerdictTitle.text = getString(R.string.verdict_duplicate)
        sc.tvVerdictMessage.text = "DUPLICATE - already scanned at 14:22 by Pawan Shukla (Flipkart PPMP)"
        sc.tvResultAwb.text = "FMPC88992211"
        sc.tvResultOrderId.text = "OD11223344"
        sc.tvResultItemsCount.text = "1 SKU (1 Unit)"
        sc.layoutItemsList.removeAllViews()
    }

    private fun showOfflineState() {
        val sc = binding.previewScannerLayout
        sc.cardIdleState.visibility = View.GONE
        sc.cardResult.visibility = View.VISIBLE

        val red = ContextCompat.getColor(this, R.color.verdict_stop)
        sc.bannerVerdict.setBackgroundColor(red)
        sc.cardResult.strokeColor = red

        sc.tvVerdictTitle.text = "OFFLINE"
        sc.tvVerdictMessage.text = getString(R.string.connection_offline)
        sc.tvResultAwb.text = "ECOM99112233"
        sc.tvResultOrderId.text = "Not submitted"
        sc.tvResultItemsCount.text = "Check connection and rescan"
        sc.layoutItemsList.removeAllViews()

        sc.tvConnectionStatus.text = "Offline"
        sc.tvConnectionStatus.setTextColor(red)
        sc.tvConnectionStatus.setBackgroundColor(ContextCompat.getColor(this, R.color.verdict_stop_bg))
    }
}
