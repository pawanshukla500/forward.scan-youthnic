package shop.youthnic.scan

import android.app.Application
import shop.youthnic.scan.data.ApiClient
import shop.youthnic.scan.data.SessionManager
import shop.youthnic.scan.util.SoundManager

class ForwardScanApp : Application() {

    lateinit var sessionManager: SessionManager
        private set

    lateinit var apiClient: ApiClient
        private set

    lateinit var soundManager: SoundManager
        private set

    override fun onCreate() {
        super.onCreate()
        instance = this
        sessionManager = SessionManager(this)
        apiClient = ApiClient(sessionManager)
        soundManager = SoundManager(this, sessionManager)
    }

    companion object {
        lateinit var instance: ForwardScanApp
            private set
    }
}
