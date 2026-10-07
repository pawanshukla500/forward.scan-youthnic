package shop.youthnic.scan.ui

import android.annotation.SuppressLint
import android.content.Intent
import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import shop.youthnic.scan.ForwardScanApp
import shop.youthnic.scan.data.AuthExpiredException
import shop.youthnic.scan.data.NetworkException
import shop.youthnic.scan.databinding.ActivitySplashBinding

@SuppressLint("CustomSplashScreen")
class SplashActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySplashBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySplashBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val app = application as ForwardScanApp
        val sessionManager = app.sessionManager
        val apiClient = app.apiClient

        lifecycleScope.launch {
            if (!sessionManager.hasValidRefreshToken()) {
                navigateToLogin()
                return@launch
            }

            // Valid refresh token exists. Verify session or refresh.
            val result = withContext(Dispatchers.IO) {
                // If we already have access token, test getMe(). If it fails with 401, refresh.
                val meRes = apiClient.getMe()
                if (meRes.isSuccess) {
                    meRes
                } else if (meRes.exceptionOrNull() is AuthExpiredException) {
                    // Try explicit refresh
                    val refRes = apiClient.refreshSessionSync()
                    if (refRes.isSuccess) apiClient.getMe() else refRes
                } else {
                    meRes
                }
            }

            if (result.isSuccess) {
                val user = sessionManager.getUser()
                if (user?.mustChangePassword == true) {
                    navigateToChangePassword()
                } else {
                    navigateToChannels()
                }
            } else {
                val ex = result.exceptionOrNull()
                if (ex is NetworkException) {
                    // Offline - do not kick user out! Allow entering channel screen.
                    val user = sessionManager.getUser()
                    if (user?.mustChangePassword == true) {
                        navigateToChangePassword()
                    } else {
                        navigateToChannels()
                    }
                } else {
                    // Invalid/revoked session
                    sessionManager.clearSession()
                    navigateToLogin()
                }
            }
        }
    }

    private fun navigateToChangePassword() {
        startActivity(Intent(this, ChangePasswordActivity::class.java))
        finish()
    }

    private fun navigateToChannels() {
        startActivity(Intent(this, ChannelActivity::class.java))
        finish()
    }

    private fun navigateToLogin() {
        startActivity(Intent(this, LoginActivity::class.java))
        finish()
    }
}
