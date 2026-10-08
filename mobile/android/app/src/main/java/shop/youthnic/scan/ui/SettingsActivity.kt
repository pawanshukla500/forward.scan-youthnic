package shop.youthnic.scan.ui

import android.content.Intent
import android.os.Bundle
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import shop.youthnic.scan.BuildConfig
import shop.youthnic.scan.ForwardScanApp
import shop.youthnic.scan.R
import shop.youthnic.scan.databinding.ActivitySettingsBinding
import shop.youthnic.scan.update.AppUpdater
import shop.youthnic.scan.util.CameraTuning

class SettingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySettingsBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val app = application as ForwardScanApp
        val sessionManager = app.sessionManager
        val apiClient = app.apiClient

        binding.btnBack.setOnClickListener {
            savePreferences()
            finish()
        }

        val user = sessionManager.getUser()
        binding.tvSettingsFullName.text = user?.fullName?.ifBlank { user.username } ?: "Forward Scan Operator"
        binding.tvSettingsRole.text = "${user?.username ?: ""} • ${user?.role ?: "scanner"}"

        binding.etStationName.setText(sessionManager.stationName)
        binding.switchSound.isChecked = sessionManager.isSoundEnabled
        binding.switchVibration.isChecked = sessionManager.isVibrationEnabled
        binding.switchFlash.isChecked = sessionManager.isFlashEnabled
        binding.switchAutoPauseCamera.text = getString(R.string.camera_auto_pause, CameraTuning.IDLE_PAUSE_MINUTES)
        binding.switchAutoPauseCamera.isChecked = sessionManager.isAutoPauseCamera
        binding.switchSharpCamera.isChecked = sessionManager.isSharpCamera
        binding.tvCameraInfo.text = sessionManager.lastCameraInfo.let {
            if (it.isBlank()) getString(R.string.camera_info_unknown) else getString(R.string.camera_info_format, it)
        }
        binding.etServerUrl.setText(sessionManager.serverUrl)
        if (!BuildConfig.DEBUG) {
            binding.etServerUrl.isEnabled = false
            binding.etServerUrl.isFocusable = false
            binding.etServerUrl.isClickable = false
            binding.tilServerUrl.helperText = getString(R.string.server_url_locked_hint)
        }

        binding.tvSettingsVersion.text = "Forward Scan v${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})"

        binding.tvInstalledVersion.text = getString(R.string.installed_version_format, BuildConfig.VERSION_NAME)
        val known = AppUpdater.available
        binding.tvUpdateStatus.text = if (known != null) {
            getString(R.string.update_available_format, known.versionName)
        } else {
            getString(R.string.update_status_idle)
        }
        binding.btnCheckUpdates.setOnClickListener { checkForUpdates() }

        if (BuildConfig.DEBUG) {
            binding.btnPreviewMode.visibility = View.VISIBLE
            binding.btnPreviewMode.setOnClickListener {
                startActivity(Intent(this, PreviewActivity::class.java))
            }
        } else {
            binding.btnPreviewMode.visibility = View.GONE
        }

        binding.btnSignOut.setOnClickListener {
            MaterialAlertDialogBuilder(this)
                .setTitle("Sign Out")
                .setMessage("Are you sure you want to sign out from this device?")
                .setPositiveButton("Sign Out") { _, _ ->
                    lifecycleScope.launch {
                        withContext(Dispatchers.IO) {
                            apiClient.logout()
                        }
                        val intent = Intent(this@SettingsActivity, LoginActivity::class.java).apply {
                            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
                        }
                        startActivity(intent)
                        finish()
                    }
                }
                .setNegativeButton("Cancel", null)
                .show()
        }
    }

    private fun checkForUpdates() {
        binding.btnCheckUpdates.isEnabled = false
        binding.tvUpdateStatus.text = getString(R.string.update_checking)
        lifecycleScope.launch {
            val result = AppUpdater.check(this@SettingsActivity, force = true)
            binding.btnCheckUpdates.isEnabled = true
            val release = result.getOrNull()
            binding.tvUpdateStatus.text = when {
                result.isFailure -> getString(R.string.update_check_failed)
                release == null -> getString(R.string.update_up_to_date)
                else -> getString(R.string.update_available_format, release.versionName)
            }
            if (release != null) AppUpdater.showUpdateDialog(this@SettingsActivity, release, always = true)
        }
    }

    override fun onResume() {
        super.onResume()
        AppUpdater.resumePendingInstall(this)
    }

    private fun savePreferences() {
        val app = application as ForwardScanApp
        val sessionManager = app.sessionManager

        sessionManager.stationName = binding.etStationName.text?.toString()?.trim().orEmpty()
        sessionManager.isSoundEnabled = binding.switchSound.isChecked
        sessionManager.isVibrationEnabled = binding.switchVibration.isChecked
        sessionManager.isFlashEnabled = binding.switchFlash.isChecked
        sessionManager.isAutoPauseCamera = binding.switchAutoPauseCamera.isChecked
        sessionManager.isSharpCamera = binding.switchSharpCamera.isChecked
        if (BuildConfig.DEBUG) {
            val url = binding.etServerUrl.text?.toString()?.trim().orEmpty()
            if (url.startsWith("http://") || url.startsWith("https://")) {
                sessionManager.serverUrl = url
            }
        }
    }

    override fun onPause() {
        super.onPause()
        savePreferences()
    }
}
