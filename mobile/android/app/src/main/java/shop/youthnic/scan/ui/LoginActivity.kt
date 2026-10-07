package shop.youthnic.scan.ui

import android.content.Intent
import android.os.Bundle
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import shop.youthnic.scan.BuildConfig
import shop.youthnic.scan.ForwardScanApp
import shop.youthnic.scan.R
import shop.youthnic.scan.databinding.ActivityLoginBinding

class LoginActivity : AppCompatActivity() {

    private lateinit var binding: ActivityLoginBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityLoginBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val app = application as ForwardScanApp
        val apiClient = app.apiClient

        binding.tvVersionInfo.text = "Forward Scan v${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})"

        binding.btnSignIn.setOnClickListener {
            val username = binding.etUsername.text?.toString()?.trim().orEmpty()
            val password = binding.etPassword.text?.toString().orEmpty()

            if (username.isEmpty() || password.isEmpty()) {
                showError(getString(R.string.error_credentials_required))
                return@setOnClickListener
            }

            hideError()
            setLoading(true)

            lifecycleScope.launch {
                val result = withContext(Dispatchers.IO) {
                    apiClient.login(username, password)
                }

                setLoading(false)

                if (result.isSuccess) {
                    // Password is NOT saved.
                    binding.etPassword.text?.clear()
                    startActivity(Intent(this@LoginActivity, ChannelActivity::class.java))
                    finish()
                } else {
                    val msg = result.exceptionOrNull()?.message ?: "Sign in failed"
                    showError(msg)
                }
            }
        }
    }

    private fun setLoading(loading: Boolean) {
        binding.btnSignIn.isEnabled = !loading
        binding.etUsername.isEnabled = !loading
        binding.etPassword.isEnabled = !loading
        binding.progressLogin.visibility = if (loading) View.VISIBLE else View.GONE
        binding.btnSignIn.text = if (loading) getString(R.string.signing_in) else getString(R.string.sign_in_button)
    }

    private fun showError(msg: String) {
        binding.tvError.text = msg
        binding.tvError.visibility = View.VISIBLE
    }

    private fun hideError() {
        binding.tvError.visibility = View.GONE
    }
}
