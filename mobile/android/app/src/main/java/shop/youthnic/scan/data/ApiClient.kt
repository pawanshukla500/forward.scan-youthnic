package shop.youthnic.scan.data

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

class NetworkException(message: String, val isOffline: Boolean = true) : IOException(message)
class ApiException(val code: Int, message: String) : Exception(message)
class AuthExpiredException(message: String = "Session expired") : Exception(message)

class ApiClient(private val sessionManager: SessionManager) {

    private val jsonMediaType = "application/json; charset=utf-8".toMediaType()

    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .writeTimeout(15, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    private fun baseUrl(): String {
        return sessionManager.serverUrl.removeSuffix("/")
    }

    private fun newRequestBuilder(path: String, authenticated: Boolean = true): Request.Builder {
        val url = "${baseUrl()}$path"
        val builder = Request.Builder().url(url)
        if (authenticated) {
            val token = sessionManager.getAccessToken()
            if (!token.isNullOrBlank()) {
                builder.header("Authorization", "Bearer $token")
            }
        }
        builder.header("Accept", "application/json")
        return builder
    }

    private fun <T> executeWithAutoRefresh(
        authenticated: Boolean = true,
        requestFactory: () -> Request,
        parser: (String) -> T
    ): Result<T> {
        return try {
            val request = requestFactory()
            val response = client.newCall(request).execute()

            if (response.code == 401 && authenticated) {
                response.close()
                // Attempt refresh
                val refreshResult = refreshSessionSync()
                if (refreshResult.isSuccess) {
                    // Retry original call once
                    val retryRequest = requestFactory()
                    val retryResponse = client.newCall(retryRequest).execute()
                    val body = retryResponse.body?.string().orEmpty()
                    if (!retryResponse.isSuccessful) {
                        retryResponse.close()
                        return Result.failure(ApiException(retryResponse.code, parseErrorMessage(body, retryResponse.code)))
                    }
                    retryResponse.close()
                    return Result.success(parser(body))
                } else {
                    sessionManager.clearSession()
                    return Result.failure(AuthExpiredException())
                }
            }

            val body = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                response.close()
                return Result.failure(ApiException(response.code, parseErrorMessage(body, response.code)))
            }
            response.close()
            Result.success(parser(body))
        } catch (e: IOException) {
            Result.failure(NetworkException("No connection — scan not submitted: ${e.message.orEmpty()}"))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    private fun parseErrorMessage(body: String, status: Int): String {
        return try {
            val json = JSONObject(body)
            json.optString("detail", "Server error ($status)")
        } catch (_: Exception) {
            "Server error ($status)"
        }
    }

    @Synchronized
    fun refreshSessionSync(): Result<AuthResponse> {
        val refreshToken = sessionManager.getRefreshToken()
        if (refreshToken.isNullOrBlank()) {
            return Result.failure(AuthExpiredException("No refresh token stored"))
        }

        val json = JSONObject().apply {
            put("refresh_token", refreshToken)
            put("device_info", android.os.Build.MODEL ?: "Android Phone")
        }
        val request = Request.Builder()
            .url("${baseUrl()}/api/auth/mobile/refresh")
            .post(json.toString().toRequestBody(jsonMediaType))
            .build()

        return try {
            val response = client.newCall(request).execute()
            val body = response.body?.string().orEmpty()
            if (response.isSuccessful) {
                response.close()
                val auth = AuthResponse.fromJson(JSONObject(body))
                sessionManager.saveSession(auth)
                Result.success(auth)
            } else {
                response.close()
                Result.failure(ApiException(response.code, parseErrorMessage(body, response.code)))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    fun login(username: String, password: String): Result<AuthResponse> {
        val json = JSONObject().apply {
            put("username", username.trim())
            put("password", password)
            put("device_info", android.os.Build.MODEL ?: "Android Phone")
        }
        val request = Request.Builder()
            .url("${baseUrl()}/api/auth/mobile/login")
            .post(json.toString().toRequestBody(jsonMediaType))
            .build()

        return try {
            val response = client.newCall(request).execute()
            val body = response.body?.string().orEmpty()
            if (response.isSuccessful) {
                response.close()
                val auth = AuthResponse.fromJson(JSONObject(body))
                sessionManager.saveSession(auth)
                Result.success(auth)
            } else {
                response.close()
                Result.failure(ApiException(response.code, parseErrorMessage(body, response.code)))
            }
        } catch (e: IOException) {
            Result.failure(NetworkException("No connection to server. Check network settings."))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    fun logout(): Boolean {
        val refreshToken = sessionManager.getRefreshToken()
        if (!refreshToken.isNullOrBlank()) {
            val json = JSONObject().apply {
                put("refresh_token", refreshToken)
            }
            val request = newRequestBuilder("/api/auth/mobile/logout", authenticated = true)
                .post(json.toString().toRequestBody(jsonMediaType))
                .build()
            try {
                client.newCall(request).execute().close()
            } catch (_: Exception) {
                // best effort
            }
        }
        sessionManager.clearSession()
        return true
    }

    fun getMe(): Result<User> {
        return executeWithAutoRefresh(
            requestFactory = { newRequestBuilder("/api/auth/me").get().build() },
            parser = { body ->
                val json = JSONObject(body)
                User.fromJson(json.getJSONObject("user"))
            }
        )
    }

    fun getChannels(): Result<List<Channel>> {
        return executeWithAutoRefresh(
            requestFactory = { newRequestBuilder("/api/channels").get().build() },
            parser = { body ->
                val json = JSONObject(body)
                val channelsArr = json.optJSONArray("channels") ?: JSONArray()
                val list = mutableListOf<Channel>()
                for (i in 0 until channelsArr.length()) {
                    val obj = channelsArr.optJSONObject(i)
                    if (obj != null) {
                        list.add(Channel.fromJson(obj))
                    }
                }
                list.sortedBy { it.sortOrder }
            }
        )
    }

    fun submitScan(channelId: Int, tracking: String, station: String): Result<ScanResponse> {
        val json = JSONObject().apply {
            put("channel_id", channelId)
            put("tracking", tracking.trim())
            put("station", station.trim())
        }
        return executeWithAutoRefresh(
            requestFactory = {
                newRequestBuilder("/api/scan")
                    .post(json.toString().toRequestBody(jsonMediaType))
                    .build()
            },
            parser = { body ->
                ScanResponse.fromJson(JSONObject(body))
            }
        )
    }

    fun getRecentScans(channelId: Int, limit: Int = 20): Result<List<ScanDetails>> {
        return executeWithAutoRefresh(
            requestFactory = {
                newRequestBuilder("/api/scans/recent?channel_id=$channelId&limit=$limit").get().build()
            },
            parser = { body ->
                val json = JSONObject(body)
                val arr = json.optJSONArray("scans") ?: JSONArray()
                val list = mutableListOf<ScanDetails>()
                for (i in 0 until arr.length()) {
                    val obj = arr.optJSONObject(i)
                    if (obj != null) list.add(ScanDetails.fromJson(obj))
                }
                list
            }
        )
    }
}
