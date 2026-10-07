package shop.youthnic.scan.data

import org.json.JSONArray
import org.json.JSONObject

data class User(
    val id: Int,
    val username: String,
    val fullName: String,
    val email: String,
    val role: String,
    val mustChangePassword: Boolean
) {
    companion object {
        fun fromJson(json: JSONObject): User {
            return User(
                id = json.optInt("id", 0),
                username = json.optString("username", ""),
                fullName = json.optString("full_name", ""),
                email = json.optString("email", ""),
                role = json.optString("role", "scanner"),
                mustChangePassword = json.optBoolean("must_change_password", false)
            )
        }
    }
}

data class AuthResponse(
    val user: User,
    val token: String,
    val refreshToken: String,
    val expiresAt: String,
    val refreshExpiresAt: String
) {
    companion object {
        fun fromJson(json: JSONObject): AuthResponse {
            val userJson = json.getJSONObject("user")
            return AuthResponse(
                user = User.fromJson(userJson),
                token = json.getString("token"),
                refreshToken = json.optString("refresh_token", ""),
                expiresAt = json.optString("expires_at", ""),
                refreshExpiresAt = json.optString("refresh_expires_at", "")
            )
        }
    }
}

data class Channel(
    val id: Int,
    val name: String,
    val marketplace: String,
    val company: String,
    val color: String,
    val scanEnabled: Boolean,
    val sortOrder: Int,
    val todayScans: Int,
    val pendingScans: Int
) {
    companion object {
        fun fromJson(json: JSONObject): Channel {
            return Channel(
                id = json.getInt("id"),
                name = json.optString("name", "Channel ${json.optInt("id")}"),
                marketplace = json.optString("marketplace", ""),
                company = json.optString("company", ""),
                color = json.optString("color", "#126B4E"),
                scanEnabled = json.optBoolean("scan_enabled", true),
                sortOrder = json.optInt("sort_order", 100),
                todayScans = json.optInt("today", 0),
                pendingScans = json.optInt("pending", 0)
            )
        }
    }
}

data class OrderItem(
    val sku: String,
    val qty: Int,
    val title: String,
    val imageUrl: String
) {
    companion object {
        fun fromJson(json: JSONObject): OrderItem {
            return OrderItem(
                sku = json.optString("sku", ""),
                qty = json.optInt("qty", 1),
                title = json.optString("title", ""),
                imageUrl = json.optString("image_url", "")
            )
        }
    }
}

data class Order(
    val id: Int,
    val channelOrderId: String,
    val courier: String,
    val statusText: String,
    val statusGroup: String,
    val itemCount: Int,
    val totalQty: Int,
    val items: List<OrderItem>
) {
    companion object {
        fun fromJson(json: JSONObject): Order {
            val itemsList = mutableListOf<OrderItem>()
            val arr = json.optJSONArray("items")
            if (arr != null) {
                for (i in 0 until arr.length()) {
                    val obj = arr.optJSONObject(i)
                    if (obj != null) itemsList.add(OrderItem.fromJson(obj))
                }
            }
            return Order(
                id = json.optInt("id", 0),
                channelOrderId = json.optString("channel_order_id", ""),
                courier = json.optString("courier", ""),
                statusText = json.optString("status_text", ""),
                statusGroup = json.optString("status_group", ""),
                itemCount = json.optInt("item_count", itemsList.size),
                totalQty = json.optInt("total_qty", itemsList.sumOf { it.qty }),
                items = itemsList
            )
        }
    }
}

data class ScanDetails(
    val id: Int,
    val trackingRaw: String,
    val trackingNorm: String,
    val result: String,
    val flags: List<String>,
    val message: String,
    val alert: String,
    val scannedAtLocal: String
) {
    companion object {
        fun fromJson(json: JSONObject): ScanDetails {
            val flagsList = mutableListOf<String>()
            val flagsArr = json.optJSONArray("flags")
            if (flagsArr != null) {
                for (i in 0 until flagsArr.length()) {
                    flagsList.add(flagsArr.optString(i))
                }
            } else {
                val flagsStr = json.optString("flags", "")
                if (flagsStr.isNotEmpty()) {
                    flagsList.addAll(flagsStr.split(",").filter { it.isNotBlank() })
                }
            }
            return ScanDetails(
                id = json.optInt("id", 0),
                trackingRaw = json.optString("tracking_raw", json.optString("tracking", "")),
                trackingNorm = json.optString("tracking_norm", ""),
                result = json.optString("result", "OK"),
                flags = flagsList,
                message = json.optString("message", ""),
                alert = json.optString("alert", ""),
                scannedAtLocal = json.optString("scanned_at_local", "")
            )
        }
    }
}

enum class VerdictType {
    OK,
    CHECK,
    STOP,
    DUPLICATE,
    NOT_IN_OMS,
    ERROR
}

data class ScanResponse(
    val severity: String,
    val code: String,
    val message: String,
    val scan: ScanDetails?,
    val order: Order?,
    val verdictType: VerdictType
) {
    companion object {
        fun fromJson(json: JSONObject): ScanResponse {
            val severity = json.optString("severity", "error")
            val code = json.optString("code", "")
            val message = json.optString("message", "")

            val scanObj = json.optJSONObject("scan")
            val scan = if (scanObj != null) ScanDetails.fromJson(scanObj) else null

            val orderObj = json.optJSONObject("order")
            val order = if (orderObj != null) Order.fromJson(orderObj) else null

            val verdictType = when {
                code == "DUPLICATE" -> VerdictType.DUPLICATE
                code == "NOT_IN_OMS" -> VerdictType.NOT_IN_OMS
                code in listOf("WRONG_CHANNEL", "CANCELLED", "RETURN", "BLOCKED", "INVALID") -> VerdictType.STOP
                severity == "error" -> VerdictType.STOP
                severity == "warning" || code == "WARN" -> VerdictType.CHECK
                severity == "success" || code == "OK" -> VerdictType.OK
                else -> VerdictType.CHECK
            }

            return ScanResponse(
                severity = severity,
                code = code,
                message = message,
                scan = scan,
                order = order,
                verdictType = verdictType
            )
        }
    }
}
