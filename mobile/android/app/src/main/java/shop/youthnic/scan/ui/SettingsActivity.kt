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
        binding.etServerUrl.setText(sessionManager.serverUrl)
        if (!BuildConfig.DEBUG) {
            binding.etServerUrl.isEnabled = false
            binding.etServerUrl.isFocusable = false
            binding.etServerUrl.isClickable = false
            binding.tilServerUrl.helperText = getString(R.string.server_url_locked_hint)
        }

        binding.tvSettingsVersion.text = "Forward Scan v${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})"

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

    private fun savePreferences() {
        val app = application as ForwardScanApp
        val sessionManager = app.sessionManager

        sessionManager.stationName = binding.etStationName.text?.toString()?.trim().orEmpty()
        sessionManager.isSoundEnabled = binding.switchSound.isChecked
        sessionManager.isVibrationEnabled = binding.switchVibration.isChecked
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
