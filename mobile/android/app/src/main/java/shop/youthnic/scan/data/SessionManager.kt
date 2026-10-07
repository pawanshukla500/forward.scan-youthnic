package shop.youthnic.scan.data

import android.content.Context
import android.content.SharedPreferences
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONObject
import shop.youthnic.scan.BuildConfig
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

class SessionManager(private val context: Context) {

    private val prefs: SharedPreferences = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    companion object {
        private const val PREFS_NAME = "fs_mobile_prefs"
        private const val KEY_ALIAS = "ForwardScanKey"
        private const val ANDROID_KEYSTORE = "AndroidKeyStore"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        private const val GCM_IV_LENGTH = 12
        private const val GCM_TAG_LENGTH = 128

        private const val PREF_ENCRYPTED_REFRESH_TOKEN = "enc_refresh_token"
        private const val PREF_ACCESS_TOKEN = "access_token"
        private const val PREF_ACCESS_EXPIRY = "access_expiry"
        private const val PREF_REFRESH_EXPIRY = "refresh_expiry"
        private const val PREF_USER_JSON = "user_json"

        private const val PREF_STATION_NAME = "station_name"
        private const val PREF_SOUND_ENABLED = "sound_enabled"
        private const val PREF_VIBRATION_ENABLED = "vibration_enabled"
        private const val PREF_LAST_CHANNEL_ID = "last_channel_id"
        private const val PREF_LAST_CHANNEL_NAME = "last_channel_name"
        private const val PREF_SERVER_URL = "server_url"
    }

    private fun getOrCreateSecretKey(): SecretKey {
        val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE)
        keyStore.load(null)
        if (keyStore.containsAlias(KEY_ALIAS)) {
            val entry = keyStore.getEntry(KEY_ALIAS, null) as? KeyStore.SecretKeyEntry
            if (entry != null) return entry.secretKey
        }

        val keyGenerator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
        val keyGenParameterSpec = KeyGenParameterSpec.Builder(
            KEY_ALIAS,
            KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
        )
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256)
            .build()

        keyGenerator.init(keyGenParameterSpec)
        return keyGenerator.generateKey()
    }

    private fun encrypt(plainText: String): String {
        return try {
            val key = getOrCreateSecretKey()
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.ENCRYPT_MODE, key)
            val iv = cipher.iv
            val cipherText = cipher.doFinal(plainText.toByteArray(Charsets.UTF_8))
            val combined = ByteArray(iv.size + cipherText.size)
            System.arraycopy(iv, 0, combined, 0, iv.size)
            System.arraycopy(cipherText, 0, combined, iv.size, cipherText.size)
            Base64.encodeToString(combined, Base64.NO_WRAP)
        } catch (e: Exception) {
            ""
        }
    }

    private fun decrypt(encryptedBase64: String): String? {
        if (encryptedBase64.isBlank()) return null
        return try {
            val combined = Base64.decode(encryptedBase64, Base64.NO_WRAP)
            if (combined.size < GCM_IV_LENGTH) return null
            val iv = ByteArray(GCM_IV_LENGTH)
            val cipherText = ByteArray(combined.size - GCM_IV_LENGTH)
            System.arraycopy(combined, 0, iv, 0, GCM_IV_LENGTH)
            System.arraycopy(combined, GCM_IV_LENGTH, cipherText, 0, cipherText.size)

            val key = getOrCreateSecretKey()
            val cipher = Cipher.getInstance(TRANSFORMATION)
            val spec = GCMParameterSpec(GCM_TAG_LENGTH, iv)
            cipher.init(Cipher.DECRYPT_MODE, key, spec)
            val plainBytes = cipher.doFinal(cipherText)
            String(plainBytes, Charsets.UTF_8)
        } catch (e: Exception) {
            null
        }
    }

    fun saveSession(auth: AuthResponse) {
        val editor = prefs.edit()
        editor.putString(PREF_ACCESS_TOKEN, auth.token)
        editor.putString(PREF_ACCESS_EXPIRY, auth.expiresAt)
        editor.putString(PREF_REFRESH_EXPIRY, auth.refreshExpiresAt)

        if (auth.refreshToken.isNotBlank()) {
            val enc = encrypt(auth.refreshToken)
            editor.putString(PREF_ENCRYPTED_REFRESH_TOKEN, enc)
        }

        val u = auth.user
        val userJson = JSONObject().apply {
            put("id", u.id)
            put("username", u.username)
            put("full_name", u.fullName)
            put("email", u.email)
            put("role", u.role)
            put("must_change_password", u.mustChangePassword)
        }
        editor.putString(PREF_USER_JSON, userJson.toString())
        editor.apply()
    }

    fun updateTokens(accessToken: String, expiresAt: String, newRefreshToken: String? = null, newRefreshExpiresAt: String? = null) {
        val editor = prefs.edit()
        editor.putString(PREF_ACCESS_TOKEN, accessToken)
        editor.putString(PREF_ACCESS_EXPIRY, expiresAt)
        if (!newRefreshToken.isNullOrBlank()) {
            editor.putString(PREF_ENCRYPTED_REFRESH_TOKEN, encrypt(newRefreshToken))
        }
        if (!newRefreshExpiresAt.isNullOrBlank()) {
            editor.putString(PREF_REFRESH_EXPIRY, newRefreshExpiresAt)
        }
        editor.apply()
    }

    fun getAccessToken(): String? = prefs.getString(PREF_ACCESS_TOKEN, null)

    fun getRefreshToken(): String? {
        val enc = prefs.getString(PREF_ENCRYPTED_REFRESH_TOKEN, null) ?: return null
        return decrypt(enc)
    }

    fun getUser(): User? {
        val raw = prefs.getString(PREF_USER_JSON, null) ?: return null
        return try {
            User.fromJson(JSONObject(raw))
        } catch (e: Exception) {
            null
        }
    }

    fun clearSession() {
        prefs.edit()
            .remove(PREF_ACCESS_TOKEN)
            .remove(PREF_ACCESS_EXPIRY)
            .remove(PREF_REFRESH_EXPIRY)
            .remove(PREF_ENCRYPTED_REFRESH_TOKEN)
            .remove(PREF_USER_JSON)
            .apply()
    }

    fun hasValidRefreshToken(): Boolean {
        return !getRefreshToken().isNullOrBlank()
    }

    // --- Harmless user preferences ---

    var stationName: String
        get() = prefs.getString(PREF_STATION_NAME, "") ?: ""
        set(value) = prefs.edit().putString(PREF_STATION_NAME, value.trim()).apply()

    var isSoundEnabled: Boolean
        get() = prefs.getBoolean(PREF_SOUND_ENABLED, true)
        set(value) = prefs.edit().putBoolean(PREF_SOUND_ENABLED, value).apply()

    var isVibrationEnabled: Boolean
        get() = prefs.getBoolean(PREF_VIBRATION_ENABLED, true)
        set(value) = prefs.edit().putBoolean(PREF_VIBRATION_ENABLED, value).apply()

    var lastChannelId: Int
        get() = prefs.getInt(PREF_LAST_CHANNEL_ID, -1)
        set(value) = prefs.edit().putInt(PREF_LAST_CHANNEL_ID, value).apply()

    var lastChannelName: String
        get() = prefs.getString(PREF_LAST_CHANNEL_NAME, "") ?: ""
        set(value) = prefs.edit().putString(PREF_LAST_CHANNEL_NAME, value).apply()

    var serverUrl: String
        get() = prefs.getString(PREF_SERVER_URL, BuildConfig.DEFAULT_API_URL) ?: BuildConfig.DEFAULT_API_URL
        set(value) = prefs.edit().putString(PREF_SERVER_URL, value.trim().removeSuffix("/")).apply()
}
