package shop.youthnic.scan.ui

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.util.Size
import android.view.View
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.core.widget.ImageViewCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.material.bottomsheet.BottomSheetBehavior
import com.google.android.material.bottomsheet.BottomSheetDialog
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.mlkit.vision.barcode.BarcodeScanner
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import shop.youthnic.scan.ForwardScanApp
import shop.youthnic.scan.R
import shop.youthnic.scan.data.AppRelease
import shop.youthnic.scan.data.AuthExpiredException
import shop.youthnic.scan.data.BarcodeRules
import shop.youthnic.scan.data.DuplicateGuard
import shop.youthnic.scan.data.NetworkException
import shop.youthnic.scan.data.PendingAwb
import shop.youthnic.scan.data.ScanContext
import shop.youthnic.scan.data.ScanDetails
import shop.youthnic.scan.data.ScanResponse
import shop.youthnic.scan.data.VerdictType
import shop.youthnic.scan.databinding.ActivityScannerBinding
import shop.youthnic.scan.databinding.DialogManualScanBinding
import shop.youthnic.scan.databinding.DialogRecentScansBinding
import shop.youthnic.scan.databinding.ItemPendingRowBinding
import shop.youthnic.scan.databinding.SheetPendingBinding
import shop.youthnic.scan.update.AppUpdater
import shop.youthnic.scan.util.Ui
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

class ScannerActivity : AppCompatActivity() {

    private lateinit var binding: ActivityScannerBinding
    private var channelId: Int = 0
    private var channelName: String = ""
    private var channelColor: String = ""

    private var cameraProvider: ProcessCameraProvider? = null
    private var camera: Camera? = null
    private var isTorchOn = false
    private lateinit var cameraExecutor: ExecutorService
    private var barcodeScanner: BarcodeScanner? = null

    private val duplicateGuard = DuplicateGuard(sameCodeSuppressMs = 8000L, anyCodeCooldownMs = 2200L)
    private val isRequestInFlight = AtomicBoolean(false)

    /** Latest numbers from the server (header, Pending badge and Pending sheet). */
    private var scanContext: ScanContext? = null
    private var periodicJob: Job? = null
    private var refreshSoonJob: Job? = null
    private var pendingSheet: SheetPendingBinding? = null

    companion object {
        const val EXTRA_CHANNEL_ID = "extra_channel_id"
        const val EXTRA_CHANNEL_NAME = "extra_channel_name"
        const val EXTRA_CHANNEL_COLOR = "extra_channel_color"

        /** Header numbers and the update check refresh this often while the scanner is open. */
        private const val REFRESH_EVERY_MS = 60_000L
        private const val PENDING_ROWS = 50
    }

    private val cameraPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) {
            startCamera()
        } else {
            binding.tvScannerHint.text = getString(R.string.camera_permission_needed)
            MaterialAlertDialogBuilder(this)
                .setTitle("Camera Permission Needed")
                .setMessage("Forward Scan requires camera access to scan shipping labels. You can also use Manual entry.")
                .setPositiveButton("Use Manual Entry") { _, _ -> showManualScanDialog() }
                .setNegativeButton("Close") { _, _ -> finish() }
                .show()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityScannerBinding.inflate(layoutInflater)
        setContentView(binding.root)

        channelId = intent.getIntExtra(EXTRA_CHANNEL_ID, 0)
        channelName = intent.getStringExtra(EXTRA_CHANNEL_NAME) ?: "Marketplace"
        channelColor = intent.getStringExtra(EXTRA_CHANNEL_COLOR) ?: "#126B4E"

        cameraExecutor = Executors.newSingleThreadExecutor()

        initUi()
        initBarcodeScanner()

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            startCamera()
        } else {
            cameraPermissionLauncher.launch(Manifest.permission.CAMERA)
        }
    }

    override fun onResume() {
        super.onResume()
        startPeriodicRefresh()
        AppUpdater.resumePendingInstall(this)
    }

    override fun onPause() {
        super.onPause()
        periodicJob?.cancel()
        periodicJob = null
    }

    private fun initUi() {
        binding.tvScannerChannelName.text = channelName
        Ui.tint(binding.viewChannelColor, Ui.parseColor(channelColor, ContextCompat.getColor(this, R.color.colorPrimary)))

        // Camera takes about a third of the screen: big enough to aim, leaves room for the whole result card.
        val screen = resources.displayMetrics.heightPixels
        val camHeight = (screen * 0.34f).toInt().coerceIn(Ui.dp(this, 200), Ui.dp(this, 320))
        binding.cameraContainer.layoutParams = binding.cameraContainer.layoutParams.apply { height = camHeight }

        binding.tvProgressScanned.text = getString(R.string.scanned_today_format, "-")
        binding.tvProgressPending.text = ""

        binding.btnBack.setOnClickListener { finish() }
        binding.btnTorch.setOnClickListener { toggleTorch() }
        binding.btnManual.setOnClickListener { showManualScanDialog() }
        binding.btnPending.setOnClickListener { showPendingSheet() }
        binding.btnRecent.setOnClickListener { showRecentScansSheet() }

        binding.cardResult.visibility = View.GONE
        binding.cardIdleState.visibility = View.VISIBLE
    }

    // ---- server numbers: header, Pending badge, update banner -----------------------------------

    private fun startPeriodicRefresh() {
        periodicJob?.cancel()
        periodicJob = lifecycleScope.launch {
            while (isActive) {
                loadContext()
                showUpdateBanner(AppUpdater.check(this@ScannerActivity).getOrNull())
                delay(REFRESH_EVERY_MS)
            }
        }
    }

    /** After a scan: refresh the numbers shortly (several stations scan the same marketplace). */
    private fun refreshSoon() {
        refreshSoonJob?.cancel()
        refreshSoonJob = lifecycleScope.launch {
            delay(1200)
            loadContext()
        }
    }

    private suspend fun loadContext(): Boolean {
        val api = (application as ForwardScanApp).apiClient
        val result = withContext(Dispatchers.IO) { api.getScanContext(channelId, PENDING_ROWS) }
        val ctx = result.getOrNull()
        if (ctx != null) {
            scanContext = ctx
            setOnline(true)
            renderCounts(ctx)
            pendingSheet?.let { renderPendingSheet(it, ctx) }
            return true
        }
        when (result.exceptionOrNull()) {
            is NetworkException -> setOnline(false)
            is AuthExpiredException -> goToLogin()
        }
        return false
    }

    private fun renderCounts(ctx: ScanContext) {
        binding.tvProgressScanned.text = getString(R.string.scanned_today_format, Ui.count(ctx.scannedToday))
        val pending = ctx.pendingTotal
        if (pending > 0) {
            binding.tvProgressPending.text = getString(R.string.pending_count_format, Ui.count(pending))
            binding.tvProgressPending.setTextColor(ContextCompat.getColor(this, R.color.verdict_check))
            binding.tvPendingBadge.text = if (pending > 999) "999+" else pending.toString()
            binding.tvPendingBadge.visibility = View.VISIBLE
        } else {
            binding.tvProgressPending.text = getString(R.string.all_scanned)
            binding.tvProgressPending.setTextColor(ContextCompat.getColor(this, R.color.verdict_ok))
            binding.tvPendingBadge.visibility = View.GONE
        }
        binding.progressBarScans.progress = ctx.awb.pct ?: if (pending == 0 && ctx.scannedToday > 0) 100 else 0
    }

    private fun setOnline(online: Boolean) {
        val tv = binding.tvConnectionStatus
        if (online) {
            tv.text = getString(R.string.connection_online)
            Ui.pillRes(tv, R.color.verdict_ok, R.color.verdict_ok_bg)
        } else {
            tv.text = getString(R.string.connection_offline_short)
            Ui.pillRes(tv, R.color.verdict_stop, R.color.verdict_stop_bg)
        }
    }

    private fun showUpdateBanner(release: AppRelease?) {
        if (release == null) {
            binding.tvUpdateBanner.visibility = View.GONE
            return
        }
        binding.tvUpdateBanner.text = getString(R.string.update_scanner_banner, release.versionName)
        binding.tvUpdateBanner.visibility = View.VISIBLE
        binding.tvUpdateBanner.setOnClickListener { AppUpdater.showUpdateDialog(this, release, always = true) }
    }

    // ---- camera -----------------------------------------------------------------------------------

    private fun initBarcodeScanner() {
        // Restrict strictly to 1-D barcode formats matching warehouse labels
        val options = BarcodeScannerOptions.Builder()
            .setBarcodeFormats(
                Barcode.FORMAT_CODE_128,
                Barcode.FORMAT_CODE_39,
                Barcode.FORMAT_CODE_93,
                Barcode.FORMAT_CODABAR,
                Barcode.FORMAT_ITF,
                Barcode.FORMAT_EAN_13,
                Barcode.FORMAT_EAN_8,
                Barcode.FORMAT_UPC_A,
                Barcode.FORMAT_UPC_E
            )
            .build()
        barcodeScanner = BarcodeScanning.getClient(options)
    }

    private fun startCamera() {
        binding.tvScannerHint.text = getString(R.string.preparing_scanner)
        val cameraProviderFuture = ProcessCameraProvider.getInstance(this)
        cameraProviderFuture.addListener({
            try {
                cameraProvider = cameraProviderFuture.get()
                bindCameraUseCases()
            } catch (e: Exception) {
                binding.tvScannerHint.text = "Error starting camera: ${e.message}"
            }
        }, ContextCompat.getMainExecutor(this))
    }

    private fun bindCameraUseCases() {
        val provider = cameraProvider ?: return

        val preview = Preview.Builder().build().also {
            it.setSurfaceProvider(binding.previewView.surfaceProvider)
        }

        val imageAnalysis = ImageAnalysis.Builder()
            .setTargetResolution(Size(1280, 720))
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .build()

        imageAnalysis.setAnalyzer(cameraExecutor) { imageProxy ->
            processImageProxy(imageProxy)
        }

        try {
            provider.unbindAll()
            camera = provider.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, preview, imageAnalysis)
            binding.tvScannerHint.text = getString(R.string.aim_at_awb)
        } catch (e: Exception) {
            binding.tvScannerHint.text = "Camera bind error: ${e.message}"
        }
    }

    @SuppressLint("UnsafeOptInUsageError")
    private fun processImageProxy(imageProxy: ImageProxy) {
        val scanner = barcodeScanner
        val mediaImage = imageProxy.image

        if (mediaImage == null || scanner == null || isRequestInFlight.get()) {
            imageProxy.close()
            return
        }

        val image = InputImage.fromMediaImage(mediaImage, imageProxy.imageInfo.rotationDegrees)
        scanner.process(image)
            .addOnSuccessListener { barcodes ->
                onBarcodesDetected(barcodes)
            }
            .addOnFailureListener {
                // Ignore transient frame decode failure
            }
            .addOnCompleteListener {
                // Ensure imageProxy is always closed in every code path
                imageProxy.close()
            }
    }

    private fun onBarcodesDetected(barcodes: List<Barcode>) {
        if (isRequestInFlight.get()) return

        for (barcode in barcodes) {
            val rawValue = barcode.rawValue?.trim().orEmpty()
            val format = barcode.format

            if (rawValue.isEmpty()) continue
            if (!BarcodeRules.isAllowed1DFormat(format)) continue
            if (BarcodeRules.looksLikeQr(rawValue)) continue

            // Duplicate & double-read guard
            if (!duplicateGuard.shouldProcess(rawValue)) {
                continue
            }

            // Valid candidate found: mark emitted and submit
            duplicateGuard.markEmitted(rawValue)
            runOnUiThread {
                submitScan(rawValue)
            }
            break
        }
    }

    // ---- scanning ---------------------------------------------------------------------------------

    private fun submitScan(rawAwb: String) {
        if (!isRequestInFlight.compareAndSet(false, true)) return

        val app = application as ForwardScanApp
        val apiClient = app.apiClient
        val soundManager = app.soundManager
        val station = app.sessionManager.stationName

        binding.progressSubmitting.visibility = View.VISIBLE
        binding.tvScannerHint.text = getString(R.string.submitting_scan)

        lifecycleScope.launch {
            val result = withContext(Dispatchers.IO) {
                apiClient.submitScan(channelId, rawAwb, station)
            }

            binding.progressSubmitting.visibility = View.GONE
            binding.tvScannerHint.text = getString(R.string.aim_at_awb)
            isRequestInFlight.set(false)

            if (result.isSuccess) {
                val scanResponse = result.getOrThrow()
                setOnline(true)
                displayScanResult(rawAwb, scanResponse)
                soundManager.playFeedback(scanResponse.verdictType)
                refreshSoon()
            } else {
                when (val ex = result.exceptionOrNull()) {
                    is NetworkException -> {
                        displayNetworkError(rawAwb)
                        soundManager.playFeedback(VerdictType.ERROR)
                    }
                    is AuthExpiredException -> goToLogin()
                    else -> {
                        setOnline(true)
                        displayGeneralError(rawAwb, ex?.message ?: "Scan rejected")
                        soundManager.playFeedback(VerdictType.ERROR)
                    }
                }
            }
        }
    }

    private fun showResultColors(color: Int) {
        binding.cardIdleState.visibility = View.GONE
        binding.cardResult.visibility = View.VISIBLE
        binding.bannerVerdict.setBackgroundColor(color)
        binding.cardResult.strokeColor = color
        binding.scrollResultArea.scrollTo(0, 0)
    }

    private fun displayScanResult(awb: String, response: ScanResponse) {
        val vType = response.verdictType
        val bannerColor = ContextCompat.getColor(
            this,
            when (vType) {
                VerdictType.OK -> R.color.verdict_ok
                VerdictType.CHECK -> R.color.verdict_check
                VerdictType.STOP -> R.color.verdict_stop
                VerdictType.DUPLICATE -> R.color.verdict_duplicate
                VerdictType.NOT_IN_OMS -> R.color.verdict_unverified
                VerdictType.ERROR -> R.color.verdict_stop
            }
        )
        showResultColors(bannerColor)

        binding.tvVerdictTitle.text = when (vType) {
            VerdictType.OK -> getString(R.string.verdict_verified)
            VerdictType.CHECK -> getString(R.string.verdict_check)
            VerdictType.STOP -> getString(R.string.verdict_stop)
            VerdictType.DUPLICATE -> getString(R.string.verdict_duplicate)
            VerdictType.NOT_IN_OMS -> getString(R.string.verdict_not_in_oms)
            VerdictType.ERROR -> "SCAN ERROR"
        }

        binding.tvVerdictMessage.text = if (vType == VerdictType.CHECK) {
            "${getString(R.string.what_to_check)} ${response.message}"
        } else {
            response.message
        }
        binding.tvResultAwb.text = awb

        val order = response.order
        binding.layoutItemsList.removeAllViews()
        if (order != null) {
            binding.tvResultOrderId.text = order.channelOrderId.ifBlank { "ID: ${order.id}" }
            binding.tvResultCourier.text = order.courier.ifBlank { "-" }
            binding.tvResultItemsCount.text = itemsSummary(order.itemCount, order.totalQty)
            for (item in order.items.take(4)) {
                addItemLine("${item.sku}  ×${item.qty}" + if (item.title.isNotBlank()) "  ${item.title.take(40)}" else "")
            }
            if (order.items.size > 4) addItemLine("+${order.items.size - 4} more")
        } else {
            binding.tvResultOrderId.text = "-"
            binding.tvResultCourier.text = "-"
            binding.tvResultItemsCount.text = "-"
        }
    }

    private fun itemsSummary(skus: Int, units: Int): String =
        "$skus SKU${if (skus == 1) "" else "s"} · $units unit${if (units == 1) "" else "s"}"

    private fun addItemLine(text: String) {
        val tv = TextView(this).apply {
            this.text = text
            textSize = 12f
            maxLines = 1
            ellipsize = android.text.TextUtils.TruncateAt.END
            setTextColor(ContextCompat.getColor(context, R.color.text_secondary))
            val pad = Ui.dp(context, 2)
            setPadding(0, pad, 0, pad)
        }
        binding.layoutItemsList.addView(tv)
    }

    private fun displayNetworkError(awb: String) {
        showResultColors(ContextCompat.getColor(this, R.color.verdict_stop))
        binding.tvVerdictTitle.text = getString(R.string.verdict_offline)
        binding.tvVerdictMessage.text = getString(R.string.connection_offline)
        binding.tvResultAwb.text = awb
        binding.tvResultOrderId.text = getString(R.string.not_submitted)
        binding.tvResultCourier.text = "-"
        binding.tvResultItemsCount.text = getString(R.string.check_connection_rescan)
        binding.layoutItemsList.removeAllViews()
        setOnline(false)
    }

    private fun displayGeneralError(awb: String, errorMsg: String) {
        showResultColors(ContextCompat.getColor(this, R.color.verdict_stop))
        binding.tvVerdictTitle.text = getString(R.string.verdict_stop)
        binding.tvVerdictMessage.text = errorMsg
        binding.tvResultAwb.text = awb
        binding.tvResultOrderId.text = "-"
        binding.tvResultCourier.text = "-"
        binding.tvResultItemsCount.text = "-"
        binding.layoutItemsList.removeAllViews()
    }

    private fun toggleTorch() {
        val cam = camera ?: return
        isTorchOn = !isTorchOn
        cam.cameraControl.enableTorch(isTorchOn)
        val color = ContextCompat.getColor(this, if (isTorchOn) R.color.colorPrimary else R.color.text_primary)
        binding.tvTorchLabel.text = getString(if (isTorchOn) R.string.torch_on else R.string.torch_off)
        binding.tvTorchLabel.setTextColor(color)
        ImageViewCompat.setImageTintList(binding.ivTorch, android.content.res.ColorStateList.valueOf(color))
    }

    private fun showManualScanDialog() {
        val dialogBinding = DialogManualScanBinding.inflate(layoutInflater)
        val dialog = MaterialAlertDialogBuilder(this)
            .setView(dialogBinding.root)
            .create()

        dialogBinding.btnCancelManual.setOnClickListener {
            dialog.dismiss()
        }

        dialogBinding.btnSubmitManual.setOnClickListener {
            val input = dialogBinding.etManualAwb.text?.toString()?.trim().orEmpty()
            if (input.length >= 6) {
                dialog.dismiss()
                submitScan(input)
            } else {
                dialogBinding.etManualAwb.error = "Enter a valid tracking ID (at least 6 characters)"
            }
        }

        dialog.show()
    }

    // ---- Pending sheet ------------------------------------------------------------------------

    private fun newSheet(): BottomSheetDialog {
        val sheet = BottomSheetDialog(this, R.style.ThemeOverlay_ForwardScan_BottomSheet)
        sheet.behavior.skipCollapsed = true
        sheet.behavior.state = BottomSheetBehavior.STATE_EXPANDED
        return sheet
    }

    private fun showPendingSheet() {
        val sb = SheetPendingBinding.inflate(layoutInflater)
        val sheet = newSheet()
        sheet.setContentView(sb.root)
        sb.tvPendingChannel.text = channelName
        sb.btnClosePending.setOnClickListener { sheet.dismiss() }
        sb.btnRefreshPending.setOnClickListener { reloadPending(sb) }
        sheet.setOnDismissListener { if (pendingSheet === sb) pendingSheet = null }
        pendingSheet = sb
        scanContext?.let { renderPendingSheet(sb, it) }
        sheet.show()
        reloadPending(sb)
    }

    private fun reloadPending(sb: SheetPendingBinding) {
        sb.progressPending.visibility = View.VISIBLE
        lifecycleScope.launch {
            val ok = loadContext()  // renders into the sheet while it is open
            if (pendingSheet !== sb) return@launch
            sb.progressPending.visibility = View.INVISIBLE
            if (!ok) {
                if (scanContext == null) {
                    sb.tvPendingEmpty.text = getString(R.string.pending_load_failed)
                    sb.tvPendingEmpty.visibility = View.VISIBLE
                } else {
                    Toast.makeText(this@ScannerActivity, R.string.pending_load_failed, Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    private fun renderPendingSheet(sb: SheetPendingBinding, ctx: ScanContext) {
        sb.tvSumToday.text = Ui.count(ctx.awb.pending)
        sb.tvSumOverdue.text = Ui.count(ctx.awb.overdue)
        sb.tvSumScanned.text = Ui.count(ctx.scannedToday)

        sb.pendingList.removeAllViews()
        if (ctx.queue.isEmpty()) {
            sb.tvPendingEmpty.text = getString(R.string.pending_none)
            sb.tvPendingEmpty.visibility = View.VISIBLE
            sb.tvPendingFooter.visibility = View.GONE
            return
        }
        sb.tvPendingEmpty.visibility = View.GONE

        ctx.queue.forEachIndexed { index, row ->
            if (index > 0) sb.pendingList.addView(divider())
            sb.pendingList.addView(pendingRow(sb.pendingList, row))
        }
        if (ctx.queueTotal > ctx.queue.size) {
            sb.tvPendingFooter.text = getString(R.string.pending_footer_format, ctx.queue.size, Ui.count(ctx.queueTotal))
            sb.tvPendingFooter.visibility = View.VISIBLE
        } else {
            sb.tvPendingFooter.visibility = View.GONE
        }
    }

    private fun pendingRow(parent: ViewGroup, row: PendingAwb): View {
        val rb = ItemPendingRowBinding.inflate(layoutInflater, parent, false)
        rb.tvRowAwb.text = row.awb
        rb.tvRowTag.text = when (row.priority) {
            "Urgent" -> getString(R.string.priority_urgent)
            "High" -> getString(R.string.priority_high)
            else -> getString(R.string.priority_normal)
        }
        when (row.priority) {
            "Urgent" -> Ui.pillRes(rb.tvRowTag, R.color.verdict_stop, R.color.verdict_stop_bg)
            "High" -> Ui.pillRes(rb.tvRowTag, R.color.verdict_check, R.color.verdict_check_bg)
            else -> Ui.pillRes(rb.tvRowTag, R.color.text_secondary, R.color.surface_muted)
        }

        val meta = mutableListOf<String>()
        if (row.orderId.isNotBlank()) meta.add(getString(R.string.order_prefix, row.orderId))
        if (row.courier.isNotBlank()) meta.add(row.courier)
        if (row.skus > 0 || row.units > 0) meta.add(itemsSummary(row.skus, row.units))
        rb.tvRowMeta.text = meta.joinToString(" · ")

        val awbAt = Ui.parseIsoUtc(row.awbGeneratedAt)
        val sla = Ui.parseIsoUtc(row.slaDate)
        if (row.ageDays > 0) {
            val from = awbAt?.let { Ui.shortDate(it) } ?: "-"
            rb.tvRowWhen.text = resources.getQuantityString(R.plurals.overdue_days, row.ageDays, from, row.ageDays)
            rb.tvRowWhen.setTextColor(ContextCompat.getColor(this, R.color.verdict_stop))
        } else if (sla != null) {
            rb.tvRowWhen.text = getString(R.string.ship_by_format, Ui.shortDateTime(sla))
        } else if (awbAt != null) {
            rb.tvRowWhen.text = getString(R.string.awb_generated_format, Ui.shortDateTime(awbAt))
        } else {
            rb.tvRowWhen.visibility = View.GONE
        }
        return rb.root
    }

    private fun divider(): View = View(this).apply {
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, Ui.dp(context, 1))
        setBackgroundColor(ContextCompat.getColor(context, R.color.border))
    }

    // ---- Recent scans sheet -------------------------------------------------------------------

    private fun showRecentScansSheet() {
        val sb = DialogRecentScansBinding.inflate(layoutInflater)
        val sheet = newSheet()
        sheet.setContentView(sb.root)
        sb.tvRecentSub.text = channelName
        sb.btnCloseRecent.setOnClickListener { sheet.dismiss() }
        sb.progressRecent.visibility = View.VISIBLE
        sheet.show()

        lifecycleScope.launch {
            val app = application as ForwardScanApp
            val result = withContext(Dispatchers.IO) {
                app.apiClient.getRecentScans(channelId, limit = 30)
            }
            sb.progressRecent.visibility = View.INVISIBLE
            sb.recentScansContainer.removeAllViews()

            val list = result.getOrNull()
            if (list == null) {
                if (result.exceptionOrNull() is NetworkException) setOnline(false)
                sb.recentScansContainer.addView(messageLine(getString(R.string.recent_load_failed)))
                return@launch
            }
            setOnline(true)
            if (list.isEmpty()) {
                sb.recentScansContainer.addView(messageLine(getString(R.string.recent_empty)))
                return@launch
            }
            list.forEachIndexed { index, scan ->
                if (index > 0) sb.recentScansContainer.addView(divider())
                sb.recentScansContainer.addView(recentRow(sb.recentScansContainer, scan))
            }
        }
    }

    private fun recentRow(parent: ViewGroup, scan: ScanDetails): View {
        val rb = ItemPendingRowBinding.inflate(layoutInflater, parent, false)
        rb.tvRowAwb.text = scan.trackingRaw
        when (scan.result) {
            "OK" -> {
                rb.tvRowTag.text = getString(R.string.result_ok)
                Ui.pillRes(rb.tvRowTag, R.color.verdict_ok, R.color.verdict_ok_bg)
            }
            "UNVERIFIED" -> {
                rb.tvRowTag.text = getString(R.string.result_unverified)
                Ui.pillRes(rb.tvRowTag, R.color.verdict_unverified, R.color.verdict_unverified_bg)
            }
            else -> {
                rb.tvRowTag.text = getString(R.string.result_check)
                Ui.pillRes(rb.tvRowTag, R.color.verdict_check, R.color.verdict_check_bg)
            }
        }
        val who = listOf(scan.scannedAtLocal, scan.user, scan.station).filter { it.isNotBlank() }.joinToString(" · ")
        rb.tvRowMeta.text = who
        if (scan.message.isNotBlank() && scan.result != "OK") {
            rb.tvRowWhen.text = scan.message
        } else {
            rb.tvRowWhen.visibility = View.GONE
        }
        return rb.root
    }

    private fun messageLine(text: String): View = TextView(this).apply {
        this.text = text
        textSize = 14f
        val pad = Ui.dp(context, 24)
        setPadding(0, pad, 0, pad)
        gravity = android.view.Gravity.CENTER
        setTextColor(ContextCompat.getColor(context, R.color.text_secondary))
    }

    private fun goToLogin() {
        startActivity(Intent(this, LoginActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
        })
        finish()
    }

    override fun onDestroy() {
        super.onDestroy()
        if (isTorchOn) camera?.cameraControl?.enableTorch(false)
        cameraProvider?.unbindAll()
        cameraExecutor.shutdown()
        barcodeScanner?.close()
    }
}
