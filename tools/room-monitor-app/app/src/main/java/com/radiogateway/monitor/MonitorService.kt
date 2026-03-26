package com.radiogateway.monitor

import android.app.*
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import org.java_websocket.client.WebSocketClient
import org.java_websocket.handshake.ServerHandshake
import java.net.URI
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.abs
import kotlin.math.log10
import kotlin.math.sqrt

class MonitorService : Service() {

    companion object {
        const val TAG = "RoomMonitor"
        const val CHANNEL_ID = "room_monitor"
        const val NOTIFICATION_ID = 1
        const val SAMPLE_RATE = 48000
        const val CHUNK_SAMPLES = 2400  // 50ms at 48kHz
        const val CHUNK_BYTES = CHUNK_SAMPLES * 2  // 16-bit = 2 bytes/sample

        var isRunning = false
        var serverUrl = ""
        var gain = 2.0f
        var vadEnabled = true
        var vadThresholdDb = -45f
        var bytesSent = 0L
        var startTime = 0L
        var currentDb = -100f
        var connected = false

        // Callbacks for UI updates
        var onStatusChanged: ((Boolean, Float, Long) -> Unit)? = null
    }

    private var audioRecord: AudioRecord? = null
    private var wsClient: WebSocketClient? = null
    private var recordThread: Thread? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var running = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val url = intent?.getStringExtra("url") ?: return START_NOT_STICKY

        serverUrl = url
        isRunning = true
        running = true
        bytesSent = 0
        startTime = System.currentTimeMillis()

        // Foreground service with persistent notification
        val notification = buildNotification("Connecting...")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(NOTIFICATION_ID, notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }

        // Wake lock to prevent CPU sleep
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "RoomMonitor::Audio")
        wakeLock?.acquire()

        // Start audio capture + WebSocket in background thread
        recordThread = Thread { captureLoop(url) }.apply {
            name = "monitor-capture"
            start()
        }

        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        isRunning = false
        recordThread?.join(3000)
        wsClient?.close()
        audioRecord?.release()
        wakeLock?.release()
        stopForeground(STOPFOREGROUND_REMOVE)
        super.onDestroy()
    }

    private fun captureLoop(url: String) {
        // Open mic with UNPROCESSED source for raw audio
        val bufSize = maxOf(
            AudioRecord.getMinBufferSize(
                SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            ) * 2,
            CHUNK_BYTES * 4
        )

        val source = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N)
            MediaRecorder.AudioSource.UNPROCESSED
        else
            MediaRecorder.AudioSource.VOICE_RECOGNITION

        audioRecord = AudioRecord(
            source, SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            bufSize
        )

        if (audioRecord?.state != AudioRecord.STATE_INITIALIZED) {
            // Fallback to VOICE_RECOGNITION if UNPROCESSED not available
            audioRecord?.release()
            audioRecord = AudioRecord(
                MediaRecorder.AudioSource.VOICE_RECOGNITION, SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                bufSize
            )
        }

        audioRecord?.startRecording()
        Log.i(TAG, "Mic recording started (source=$source, rate=$SAMPLE_RATE)")

        val pcmBuf = ShortArray(CHUNK_SAMPLES)

        while (running) {
            // Connect/reconnect WebSocket
            if (wsClient == null || !connected) {
                connectWebSocket(url)
                // Wait for connection or timeout
                var waited = 0
                while (!connected && running && waited < 5000) {
                    Thread.sleep(100)
                    waited += 100
                }
                if (!connected) {
                    Thread.sleep(5000)  // Retry delay
                    continue
                }
            }

            // Read audio chunk
            val read = audioRecord?.read(pcmBuf, 0, CHUNK_SAMPLES) ?: -1
            if (read <= 0) {
                Thread.sleep(10)
                continue
            }

            // Compute RMS in dB
            var sumSq = 0.0
            for (i in 0 until read) {
                val s = pcmBuf[i].toDouble()
                sumSq += s * s
            }
            val rms = sqrt(sumSq / read)
            val db = if (rms > 0) 20.0 * log10(rms / 32767.0) else -100.0
            currentDb = db.toFloat()

            // VAD gate
            if (vadEnabled && db < vadThresholdDb) {
                onStatusChanged?.invoke(connected, currentDb, bytesSent)
                continue
            }

            // Apply gain
            val sendBuf = ByteBuffer.allocate(read * 2).order(ByteOrder.LITTLE_ENDIAN)
            for (i in 0 until read) {
                val amplified = (pcmBuf[i] * gain).toInt().coerceIn(-32768, 32767)
                sendBuf.putShort(amplified.toShort())
            }

            // Send over WebSocket
            try {
                wsClient?.send(sendBuf.array())
                bytesSent += sendBuf.capacity()
            } catch (e: Exception) {
                Log.w(TAG, "Send error: ${e.message}")
                connected = false
            }

            onStatusChanged?.invoke(connected, currentDb, bytesSent)
        }

        audioRecord?.stop()
        wsClient?.close()
        Log.i(TAG, "Capture loop ended")
    }

    private fun connectWebSocket(url: String) {
        try {
            wsClient?.close()
        } catch (_: Exception) {}

        wsClient = object : WebSocketClient(URI(url)) {
            override fun onOpen(handshakedata: ServerHandshake?) {
                Log.i(TAG, "WebSocket connected to $url")
                connected = true
                updateNotification("Live — streaming audio")
            }

            override fun onMessage(message: String?) {}
            override fun onMessage(bytes: ByteBuffer?) {}

            override fun onClose(code: Int, reason: String?, remote: Boolean) {
                Log.i(TAG, "WebSocket closed: $reason")
                connected = false
                updateNotification("Disconnected — reconnecting...")
            }

            override fun onError(ex: Exception?) {
                Log.w(TAG, "WebSocket error: ${ex?.message}")
                connected = false
            }
        }

        try {
            wsClient?.connect()
        } catch (e: Exception) {
            Log.w(TAG, "Connect failed: ${e.message}")
        }
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID, "Room Monitor",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Audio monitoring service"
            setShowBadge(false)
        }
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        val intent = Intent(this, MainActivity::class.java)
        val pending = PendingIntent.getActivity(
            this, 0, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Room Monitor")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentIntent(pending)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(text: String) {
        val nm = getSystemService(NotificationManager::class.java)
        nm.notify(NOTIFICATION_ID, buildNotification(text))
    }
}
