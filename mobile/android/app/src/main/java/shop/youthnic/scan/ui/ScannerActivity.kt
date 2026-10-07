package shop.youthnic.scan.ui

import android.Manifest
import android.annotation.SuppressLint
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Bundle
import android.util.Size
import android.view.LayoutInflater
import android.view.View
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.textfield.TextInputEditText
import com.google.mlkit.vision.barcode.BarcodeScanner
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import shop.youthnic.scan.ForwardScanApp
import shop.youthnic.scan.R
import shop.youthnic.scan.data.BarcodeRules
import shop.youthnic.scan.data.DuplicateGuard
import shop.youthnic.scan.data.NetworkException
import shop.youthnic.scan.data.ScanDetails
import shop.youthnic.scan.data.ScanResponse
import shop.youthnic.scan.data.VerdictType
import shop.youthnic.scan.databinding.ActivityScannerBinding
import shop.youthnic.scan.databinding.DialogManualScanBinding
import shop.youthnic.scan.databinding.DialogRecentScansBinding
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

    private var scannedCountToday = 0
    private var pendingCount = 0

    companion object {
        const val EXTRA_CHANNEL_ID = "extra_channel_id"
        const val EXTRA_CHANNEL_NAME = "extra_channel_name"
        const val EXTRA_CHANNEL_COLOR = "extra_channel_color"
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

    private fun initUi() {
        binding.tvScannerChannelName.text = channelName
        try {
            if (channelColor.isNotBlank()) {
                val colorInt = Color.parseColor(channelColor)
                binding.viewChannelColor.setBackgroundColor(colorInt)
            }
        } catch (_: Exception) {}

        binding.btnBack.setOnClickListener {
            finish()
        }

        binding.btnTorch.setOnClickListener {
            toggleTorch()
        }

        binding.btnManual.setOnClickListener {
            showManualScanDialog()
        }

        binding.btnRecent.setOnClickListener {
            showRecentScansDialog()
        }

        binding.cardResult.visibility = View.GONE
        binding.cardIdleState.visibility = View.VISIBLE
    }

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

        val cameraSelector = CameraSelector.DEFAULT_BACK_CAMERA

        try {
            provider.unbindAll()
            camera = provider.bindToLifecycle(this, cameraSelector, preview, imageAnalysis)
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
                displayScanResult(rawAwb, scanResponse)
                soundManager.playFeedback(scanResponse.verdictType)
                if (scanResponse.verdictType == VerdictType.OK || scanResponse.verdictType == VerdictType.CHECK) {
                    scannedCountToday++
                    if (pendingCount > 0) pendingCount--
                    updateProgressCounters()
                }
            } else {
                val ex = result.exceptionOrNull()
                if (ex is NetworkException) {
                    displayNetworkError(rawAwb)
                    soundManager.playFeedback(VerdictType.ERROR)
                } else {
                    displayGeneralError(rawAwb, ex?.message ?: "Scan rejected")
                    soundManager.playFeedback(VerdictType.ERROR)
                }
            }
        }
    }

    private fun displayScanResult(awb: String, response: ScanResponse) {
        binding.cardIdleState.visibility = View.GONE
        binding.cardResult.visibility = View.VISIBLE

        val vType = response.verdictType
        val bannerColor = when (vType) {
            VerdictType.OK -> ContextCompat.getColor(this, R.color.verdict_ok)
            VerdictType.CHECK -> ContextCompat.getColor(this, R.color.verdict_check)
            VerdictType.STOP -> ContextCompat.getColor(this, R.color.verdict_stop)
            VerdictType.DUPLICATE -> ContextCompat.getColor(this, R.color.verdict_duplicate)
            VerdictType.NOT_IN_OMS -> ContextCompat.getColor(this, R.color.verdict_unverified)
            VerdictType.ERROR -> ContextCompat.getColor(this, R.color.verdict_stop)
        }

        binding.bannerVerdict.setBackgroundColor(bannerColor)
        binding.cardResult.strokeColor = bannerColor

        binding.tvVerdictTitle.text = when (vType) {
            VerdictType.OK -> getString(R.string.verdict_verified)
            VerdictType.CHECK -> getString(R.string.verdict_check)
            VerdictType.STOP -> getString(R.string.verdict_stop)
            VerdictType.DUPLICATE -> getString(R.string.verdict_duplicate)
            VerdictType.NOT_IN_OMS -> getString(R.string.verdict_not_in_oms)
            VerdictType.ERROR -> "SCAN ERROR"
        }

        val messageText = if (vType == VerdictType.CHECK) {
            "${getString(R.string.what_to_check)} ${response.message}"
        } else {
            response.message
        }
        binding.tvVerdictMessage.text = messageText
        binding.tvResultAwb.text = awb

        val order = response.order
        if (order != null) {
            binding.tvResultOrderId.text = order.channelOrderId.ifBlank { "ID: ${order.id}" }
            binding.tvResultItemsCount.text = "${order.itemCount} SKU (${order.totalQty} Units)"

            binding.layoutItemsList.removeAllViews()
            val inflater = LayoutInflater.from(this)
            for (item in order.items.take(4)) {
                val tvItem = TextView(this).apply {
                    text = "• ${item.sku} (x${item.qty}) ${item.title.take(35)}"
                    textSize = 12f
                    setTextColor(ContextCompat.getColor(context, R.color.text_secondary))
                    setPadding(0, 4, 0, 4)
                }
                binding.layoutItemsList.addView(tvItem)
            }
        } else {
            binding.tvResultOrderId.text = "-"
            binding.tvResultItemsCount.text = "-"
            binding.layoutItemsList.removeAllViews()
        }
    }

    private fun displayNetworkError(awb: String) {
        binding.cardIdleState.visibility = View.GONE
        binding.cardResult.visibility = View.VISIBLE

        val red = ContextCompat.getColor(this, R.color.verdict_stop)
        binding.bannerVerdict.setBackgroundColor(red)
        binding.cardResult.strokeColor = red

        binding.tvVerdictTitle.text = "OFFLINE"
        binding.tvVerdictMessage.text = getString(R.string.connection_offline)
        binding.tvResultAwb.text = awb
        binding.tvResultOrderId.text = "Not submitted"
        binding.tvResultItemsCount.text = "Check connection and rescan"
        binding.layoutItemsList.removeAllViews()

        binding.tvConnectionStatus.text = "Offline"
        binding.tvConnectionStatus.setTextColor(red)
        binding.tvConnectionStatus.setBackgroundColor(ContextCompat.getColor(this, R.color.verdict_stop_bg))
    }

    private fun displayGeneralError(awb: String, errorMsg: String) {
        binding.cardIdleState.visibility = View.GONE
        binding.cardResult.visibility = View.VISIBLE

        val red = ContextCompat.getColor(this, R.color.verdict_stop)
        binding.bannerVerdict.setBackgroundColor(red)
        binding.cardResult.strokeColor = red

        binding.tvVerdictTitle.text = getString(R.string.verdict_stop)
        binding.tvVerdictMessage.text = errorMsg
        binding.tvResultAwb.text = awb
        binding.tvResultOrderId.text = "-"
        binding.tvResultItemsCount.text = "-"
        binding.layoutItemsList.removeAllViews()
    }

    private fun updateProgressCounters() {
        binding.tvProgressScanned.text = "Scanned: $scannedCountToday"
        binding.tvProgressPending.text = "$pendingCount pending"
        val total = scannedCountToday + pendingCount
        if (total > 0) {
            val pct = (scannedCountToday * 100) / total
            binding.progressBarScans.progress = pct
        }
    }

    private fun toggleTorch() {
        val cam = camera ?: return
        isTorchOn = !isTorchOn
        cam.cameraControl.enableTorch(isTorchOn)
        binding.btnTorch.text = if (isTorchOn) getString(R.string.torch_off) else getString(R.string.torch_on)
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

    private fun showRecentScansDialog() {
        val dialogBinding = DialogRecentScansBinding.inflate(layoutInflater)
        val dialog = MaterialAlertDialogBuilder(this)
            .setView(dialogBinding.root)
            .create()

        dialogBinding.btnCloseRecent.setOnClickListener {
            dialog.dismiss()
        }

        dialogBinding.progressRecent.visibility = View.VISIBLE

        lifecycleScope.launch {
            val app = application as ForwardScanApp
            val result = withContext(Dispatchers.IO) {
                app.apiClient.getRecentScans(channelId, limit = 25)
            }

            dialogBinding.progressRecent.visibility = View.GONE

            if (result.isSuccess) {
                val list = result.getOrNull().orEmpty()
                if (list.isEmpty()) {
                    val tv = TextView(this@ScannerActivity).apply {
                        text = "No scans yet today for this channel"
                        setPadding(16, 24, 16, 24)
                        setTextColor(ContextCompat.getColor(context, R.color.text_muted))
                    }
                    dialogBinding.recentScansContainer.addView(tv)
                } else {
                    for (scan in list) {
                        val tv = TextView(this@ScannerActivity).apply {
                            text = "${scan.trackingRaw} [${scan.result}] • ${scan.scannedAtLocal}"
                            textSize = 13f
                            setTextColor(
                                if (scan.result == "OK") ContextCompat.getColor(context, R.color.verdict_ok)
                                else ContextCompat.getColor(context, R.color.verdict_check)
                            )
                            setPadding(8, 12, 8, 12)
                        }
                        dialogBinding.recentScansContainer.addView(tv)
                    }
                }
            }
        }

        dialog.show()
    }

    override fun onDestroy() {
        super.onDestroy()
        if (isTorchOn) camera?.cameraControl?.enableTorch(false)
        cameraProvider?.unbindAll()
        cameraExecutor.shutdown()
        barcodeScanner?.close()
    }
}
