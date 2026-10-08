package shop.youthnic.scan.ui

import android.Manifest
import android.annotation.SuppressLint
import android.app.Dialog
import android.content.Intent
import android.content.pm.PackageManager
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CaptureRequest
import android.media.AudioManager
import android.os.Bundle
import android.os.SystemClock
import android.util.Range
import android.util.Size
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.view.animation.OvershootInterpolator
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.annotation.ColorInt
import androidx.annotation.ColorRes
import androidx.annotation.DrawableRes
import androidx.annotation.OptIn
import androidx.annotation.StringRes
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.camera2.interop.Camera2CameraInfo
import androidx.camera.camera2.interop.Camera2Interop
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.CameraState
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.core.widget.ImageViewCompat
import androidx.lifecycle.Lifecycle
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
import shop.youthnic.scan.databinding.ItemSignalRowBinding
import shop.youthnic.scan.databinding.SheetPendingBinding
import shop.youthnic.scan.update.AppUpdater
import shop.youthnic.scan.util.CameraTuning
import shop.youthnic.scan.util.Cue
import shop.youthnic.scan.util.ScanSignals
import shop.youthnic.scan.util.Ui
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

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

    /** Camera open (bound) or closed by the idle pause; [boundSharp] = opened at 1080p. */
    private var cameraRunning = false
    private var boundSharp = false
    /** This phone rejected the frame-rate cap once: open the camera without it from now on. */
    private var fpsCapFailed = false
    /** Dialogs / sheets currently covering the camera (read on the camera thread). */
    private val openDialogs = AtomicInteger(0)
    private var lastAnalyzedAt = 0L
    private var lastActivityAt = 0L
    private var idleJob: Job? = null

    /** Full-screen blink in the result's colour, laid over the whole screen in [initUi]. */
    private lateinit var flashView: View
    private var flashJob: Job? = null
    private var stampJob: Job? = null

    /** Results since the scanner was opened (by [Cue]) and the running scan number shown on the card. */
    private val tally = IntArray(Cue.values().size)
    private var scanSeq = 0
    private val clock = SimpleDateFormat("HH:mm:ss", Locale.US)

    /** Colour, icon and words of one result: shared by the stamp, the card banner, the legend and the tally. */
    private class SignalStyle(
        @ColorRes val color: Int,
        @DrawableRes val icon: Int,
        @StringRes val stamp: Int,
        @StringRes val legend: Int,
        val stampHoldMs: Long
    )

    private fun styleOf(cue: Cue): SignalStyle = when (cue) {
        Cue.OK -> SignalStyle(R.color.verdict_ok, R.drawable.ic_signal_ok, R.string.signal_ok, R.string.legend_ok, 1100)
        Cue.DUPLICATE -> SignalStyle(R.color.verdict_duplicate, R.drawable.ic_signal_duplicate, R.string.signal_duplicate, R.string.legend_duplicate, 1900)
        Cue.NOT_FOUND -> SignalStyle(R.color.verdict_unverified, R.drawable.ic_signal_not_found, R.string.signal_not_found, R.string.legend_not_found, 1900)
        Cue.CHECK -> SignalStyle(R.color.verdict_check, R.drawable.ic_signal_check, R.string.signal_check, R.string.legend_check, 1900)
        Cue.STOP -> SignalStyle(R.color.verdict_stop, R.drawable.ic_signal_stop, R.string.signal_stop, R.string.legend_stop, 2200)
    }

    companion object {
        const val EXTRA_CHANNEL_ID = "extra_channel_id"
        const val EXTRA_CHANNEL_NAME = "extra_channel_name"
        const val EXTRA_CHANNEL_COLOR = "extra_channel_color"

        /** Header numbers and the update check refresh this often while the scanner is open. */
        private const val REFRESH_EVERY_MS = 60_000L
        private const val PENDING_ROWS = 50
        private const val FRAME_MS = 16L
        private const val IDLE_CHECK_MS = 5_000L
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
        volumeControlStream = AudioManager.STREAM_MUSIC  // the volume keys set the beep volume here

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
        noteActivity()
        // Back from Settings with another camera quality, or the phone woken up after an idle pause: open the
        // camera again so the packer can scan straight away. (The first start is startCamera() in onCreate.)
        if (cameraProvider != null && hasCameraPermission()) {
            val app = application as ForwardScanApp
            if (!cameraRunning || boundSharp != app.sessionManager.isSharpCamera) resumeCamera()
        }
        updateKeepScreenOn()  // the idle pause may have been switched off in Settings
        startIdleWatch()
    }

    override fun onPause() {
        super.onPause()
        periodicJob?.cancel()
        periodicJob = null
        idleJob?.cancel()
        idleJob = null
    }

    override fun dispatchTouchEvent(ev: MotionEvent): Boolean {
        if (ev.actionMasked == MotionEvent.ACTION_DOWN) noteActivity()
        return super.dispatchTouchEvent(ev)
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
        binding.layoutCameraPaused.setOnClickListener { resumeCamera() }

        binding.cardResult.visibility = View.GONE
        binding.cardIdleState.visibility = View.VISIBLE

        // Not clickable, so touches pass through to the screen below while it blinks.
        flashView = View(this).apply {
            alpha = 0f
            visibility = View.GONE
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
        }
        addContentView(flashView, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        bindLegendRow(binding.legendOk, Cue.OK)
        bindLegendRow(binding.legendDuplicate, Cue.DUPLICATE)
        bindLegendRow(binding.legendNotFound, Cue.NOT_FOUND)
        bindLegendRow(binding.legendCheck, Cue.CHECK)
        bindLegendRow(binding.legendStop, Cue.STOP)
        (application as ForwardScanApp).soundManager.prepare()
    }

    /** Idle-card legend: what each result looks, sounds and feels like; a tap plays it (not counted). */
    private fun bindLegendRow(row: ItemSignalRowBinding, cue: Cue) {
        val style = styleOf(cue)
        val color = ContextCompat.getColor(this, style.color)
        Ui.tint(row.ivSignalIcon, color)
        row.ivSignalIcon.setImageResource(style.icon)
        row.tvSignalName.text = getString(style.stamp)
        row.tvSignalName.setTextColor(color)
        row.tvSignalDesc.text = getString(style.legend)
        row.root.setOnClickListener { signal(cue, getString(style.stamp)) }
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

    /** "Not more than" [size] (sensor orientation, 16:9 first): CameraX takes the closest size at or below it. */
    private fun sizeAtMost(size: Pair<Int, Int>): ResolutionSelector = ResolutionSelector.Builder()
        .setAspectRatioStrategy(AspectRatioStrategy.RATIO_16_9_FALLBACK_AUTO_STRATEGY)
        .setResolutionStrategy(
            ResolutionStrategy(Size(size.first, size.second), ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER)
        )
        .build()

    /**
     * Opens the camera within the battery limits of [CameraTuning]: preview and barcode reading at 720p
     * (1080p only with Settings -> Sharper camera), the sensor capped at ~24 fps when the phone allows it,
     * the cheaper SurfaceView preview, and the screen kept on only while the camera runs.
     */
    @OptIn(ExperimentalCamera2Interop::class)
    private fun bindCameraUseCases() {
        val provider = cameraProvider ?: return
        val session = (application as ForwardScanApp).sessionManager
        val sharp = session.isSharpCamera

        binding.previewView.implementationMode = PreviewView.ImplementationMode.PERFORMANCE
        val previewBuilder = Preview.Builder().setResolutionSelector(sizeAtMost(CameraTuning.PREVIEW))
        val fps = if (fpsCapFailed) null else supportedFpsCap(provider)
        if (fps != null) {
            Camera2Interop.Extender(previewBuilder)
                .setCaptureRequestOption(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, Range(fps.first, fps.second))
        }
        val preview = previewBuilder.build().also {
            it.setSurfaceProvider(binding.previewView.surfaceProvider)
        }

        val imageAnalysis = ImageAnalysis.Builder()
            .setResolutionSelector(sizeAtMost(CameraTuning.analysisSize(sharp)))
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .build()

        imageAnalysis.setAnalyzer(cameraExecutor) { imageProxy ->
            processImageProxy(imageProxy)
        }

        try {
            provider.unbindAll()
            val cam = provider.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, preview, imageAnalysis)
            camera = cam
            cameraRunning = true
            boundSharp = sharp
            setTorchUi(false)
            binding.layoutCameraPaused.visibility = View.GONE
            binding.tvScannerHint.text = getString(R.string.aim_at_awb)
            updateKeepScreenOn()
            imageAnalysis.resolutionInfo?.resolution?.let {
                session.lastCameraInfo = CameraTuning.describe(it.width, it.height, fps)
            }
            // A phone that rejects the frame-rate cap reports a camera error: open it once more without the cap.
            cam.cameraInfo.cameraState.removeObservers(this)
            if (fps != null) {
                cam.cameraInfo.cameraState.observe(this) { state ->
                    val code = state.error?.code
                    val fromCap = code == CameraState.ERROR_STREAM_CONFIG || code == CameraState.ERROR_CAMERA_FATAL_ERROR
                    if (fromCap && !fpsCapFailed && cameraRunning) {
                        fpsCapFailed = true
                        bindCameraUseCases()
                    }
                }
            }
        } catch (e: Exception) {
            cameraRunning = false
            camera = null
            setTorchUi(false)
            updateKeepScreenOn()
            binding.tvScannerHint.text = "Camera bind error: ${e.message}"
            binding.tvCameraPausedSub.text = e.message ?: ""
            binding.layoutCameraPaused.visibility = View.VISIBLE  // "Tap to scan" retries
        }
    }

    /** The phone's own frame-rate range closest under [CameraTuning.MAX_FPS], or null to keep its default. */
    @OptIn(ExperimentalCamera2Interop::class)
    private fun supportedFpsCap(provider: ProcessCameraProvider): Pair<Int, Int>? = try {
        val info = CameraSelector.DEFAULT_BACK_CAMERA.filter(provider.availableCameraInfos).firstOrNull()
        val ranges = info?.let {
            Camera2CameraInfo.from(it).getCameraCharacteristic(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES)
        }
        CameraTuning.pickFpsRange(ranges.orEmpty().map { it.lower to it.upper })
    } catch (_: Exception) {
        null
    }

    private fun hasCameraPermission(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED

    // ---- idle pause: the camera closes after a while without scans ---------------------------------

    /** A scan or a touch: the packer is working, keep the camera open. */
    private fun noteActivity() {
        lastActivityAt = SystemClock.elapsedRealtime()
    }

    private fun startIdleWatch() {
        idleJob?.cancel()
        idleJob = lifecycleScope.launch {
            while (isActive) {
                delay(IDLE_CHECK_MS)
                val autoPause = (application as ForwardScanApp).sessionManager.isAutoPauseCamera
                val idleFor = SystemClock.elapsedRealtime() - lastActivityAt
                if (cameraRunning && autoPause && idleFor >= CameraTuning.IDLE_PAUSE_MS && !isRequestInFlight.get()) {
                    pauseCamera()
                }
            }
        }
    }

    /** Closes the camera (sensor, ML Kit and torch all stop) and lets the screen go to sleep as usual. */
    private fun pauseCamera() {
        if (!cameraRunning) return
        cameraRunning = false
        camera?.cameraInfo?.cameraState?.removeObservers(this)
        cameraProvider?.unbindAll()
        camera = null
        setTorchUi(false)
        binding.tvCameraPausedSub.text = getString(R.string.camera_paused_sub, CameraTuning.IDLE_PAUSE_MINUTES)
        binding.layoutCameraPaused.visibility = View.VISIBLE
        updateKeepScreenOn()
    }

    private fun resumeCamera() {
        noteActivity()
        if (!hasCameraPermission()) return
        binding.layoutCameraPaused.visibility = View.GONE
        if (cameraProvider == null) startCamera() else bindCameraUseCases()
    }

    /**
     * Screen stays on while the camera is open (packers rarely touch the phone between scans), but only when
     * the idle pause is on - then it is never longer than [CameraTuning.IDLE_PAUSE_MS] after the last scan.
     */
    private fun updateKeepScreenOn() {
        val autoPause = (application as ForwardScanApp).sessionManager.isAutoPauseCamera
        if (cameraRunning && autoPause) {
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
    }

    /** Dialogs and sheets cover the camera: stop reading barcodes behind them (no accidental scan, less CPU). */
    private fun blockAnalysisWhileShown(dialog: Dialog, onDismiss: () -> Unit = {}) {
        openDialogs.incrementAndGet()
        dialog.setOnDismissListener {
            if (openDialogs.decrementAndGet() < 0) openDialogs.set(0)  // (updateAndGet needs API 24)
            onDismiss()
            // Touches on a sheet go to its own window, not to dispatchTouchEvent: someone reading the Pending list
            // for 2+ minutes is not idle. If the idle pause closed the camera behind it, open it again.
            if (!cameraRunning && cameraProvider != null && hasCameraPermission() &&
                lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)
            ) {
                resumeCamera()
            } else {
                noteActivity()
            }
        }
    }

    @SuppressLint("UnsafeOptInUsageError")
    private fun processImageProxy(imageProxy: ImageProxy) {
        val scanner = barcodeScanner
        val mediaImage = imageProxy.image
        val now = SystemClock.elapsedRealtime()

        // Battery: read at most every ANALYZE_EVERY_MS, and not at all while a scan is being sent, right after
        // a scan (every code is ignored during the cooldown anyway) or behind a dialog.
        if (mediaImage == null || scanner == null || isRequestInFlight.get() || openDialogs.get() > 0 ||
            now - lastAnalyzedAt < CameraTuning.ANALYZE_EVERY_MS || duplicateGuard.inCooldown()
        ) {
            imageProxy.close()
            return
        }
        lastAnalyzedAt = now

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
        noteActivity()

        val app = application as ForwardScanApp
        val apiClient = app.apiClient
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
                refreshSoon()
            } else {
                when (val ex = result.exceptionOrNull()) {
                    is NetworkException -> if (ex.isOffline) displayNetworkError(rawAwb) else displayNotConfirmed(rawAwb)
                    is AuthExpiredException -> goToLogin()
                    else -> {
                        setOnline(true)
                        displayGeneralError(rawAwb, ex?.message ?: "Scan rejected")
                    }
                }
            }
        }
    }

    // ---- result signals: card banner, stamp over the camera, screen blink, beep, vibration ----------

    /**
     * Shows one result everywhere at once, so a packer knows it without reading: the card banner (colour, icon,
     * title, scan number + time, what to do now) and [signal]. [counted] = a real scan answer from the server
     * (goes into the session tally); offline / rejected requests are not counted.
     */
    private fun showVerdict(cue: Cue, title: String, message: String, action: String, stampText: String, counted: Boolean) {
        val style = styleOf(cue)
        val color = ContextCompat.getColor(this, style.color)
        binding.cardIdleState.visibility = View.GONE
        binding.cardResult.visibility = View.VISIBLE
        binding.bannerVerdict.setBackgroundColor(color)
        binding.cardResult.strokeColor = color
        binding.ivVerdictIcon.setImageResource(style.icon)
        binding.tvVerdictTitle.text = title
        binding.tvVerdictMessage.text = message
        binding.tvVerdictMessage.visibility = if (message.isBlank()) View.GONE else View.VISIBLE
        binding.tvVerdictAction.text = action
        binding.tvVerdictAction.visibility = if (action.isBlank()) View.GONE else View.VISIBLE

        val time = clock.format(Date())
        if (counted) {
            scanSeq++
            tally[cue.ordinal]++
            renderTally()
        }
        binding.tvScanSeq.text = if (counted) getString(R.string.scan_seq_format, scanSeq, time) else time
        binding.scrollResultArea.scrollTo(0, 0)

        // A small "pop" so a second OK in a row visibly replaces the first one.
        val card = binding.cardResult
        card.animate().cancel()
        card.scaleX = 0.96f
        card.scaleY = 0.96f
        card.animate().scaleX(1f).scaleY(1f).setStartDelay(0).setDuration(180).start()

        signal(cue, stampText)
    }

    /** Beep + vibration, the big stamp over the camera and the full-screen blink for [cue]. */
    private fun signal(cue: Cue, stampText: String) {
        val style = styleOf(cue)
        val color = ContextCompat.getColor(this, style.color)
        (application as ForwardScanApp).soundManager.play(cue)
        showStamp(style, color, stampText)
        flash(cue, color)
    }

    private fun showStamp(style: SignalStyle, @ColorInt color: Int, text: String) {
        val stamp = binding.layoutSignalStamp
        stampJob?.cancel()
        stamp.animate().cancel()
        Ui.tint(stamp, color)
        binding.ivSignalStamp.setImageResource(style.icon)
        binding.tvSignalStamp.text = text
        stamp.alpha = 0f
        stamp.scaleX = 0.7f
        stamp.scaleY = 0.7f
        stamp.visibility = View.VISIBLE
        stamp.animate().alpha(1f).scaleX(1f).scaleY(1f).setStartDelay(0).setDuration(180)
            .setInterpolator(OvershootInterpolator()).start()
        // Timed with a coroutine, not an animator delay: it still hides when Android animations are switched off.
        stampJob = lifecycleScope.launch {
            delay(style.stampHoldMs)
            stamp.animate().alpha(0f).setStartDelay(0).setDuration(220)
                .withEndAction { stamp.visibility = View.GONE }.start()
        }
    }

    /** Blinks the whole screen (ScanSignals.flashLevel); frame-timed so it works with animations switched off. */
    private fun flash(cue: Cue, @ColorInt color: Int) {
        flashJob?.cancel()
        if (!(application as ForwardScanApp).sessionManager.isFlashEnabled) return
        flashJob = lifecycleScope.launch {
            flashView.setBackgroundColor(color)
            flashView.alpha = 0f
            flashView.visibility = View.VISIBLE
            val total = ScanSignals.flashDurationMs(cue)
            val start = SystemClock.uptimeMillis()
            try {
                while (true) {
                    val t = (SystemClock.uptimeMillis() - start).toInt()
                    if (t >= total) break
                    flashView.alpha = ScanSignals.flashLevel(cue, t) * ScanSignals.FLASH_MAX_ALPHA
                    delay(FRAME_MS)
                }
            } finally {
                // A newer scan's blink owns the overlay now: leave it alone.
                if (flashJob === coroutineContext[Job]) {
                    flashView.alpha = 0f
                    flashView.visibility = View.GONE
                }
            }
        }
    }

    private fun renderTally() {
        binding.scrollTally.visibility = View.VISIBLE
        binding.tvTallyOk.text = getString(R.string.tally_ok, Ui.count(tally[Cue.OK.ordinal]))
        binding.tvTallyDuplicate.text = getString(R.string.tally_duplicate, Ui.count(tally[Cue.DUPLICATE.ordinal]))
        binding.tvTallyNotFound.text = getString(R.string.tally_not_found, Ui.count(tally[Cue.NOT_FOUND.ordinal]))
        tallyPill(binding.tvTallyCheck, R.string.tally_check, tally[Cue.CHECK.ordinal])
        tallyPill(binding.tvTallyStop, R.string.tally_stop, tally[Cue.STOP.ordinal])
    }

    /** Check and Stop only appear once they happen. */
    private fun tallyPill(view: TextView, @StringRes format: Int, count: Int) {
        view.text = getString(format, Ui.count(count))
        view.visibility = if (count > 0) View.VISIBLE else View.GONE
    }

    private fun titleFor(verdict: VerdictType): String = getString(
        when (verdict) {
            VerdictType.OK -> R.string.verdict_verified
            VerdictType.CHECK -> R.string.verdict_check
            VerdictType.DUPLICATE -> R.string.verdict_duplicate
            VerdictType.NOT_IN_OMS -> R.string.verdict_not_in_oms
            VerdictType.STOP, VerdictType.ERROR -> R.string.verdict_stop
            VerdictType.WRONG_BARCODE -> R.string.verdict_wrong_barcode
        }
    )

    /** What to do with the packet now (the same advice as the web scan page). */
    private fun actionFor(response: ScanResponse): String = getString(
        when (response.verdictType) {
            VerdictType.OK -> if (response.code == "ALREADY_SAVED") R.string.action_already_saved else R.string.action_ok
            VerdictType.DUPLICATE -> R.string.action_duplicate
            VerdictType.NOT_IN_OMS -> R.string.action_not_found
            VerdictType.CHECK -> when (response.code) {
                "NOT_RTS" -> R.string.action_check_not_rts
                "PARTIAL_CANCEL" -> R.string.action_check_partial_cancel
                "STATUS_CHANGED" -> R.string.action_check_status_changed
                "CHANNEL_UNMAPPED" -> R.string.action_check_channel_unmapped
                else -> R.string.action_check
            }
            VerdictType.STOP, VerdictType.ERROR -> when (response.code) {
                "ALERT" -> R.string.action_alert
                "REPLACED" -> R.string.action_old_label
                else -> R.string.action_stop
            }
            VerdictType.WRONG_BARCODE -> R.string.action_wrong_barcode
        }
    )

    private fun displayScanResult(awb: String, response: ScanResponse) {
        val vType = response.verdictType
        val cue = ScanSignals.cueFor(vType)
        val message = when {
            vType == VerdictType.CHECK -> "${getString(R.string.what_to_check)} ${response.message}"
            vType == VerdictType.OK && response.message.equals("Verified", ignoreCase = true) -> ""  // title says it
            // "DUPLICATE - already scanned on ... by ..." -> the title already says DUPLICATE
            vType == VerdictType.DUPLICATE -> response.message.removePrefix("DUPLICATE - ").replaceFirstChar { it.uppercase() }
            else -> response.message
        }
        val wrong = vType == VerdictType.WRONG_BARCODE
        // the packer's own scan of a moment ago, sent again (no answer in time): OK, but already in the tally
        val repeat = response.code == "ALREADY_SAVED"
        val stamp = getString(if (wrong) R.string.signal_wrong_barcode else styleOf(cue).stamp)
        // nothing was saved for a wrong barcode: not a scan, not in the tally / scan number
        showVerdict(cue, titleFor(vType), message, actionFor(response), stamp, counted = !wrong && !repeat)
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

    /** Sent, but no answer in time: it may be saved. Rescanning shows the result (the server answers OK for
     *  the same packer's own scan a moment ago - never "Duplicate - set aside"). */
    private fun displayNotConfirmed(awb: String) {
        showVerdict(
            Cue.CHECK, getString(R.string.verdict_not_confirmed), getString(R.string.not_confirmed_message),
            getString(R.string.action_not_confirmed), getString(R.string.signal_not_confirmed), counted = false
        )
        binding.tvResultAwb.text = awb
        binding.tvResultOrderId.text = "-"
        binding.tvResultCourier.text = "-"
        binding.tvResultItemsCount.text = "-"
        binding.layoutItemsList.removeAllViews()
    }

    private fun displayNetworkError(awb: String) {
        showVerdict(
            Cue.STOP, getString(R.string.verdict_offline), getString(R.string.connection_offline),
            getString(R.string.action_offline), getString(R.string.signal_not_sent), counted = false
        )
        binding.tvResultAwb.text = awb
        binding.tvResultOrderId.text = getString(R.string.not_submitted)
        binding.tvResultCourier.text = "-"
        binding.tvResultItemsCount.text = getString(R.string.check_connection_rescan)
        binding.layoutItemsList.removeAllViews()
        setOnline(false)
    }

    private fun displayGeneralError(awb: String, errorMsg: String) {
        showVerdict(
            Cue.STOP, getString(R.string.verdict_stop), errorMsg, getString(R.string.action_stop),
            getString(R.string.signal_stop), counted = false
        )
        binding.tvResultAwb.text = awb
        binding.tvResultOrderId.text = "-"
        binding.tvResultCourier.text = "-"
        binding.tvResultItemsCount.text = "-"
        binding.layoutItemsList.removeAllViews()
    }

    private fun toggleTorch() {
        val cam = camera ?: return
        setTorchUi(!isTorchOn)
        cam.cameraControl.enableTorch(isTorchOn)
    }

    /** Torch button state; a newly opened or closed camera always starts with the torch off. */
    private fun setTorchUi(on: Boolean) {
        isTorchOn = on
        val color = ContextCompat.getColor(this, if (on) R.color.colorPrimary else R.color.text_primary)
        binding.tvTorchLabel.text = getString(if (on) R.string.torch_on else R.string.torch_off)
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

        blockAnalysisWhileShown(dialog)
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
        blockAnalysisWhileShown(sheet) { if (pendingSheet === sb) pendingSheet = null }
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
        if (row.shippedInOms) meta.add(0, getString(R.string.pending_shipped_in_oms))
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
        blockAnalysisWhileShown(sheet)
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
