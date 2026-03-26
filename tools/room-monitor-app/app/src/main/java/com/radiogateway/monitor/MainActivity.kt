package com.radiogateway.monitor

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.*
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

class MainActivity : AppCompatActivity() {

    private lateinit var serverUrl: EditText
    private lateinit var statusText: TextView
    private lateinit var statsText: TextView
    private lateinit var levelBar: ProgressBar
    private lateinit var gainSlider: SeekBar
    private lateinit var vadSlider: SeekBar
    private lateinit var vadEnable: CheckBox
    private lateinit var startStopBtn: Button

    private val handler = Handler(Looper.getMainLooper())
    private val updateRunnable = object : Runnable {
        override fun run() {
            updateUI()
            handler.postDelayed(this, 200)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        serverUrl = findViewById(R.id.serverUrl)
        statusText = findViewById(R.id.statusText)
        statsText = findViewById(R.id.statsText)
        levelBar = findViewById(R.id.levelBar)
        gainSlider = findViewById(R.id.gainSlider)
        vadSlider = findViewById(R.id.vadSlider)
        vadEnable = findViewById(R.id.vadEnable)
        startStopBtn = findViewById(R.id.startStopBtn)

        // Gain: slider 0-100 maps to 1x-10x
        gainSlider.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(sb: SeekBar?, progress: Int, fromUser: Boolean) {
                MonitorService.gain = 1.0f + progress * 0.09f  // 1.0 to 10.0
            }
            override fun onStartTrackingTouch(sb: SeekBar?) {}
            override fun onStopTrackingTouch(sb: SeekBar?) {}
        })

        // VAD: slider 0-40 maps to -60dB to -20dB
        vadSlider.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(sb: SeekBar?, progress: Int, fromUser: Boolean) {
                MonitorService.vadThresholdDb = -60f + progress
            }
            override fun onStartTrackingTouch(sb: SeekBar?) {}
            override fun onStopTrackingTouch(sb: SeekBar?) {}
        })

        vadEnable.setOnCheckedChangeListener { _, checked ->
            MonitorService.vadEnabled = checked
        }

        startStopBtn.setOnClickListener {
            if (MonitorService.isRunning) {
                stopMonitoring()
            } else {
                requestPermissionsAndStart()
            }
        }

        // Status update callback
        MonitorService.onStatusChanged = { connected, db, bytes ->
            // Called from service thread — UI updates via handler
        }

        handler.post(updateRunnable)
    }

    override fun onDestroy() {
        handler.removeCallbacks(updateRunnable)
        MonitorService.onStatusChanged = null
        super.onDestroy()
    }

    private fun updateUI() {
        if (MonitorService.isRunning) {
            startStopBtn.text = "STOP"
            startStopBtn.setBackgroundColor(0xFFE74C3C.toInt())

            if (MonitorService.connected) {
                statusText.text = "Live"
                statusText.setTextColor(0xFF2ECC71.toInt())
            } else {
                statusText.text = "Reconnecting..."
                statusText.setTextColor(0xFFF39C12.toInt())
            }

            // Level bar: map -60..0 dB to 0..100
            val db = MonitorService.currentDb
            val pct = ((db + 60) / 60 * 100).coerceIn(0f, 100f).toInt()
            levelBar.progress = pct

            // Stats
            val elapsed = (System.currentTimeMillis() - MonitorService.startTime) / 1000
            val min = elapsed / 60
            val sec = elapsed % 60
            val kb = MonitorService.bytesSent / 1024
            val mb = kb / 1024
            val sizeStr = if (mb > 0) "${mb} MB" else "${kb} KB"
            statsText.text = "Duration: ${min}:${String.format("%02d", sec)} | Sent: $sizeStr | ${String.format("%.0f", db)} dB"
        } else {
            startStopBtn.text = "START"
            startStopBtn.setBackgroundColor(0xFF0F3460.toInt())
            statusText.text = "Disconnected"
            statusText.setTextColor(0xFFE74C3C.toInt())
            levelBar.progress = 0
            statsText.text = "Duration: 0:00 | Sent: 0 KB"
        }
    }

    private fun requestPermissionsAndStart() {
        val perms = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            perms.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        val needed = perms.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (needed.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, needed.toTypedArray(), 100)
        } else {
            startMonitoring()
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 100 && grantResults.all { it == PackageManager.PERMISSION_GRANTED }) {
            startMonitoring()
        } else {
            Toast.makeText(this, "Mic permission required", Toast.LENGTH_SHORT).show()
        }
    }

    private fun startMonitoring() {
        val url = serverUrl.text.toString().trim()
        if (url.isEmpty()) {
            Toast.makeText(this, "Enter gateway URL", Toast.LENGTH_SHORT).show()
            return
        }
        val intent = Intent(this, MonitorService::class.java).apply {
            putExtra("url", url)
        }
        ContextCompat.startForegroundService(this, intent)
    }

    private fun stopMonitoring() {
        stopService(Intent(this, MonitorService::class.java))
    }
}
