package shop.youthnic.scan.ui

import android.content.Intent
import android.os.Bundle
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import shop.youthnic.scan.ForwardScanApp
import shop.youthnic.scan.R
import shop.youthnic.scan.databinding.ActivityChangePasswordBinding

class ChangePasswordActivity : AppCompatActivity() {

    private lateinit var binding: ActivityChangePasswordBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityChangePasswordBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val app = application as ForwardScanApp
        val sessionManager = app.sessionManager
        val apiClient = app.apiClient

        binding.btnChangePassword.setOnClickListener {
            val currentPassword = binding.etCurrentPassword.text?.toString().orEmpty()
            val newPassword = binding.etNewPassword.text?.toString().orEmpty()
            val confirmPassword = binding.etConfirmPassword.text?.toString().orEmpty()

            if (currentPassword.isBlank() || newPassword.isBlank() || confirmPassword.isBlank()) {
                showError(getString(R.string.error_all_fields_required))
                return@setOnClickListener
            }

            if (newPassword.length < 6) {
                showError(getString(R.string.error_password_too_short))
                return@setOnClickListener
            }

            if (newPassword == currentPassword) {
                showError(getString(R.string.error_password_same))
                return@setOnClickListener
            }

            if (newPassword != confirmPassword) {
                showError(getString(R.string.error_password_mismatch))
                return@setOnClickListener
            }

            hideError()
            setLoading(true)

            lifecycleScope.launch {
                val changeResult = withContext(Dispatchers.IO) {
                    apiClient.changePassword(currentPassword, newPassword)
                }

                if (changeResult.isSuccess) {
                    val user = sessionManager.getUser()
                    val username = user?.username.orEmpty()

                    // Establish fresh mobile session with updated credentials
                    val loginResult = withContext(Dispatchers.IO) {
                        apiClient.login(username, newPassword)
                    }

                    setLoading(false)

                    // Clear fields from memory
                    binding.etCurrentPassword.text?.clear()
                    binding.etNewPassword.text?.clear()
                    binding.etConfirmPassword.text?.clear()

                    if (loginResult.isSuccess) {
                        val intent = Intent(this@ChangePasswordActivity, ChannelActivity::class.java).apply {
                            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
                        }
                        startActivity(intent)
                        finish()
                    } else {
                        val intent = Intent(this@ChangePasswordActivity, LoginActivity::class.java).apply {
                            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
                        }
                        startActivity(intent)
                        finish()
                    }
                } else {
                    setLoading(false)
                    val msg = changeResult.exceptionOrNull()?.message ?: "Failed to change password"
                    showError(msg)
                }
            }
        }

        binding.btnSignOut.setOnClickListener {
            lifecycleScope.launch {
                withContext(Dispatchers.IO) {
                    apiClient.logout()
                }
                val intent = Intent(this@ChangePasswordActivity, LoginActivity::class.java).apply {
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
                }
                startActivity(intent)
                finish()
            }
        }
    }

    private fun setLoading(loading: Boolean) {
        binding.btnChangePassword.isEnabled = !loading
        binding.btnSignOut.isEnabled = !loading
        binding.etCurrentPassword.isEnabled = !loading
        binding.etNewPassword.isEnabled = !loading
        binding.etConfirmPassword.isEnabled = !loading
        binding.progressChangePassword.visibility = if (loading) View.VISIBLE else View.GONE
        binding.btnChangePassword.text = if (loading) getString(R.string.updating_password) else getString(R.string.update_password_button)
    }

    private fun showError(msg: String) {
        binding.tvError.text = msg
        binding.tvError.visibility = View.VISIBLE
    }

    private fun hideError() {
        binding.tvError.visibility = View.GONE
    }
}
