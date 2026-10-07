package shop.youthnic.scan.ui

import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import shop.youthnic.scan.ForwardScanApp
import shop.youthnic.scan.data.Channel
import shop.youthnic.scan.databinding.ActivityChannelBinding
import shop.youthnic.scan.databinding.ItemChannelCardBinding

class ChannelActivity : AppCompatActivity() {

    private lateinit var binding: ActivityChannelBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityChannelBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val app = application as ForwardScanApp
        val sessionManager = app.sessionManager

        binding.btnSettings.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }

        binding.cardContinueLast.setOnClickListener {
            val lastId = sessionManager.lastChannelId
            val lastName = sessionManager.lastChannelName
            if (lastId != -1) {
                openScanner(lastId, lastName, "#126B4E")
            }
        }
    }

    override fun onResume() {
        super.onResume()
        updateHeader()
        loadChannels()
    }

    private fun updateHeader() {
        val app = application as ForwardScanApp
        val sessionManager = app.sessionManager
        val user = sessionManager.getUser()

        val name = user?.fullName?.ifBlank { user.username } ?: "Packer"
        val station = sessionManager.stationName
        binding.tvUserInfo.text = if (station.isNotBlank()) "$name • $station" else name

        val lastId = sessionManager.lastChannelId
        val lastName = sessionManager.lastChannelName
        if (lastId != -1 && lastName.isNotBlank()) {
            binding.cardContinueLast.visibility = View.VISIBLE
            binding.tvContinueLastChannelName.text = "Continue $lastName"
        } else {
            binding.cardContinueLast.visibility = View.GONE
        }
    }

    private fun loadChannels() {
        val app = application as ForwardScanApp
        val apiClient = app.apiClient

        binding.progressChannels.visibility = View.VISIBLE

        lifecycleScope.launch {
            val result = withContext(Dispatchers.IO) {
                apiClient.getChannels()
            }

            binding.progressChannels.visibility = View.GONE

            if (result.isSuccess) {
                val channels = result.getOrNull().orEmpty()
                renderChannels(channels)
            } else {
                if (binding.channelsContainer.childCount == 0) {
                    binding.tvEmptyChannels.visibility = View.VISIBLE
                    binding.tvEmptyChannels.text = result.exceptionOrNull()?.message ?: "Unable to load channels"
                }
            }
        }
    }

    private fun renderChannels(channels: List<Channel>) {
        binding.channelsContainer.removeAllViews()

        if (channels.isEmpty()) {
            binding.tvEmptyChannels.visibility = View.VISIBLE
            return
        }

        binding.tvEmptyChannels.visibility = View.GONE

        val inflater = LayoutInflater.from(this)
        for (channel in channels) {
            val itemBinding = ItemChannelCardBinding.inflate(inflater, binding.channelsContainer, false)

            itemBinding.tvChannelName.text = channel.name
            itemBinding.tvChannelSubtitle.text = "${channel.marketplace} • ${channel.todayScans} scanned today"
            itemBinding.tvPendingBadge.text = "${channel.pendingScans} pending"

            try {
                if (channel.color.isNotBlank()) {
                    val colorInt = Color.parseColor(channel.color)
                    itemBinding.viewColorMarker.setBackgroundColor(colorInt)
                }
            } catch (_: Exception) {}

            itemBinding.root.setOnClickListener {
                val sessionManager = (application as ForwardScanApp).sessionManager
                sessionManager.lastChannelId = channel.id
                sessionManager.lastChannelName = channel.name
                openScanner(channel.id, channel.name, channel.color)
            }

            binding.channelsContainer.addView(itemBinding.root)
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
}
