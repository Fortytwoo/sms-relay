package com.fortytwoo.smsoutbox;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;

import java.util.List;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

public final class ReliableDeliveryService extends Service {
    static final String ACTION_PROCESS = "com.fortytwoo.smsoutbox.PROCESS";
    private static final String TAG = "SmsReliableOutbox";
    private static final String CHANNEL_ID = "sms_reliable_outbox";
    private static final int NOTIFICATION_ID = 9201;
    private static final long POLL_INTERVAL_SECONDS = 30L;
    private static final long BETWEEN_REQUESTS_MS = 5_000L;
    private static final long DELIVERED_RETENTION_MS = 7L * 24L * 3_600_000L;

    private final AtomicBoolean processing = new AtomicBoolean(false);
    private ScheduledExecutorService executor;
    private OutboxDatabase database;
    private ConnectivityManager connectivityManager;
    private ConnectivityManager.NetworkCallback networkCallback;

    @Override
    public void onCreate() {
        super.onCreate();
        database = new OutboxDatabase(this);
        connectivityManager = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        createChannel();
        startForeground(NOTIFICATION_ID, notification("Starting reliable delivery"));
        executor = Executors.newSingleThreadScheduledExecutor();
        executor.scheduleWithFixedDelay(
                this::processSafely,
                0,
                POLL_INTERVAL_SECONDS,
                TimeUnit.SECONDS);
        registerNetworkCallback();
        Log.i(TAG, "reliable delivery service started");
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        kick();
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        if (connectivityManager != null && networkCallback != null) {
            try {
                connectivityManager.unregisterNetworkCallback(networkCallback);
            } catch (RuntimeException ignored) {
                // Already unregistered.
            }
        }
        if (executor != null) {
            executor.shutdownNow();
        }
        if (database != null) {
            database.close();
        }
        super.onDestroy();
    }

    private void registerNetworkCallback() {
        if (connectivityManager == null) {
            return;
        }
        networkCallback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(Network network) {
                kick();
            }

            @Override
            public void onCapabilitiesChanged(Network network, NetworkCapabilities capabilities) {
                if (capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)) {
                    kick();
                }
            }
        };
        try {
            connectivityManager.registerDefaultNetworkCallback(networkCallback);
        } catch (RuntimeException error) {
            Log.w(TAG, "unable to register network callback: " + error.getClass().getSimpleName());
        }
    }

    private void kick() {
        ScheduledExecutorService current = executor;
        if (current != null && !current.isShutdown()) {
            current.execute(this::processSafely);
        }
    }

    private void processSafely() {
        if (!processing.compareAndSet(false, true)) {
            return;
        }
        Log.d(TAG, "delivery cycle started");
        PowerManager.WakeLock wakeLock = null;
        try {
            PowerManager powerManager = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (powerManager != null) {
                wakeLock = powerManager.newWakeLock(
                        PowerManager.PARTIAL_WAKE_LOCK, "smsoutbox:delivery");
                wakeLock.acquire(120_000L);
            }
            processOnce();
        } catch (Throwable error) {
            Log.e(TAG, "delivery cycle failed: " + safe(error));
        } finally {
            if (wakeLock != null && wakeLock.isHeld()) {
                wakeLock.release();
            }
            processing.set(false);
        }
    }

    private void processOnce() throws Exception {
        SecureConfig.Value config = SecureConfig.load(this);
        if (config == null) {
            updateNotification("Waiting for secure configuration");
            return;
        }

        long cursor = database.getLastScannedSmsId();
        List<MessageRecord> scanned = SmsScanner.scanAfter(this, cursor);
        Log.d(TAG, "scan completed after_id=" + cursor + " found=" + scanned.size());
        int inserted = database.enqueueAndAdvance(scanned);
        if (inserted > 0) {
            Log.i(TAG, "queued new messages count=" + inserted);
        }

        OutboxDatabase.Stats before = database.stats();
        updateNotification("Pending " + before.pending + ", cursor " + before.lastScannedSmsId);
        if (!networkValidated()) {
            if (before.pending > 0) {
                Log.i(TAG, "network unavailable; pending=" + before.pending);
            }
            return;
        }

        RelayClient client = new RelayClient();
        List<OutboxDatabase.PendingItem> due = database.due(System.currentTimeMillis(), 20);
        for (int index = 0; index < due.size(); index++) {
            OutboxDatabase.PendingItem item = due.get(index);
            RelayClient.Result result = client.deliver(config, item.message);
            long now = System.currentTimeMillis();
            if (result.success) {
                database.markDelivered(item.rowId, now);
                Log.i(TAG, "delivered sms_id=" + item.message.smsId
                        + " relay_id=" + result.relayId
                        + " duplicate=" + result.duplicate);
            } else {
                int attempts = item.attempts + 1;
                long delay = BackoffPolicy.delayMillis(attempts);
                database.markFailed(item.rowId, attempts, now + delay, result.error);
                Log.w(TAG, "delivery deferred sms_id=" + item.message.smsId
                        + " attempts=" + attempts
                        + " retry_in_ms=" + delay
                        + " error=" + result.error);
                break;
            }
            if (index + 1 < due.size()) {
                Thread.sleep(BETWEEN_REQUESTS_MS);
            }
        }

        database.pruneDelivered(System.currentTimeMillis() - DELIVERED_RETENTION_MS);
        OutboxDatabase.Stats after = database.stats();
        updateNotification("Pending " + after.pending + ", delivered " + after.delivered);
    }

    private boolean networkValidated() {
        if (connectivityManager == null) {
            return false;
        }
        Network active = connectivityManager.getActiveNetwork();
        NetworkCapabilities capabilities = connectivityManager.getNetworkCapabilities(active);
        return capabilities != null
                && capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                && capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED);
    }

    private void createChannel() {
        NotificationManager manager = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager != null) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID,
                    "SMS reliable delivery",
                    NotificationManager.IMPORTANCE_LOW);
            channel.setDescription("Durable SMS outbox and retry status");
            manager.createNotificationChannel(channel);
        }
    }

    private Notification notification(String text) {
        return new Notification.Builder(this, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentTitle("SMS reliable delivery")
                .setContentText(text)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .build();
    }

    private void updateNotification(String text) {
        NotificationManager manager = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager != null) {
            manager.notify(NOTIFICATION_ID, notification(text));
        }
    }

    private static String safe(Throwable error) {
        String message = error.getMessage();
        String result = error.getClass().getSimpleName() + ":" + (message == null ? "unknown" : message);
        result = result.replace('\n', ' ').replace('\r', ' ');
        return result.substring(0, Math.min(240, result.length()));
    }
}
