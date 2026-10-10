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
    val pendingScans: Int,
    /** Today's AWBs (same numbers as the web picker and the scan screen); null on an older server. */
    val awbToday: AwbCounts? = null
) {
    /** What the picker shows as pending: synced - scanned (every AWB not scanned yet, whatever day it was made). */
    val pendingTotal: Int
        get() = awbToday?.pendingAll ?: pendingScans

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
                pendingScans = json.optInt("pending", 0),
                awbToday = json.optJSONObject("awb_today")?.let { AwbCounts.fromJson(it) }
            )
        }
    }
}

/** One marketplace's orders to dispatch today (server: reconcile). The owner's simple sum (10 Oct 2026):
    [synced] - [scanned] = [pendingAll] - e.g. 1000 synced, 980 scanned -> 20 pending. */
data class AwbCounts(
    val generated: Int,
    /** scanned of the synced orders (an earlier day's AWB scanned today included) */
    val scanned: Int,
    /** today's AWBs not scanned yet (part of [pendingAll]) */
    val pending: Int,
    /** earlier days' AWBs not scanned yet (part of [pendingAll]; not shown on its own any more) */
    val overdue: Int,
    val cancelled: Int,
    /** % of the synced orders scanned; null when there is nothing to dispatch. */
    val pct: Int?,
    /** every synced order not scanned yet */
    val pendingAll: Int = pending + overdue,
    /** synced orders to dispatch (cancelled ones not counted) */
    val synced: Int = scanned + pendingAll
) {
    companion object {
        fun fromJson(json: JSONObject): AwbCounts {
            val scanned = json.optInt("scanned", 0)
            val pending = json.optInt("pending", 0)
            val overdue = json.optInt("overdue", 0)
            val pendingAll = json.optInt("pending_all", pending + overdue)  // older server: no field
            return AwbCounts(
                generated = json.optInt("generated", 0),
                scanned = scanned,
                pending = pending,
                overdue = overdue,
                cancelled = json.optInt("cancelled", 0),
                pct = if (json.isNull("pct") || !json.has("pct")) null else json.optInt("pct", 0),
                pendingAll = pendingAll,
                synced = json.optInt("synced", scanned + pendingAll)
            )
        }
    }
}

/** One AWB still waiting to be scanned (server: /api/scan-context "queue"). */
data class PendingAwb(
    val awb: String,
    val orderId: String,
    val courier: String,
    val skus: Int,
    val units: Int,
    val slaDate: String,
    val awbGeneratedAt: String,
    val ageDays: Int,
    val priority: String,
    /** pending here although OMSGuru already shows it shipped / in transit */
    val shippedInOms: Boolean = false
) {
    companion object {
        fun fromJson(json: JSONObject): PendingAwb {
            return PendingAwb(
                awb = json.optString("awb", ""),
                orderId = json.optString("order_id", ""),
                courier = json.optString("courier", ""),
                skus = json.optInt("skus", 0),
                units = json.optInt("units", 0),
                slaDate = if (json.isNull("sla_date")) "" else json.optString("sla_date", ""),
                awbGeneratedAt = if (json.isNull("awb_generated_at")) "" else json.optString("awb_generated_at", ""),
                ageDays = json.optInt("age_days", 0),
                priority = json.optString("priority", "Normal"),
                shippedInOms = json.optBoolean("shipped_in_oms", false)
            )
        }
    }
}

/** Everything the scan screen shows besides the scan itself (server: GET /api/scan-context). */
data class ScanContext(
    /** successful scans today (verified + check) */
    val scannedToday: Int,
    val awb: AwbCounts,
    val queue: List<PendingAwb>,
    val queueTotal: Int,
    /** "Not found" scans today: flagged, not counted in [scannedToday] */
    val notFoundToday: Int = 0
) {
    /** Unscanned AWBs: synced - scanned (the same number the Pending list counts). */
    val pendingTotal: Int
        get() = queueTotal

    companion object {
        fun fromJson(json: JSONObject): ScanContext {
            val stats = json.optJSONObject("stats") ?: JSONObject()
            val awb = AwbCounts.fromJson(json.optJSONObject("awb") ?: JSONObject())
            val arr = json.optJSONArray("queue") ?: JSONArray()
            val queue = mutableListOf<PendingAwb>()
            for (i in 0 until arr.length()) {
                arr.optJSONObject(i)?.let { queue.add(PendingAwb.fromJson(it)) }
            }
            return ScanContext(
                scannedToday = stats.optInt("scanned", 0),
                awb = awb,
                queue = queue,
                queueTotal = json.optInt("queue_total", awb.pendingAll),
                notFoundToday = stats.optInt("not_found", 0)
            )
        }
    }
}

/** The newest app build published on the server (GET /api/app/latest). */
data class AppRelease(
    val versionCode: Int,
    val versionName: String,
    val notes: String,
    val sizeBytes: Long,
    val sha256: String,
    val publishedAt: String,
    val minVersionCode: Int,
    val downloadUrl: String,
    val pageUrl: String
) {
    fun isNewerThan(installedVersionCode: Int): Boolean = versionCode > installedVersionCode

    /** The server says versions below minVersionCode must update before scanning again. */
    fun isRequiredFor(installedVersionCode: Int): Boolean =
        isNewerThan(installedVersionCode) && installedVersionCode < minVersionCode

    companion object {
        /** null when nothing is published yet (or the answer is unusable). */
        fun fromJson(json: JSONObject): AppRelease? {
            if (!json.optBoolean("available", false)) return null
            val code = json.optInt("version_code", 0)
            val url = json.optString("download_url", "")
            if (code <= 0 || url.isBlank()) return null
            return AppRelease(
                versionCode = code,
                versionName = json.optString("version_name", code.toString()),
                notes = json.optString("notes", ""),
                sizeBytes = json.optLong("size", 0L),
                sha256 = json.optString("sha256", ""),
                publishedAt = json.optString("published_at", ""),
                minVersionCode = json.optInt("min_version_code", 0),
                downloadUrl = url,
                pageUrl = json.optString("page_url", url)
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
    val scannedAtLocal: String,
    val user: String = "",
    val station: String = ""
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
                scannedAtLocal = json.optString("scanned_at_local", ""),
                user = json.optString("user", ""),
                station = json.optString("station", "")
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
    /** Not an AWB (route / 2-D code, packet id, product barcode...) - nothing saved, scan the AWB of the same packet */
    WRONG_BARCODE,
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
                code in listOf("WRONG_BARCODE", "INVALID", "AMBIGUOUS") -> VerdictType.WRONG_BARCODE
                code in listOf("WRONG_CHANNEL", "CANCELLED", "RETURN", "BLOCKED") -> VerdictType.STOP
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
