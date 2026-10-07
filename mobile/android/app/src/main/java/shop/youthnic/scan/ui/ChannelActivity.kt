package shop.youthnic.scan.ui

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import shop.youthnic.scan.ForwardScanApp
import shop.youthnic.scan.R
import shop.youthnic.scan.data.AppRelease
import shop.youthnic.scan.data.AuthExpiredException
import shop.youthnic.scan.data.Channel
import shop.youthnic.scan.databinding.ActivityChannelBinding
import shop.youthnic.scan.databinding.ItemChannelCardBinding
import shop.youthnic.scan.update.AppUpdater
import shop.youthnic.scan.util.Ui

class ChannelActivity : AppCompatActivity() {

    private lateinit var binding: ActivityChannelBinding
    private var loading = false

    private val notificationPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) AppUpdater.available?.let { AppUpdater.notifyOnce(this, it) }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityChannelBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.btnSettings.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
        binding.btnRefresh.setOnClickListener { loadChannels() }

        binding.cardContinueLast.setOnClickListener {
            val sm = (application as ForwardScanApp).sessionManager
            if (sm.lastChannelId != -1) {
                openScanner(sm.lastChannelId, sm.lastChannelName, sm.lastChannelColor)
            }
        }

        askNotificationPermissionOnce()
    }

    override fun onResume() {
        super.onResume()
        updateHeader()
        loadChannels()
        checkForUpdate()
        AppUpdater.resumePendingInstall(this)
    }

    /** Android 13+: ask once so update notifications can be shown (the in-app banner works either way). */
    private fun askNotificationPermissionOnce() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) return
        val sm = (application as ForwardScanApp).sessionManager
        if (sm.askedNotificationPermission) return
        sm.askedNotificationPermission = true
        notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
    }

    private fun checkForUpdate() {
        showUpdateCard(AppUpdater.available)
        lifecycleScope.launch {
            val release = AppUpdater.check(this@ChannelActivity).getOrNull()
            showUpdateCard(release)
            if (release != null) AppUpdater.showUpdateDialog(this@ChannelActivity, release)
        }
    }

    private fun showUpdateCard(release: AppRelease?) {
        if (release == null) {
            binding.cardUpdate.visibility = View.GONE
            return
        }
        binding.tvUpdateTitle.text = getString(R.string.update_banner_title, release.versionName)
        binding.cardUpdate.visibility = View.VISIBLE
        binding.cardUpdate.setOnClickListener { AppUpdater.startUpdate(this, release) }
    }

    private fun updateHeader() {
        val sm = (application as ForwardScanApp).sessionManager
        val user = sm.getUser()

        val name = user?.fullName?.ifBlank { user.username } ?: "Packer"
        val station = sm.stationName
        binding.tvUserInfo.text = if (station.isNotBlank()) "$name · $station" else name

        val lastId = sm.lastChannelId
        val lastName = sm.lastChannelName
        if (lastId != -1 && lastName.isNotBlank()) {
            binding.cardContinueLast.visibility = View.VISIBLE
            binding.tvContinueLastChannelName.text = getString(R.string.continue_last_channel, lastName)
        } else {
            binding.cardContinueLast.visibility = View.GONE
        }
    }

    private fun loadChannels() {
        if (loading) return
        loading = true
        val apiClient = (application as ForwardScanApp).apiClient
        binding.progressChannels.visibility = View.VISIBLE

        lifecycleScope.launch {
            val result = withContext(Dispatchers.IO) { apiClient.getChannels() }
            loading = false
            binding.progressChannels.visibility = View.INVISIBLE

            if (result.isSuccess) {
                renderChannels(result.getOrNull().orEmpty())
            } else {
                val ex = result.exceptionOrNull()
                if (ex is AuthExpiredException) {
                    goToLogin()
                    return@launch
                }
                if (binding.channelsContainer.childCount == 0) {
                    binding.tvEmptyChannels.visibility = View.VISIBLE
                    binding.tvEmptyChannels.text = ex?.message ?: "Unable to load marketplaces"
                }
            }
        }
    }

    private fun renderChannels(channels: List<Channel>) {
        binding.channelsContainer.removeAllViews()

        if (channels.isEmpty()) {
            binding.tvEmptyChannels.text = getString(R.string.no_channels)
            binding.tvEmptyChannels.visibility = View.VISIBLE
            binding.tvTotalPending.text = ""
            return
        }
        binding.tvEmptyChannels.visibility = View.GONE

        val total = channels.sumOf { it.pendingTotal }
        binding.tvTotalPending.text = if (total > 0) getString(R.string.total_pending_format, Ui.count(total)) else ""

        val brand = ContextCompat.getColor(this, R.color.colorPrimary)
        val inflater = LayoutInflater.from(this)
        for (channel in channels) {
            val item = ItemChannelCardBinding.inflate(inflater, binding.channelsContainer, false)

            item.tvChannelName.text = channel.name
            val market = channel.marketplace.ifBlank { channel.company }
            val scanned = getString(R.string.today_scans_format_s, Ui.count(channel.todayScans))
            item.tvChannelSubtitle.text = if (market.isNotBlank()) "$market · $scanned" else scanned
            Ui.tint(item.viewColorMarker, Ui.parseColor(channel.color, brand))

            val pending = channel.pendingTotal
            if (pending > 0) {
                item.tvPendingCount.text = Ui.count(pending)
                item.tvPendingLabel.text = getString(R.string.pending_label)
                Ui.pillRes(item.tvPendingCount, R.color.verdict_check, R.color.verdict_check_bg)
            } else {
                item.tvPendingCount.text = "0"
                item.tvPendingLabel.text = getString(R.string.all_done)
                Ui.pillRes(item.tvPendingCount, R.color.verdict_ok, R.color.verdict_ok_bg)
            }

            val awb = channel.awbToday
            val overdue = awb?.overdue ?: 0
            if (overdue > 0) {
                item.tvChannelOverdue.text = getString(R.string.overdue_format, Ui.count(overdue))
                item.tvChannelOverdue.visibility = View.VISIBLE
            } else {
                item.tvChannelOverdue.visibility = View.GONE
            }
            if (awb != null && awb.generated > 0) {
                item.progressChannelToday.progress = awb.pct ?: 0
                item.progressChannelToday.visibility = View.VISIBLE
            } else {
                item.progressChannelToday.visibility = View.GONE
            }

            item.root.contentDescription = "${channel.name}, ${Ui.count(pending)} pending"
            item.root.setOnClickListener {
                val sm = (application as ForwardScanApp).sessionManager
                sm.lastChannelId = channel.id
                sm.lastChannelName = channel.name
                sm.lastChannelColor = channel.color
                openScanner(channel.id, channel.name, channel.color)
            }

            binding.channelsContainer.addView(item.root)
        }
    }

    private fun openScanner(channelId: Int, channelName: String, channelColor: String) {
        val intent = Intent(this, ScannerActivity::class.java).apply {
            putExtra(ScannerActivity.EXTRA_CHANNEL_ID, channelId)
            putExtra(ScannerActivity.EXTRA_CHANNEL_NAME, channelName)
            putExtra(ScannerActivity.EXTRA_CHANNEL_COLOR, channelColor)
        }
        startActivity(intent)
    }

    private fun goToLogin() {
        startActivity(Intent(this, LoginActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
        })
        finish()
    }
}
