package com.fortytwoo.smsoutbox;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.util.Log;

public final class ConfigReceiver extends BroadcastReceiver {
    private static final String TAG = "SmsReliableOutbox";
    private static final String ACTION_CONFIGURE = "com.fortytwoo.smsoutbox.CONFIGURE";
    private static final String ACTION_STATUS = "com.fortytwoo.smsoutbox.STATUS";

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent == null ? "" : intent.getAction();
        try {
            if (ACTION_CONFIGURE.equals(action)) {
                configure(context, intent);
                setResult(Activity.RESULT_OK, "configured", null);
            } else if (ACTION_STATUS.equals(action)) {
                reportStatus(context);
            } else {
                SmsEventReceiver.start(context);
                setResult(Activity.RESULT_OK, "kicked", null);
            }
        } catch (Exception error) {
            Log.e(TAG, "configuration command failed: " + error.getClass().getSimpleName());
            setResult(Activity.RESULT_CANCELED, "error:" + error.getMessage(), null);
        }
    }

    private static void configure(Context context, Intent intent) throws Exception {
        String endpoint = intent.getStringExtra("endpoint");
        String apiKey = intent.getStringExtra("api_key");
        if (endpoint == null || !"https".equalsIgnoreCase(Uri.parse(endpoint).getScheme())) {
            throw new IllegalArgumentException("endpoint_must_use_https");
        }
        if (apiKey == null || apiKey.length() != 64) {
            throw new IllegalArgumentException("api_key_must_have_64_characters");
        }
        SecureConfig.save(context, endpoint, apiKey);

        long bootstrapAfterId = intent.getLongExtra("bootstrap_after_id", Long.MIN_VALUE);
        if (bootstrapAfterId == Long.MIN_VALUE) {
            bootstrapAfterId = SmsScanner.latestSmsId(context);
        }
        new OutboxDatabase(context).resetCursor(Math.max(0L, bootstrapAfterId));
        Log.i(TAG, "configuration saved; bootstrap_after_id=" + bootstrapAfterId);
        SmsEventReceiver.start(context);
    }

    private void reportStatus(Context context) {
        OutboxDatabase.Stats stats = new OutboxDatabase(context).stats();
        boolean configured = SecureConfig.load(context) != null;
        long systemLatestSmsId = SmsScanner.latestSmsId(context);
        int visibleAfterCursor = SmsScanner.scanAfter(context, stats.lastScannedSmsId).size();
        String status = "configured=" + configured
                + " last_scanned_sms_id=" + stats.lastScannedSmsId
                + " system_latest_sms_id=" + systemLatestSmsId
                + " visible_after_cursor=" + visibleAfterCursor
                + " pending=" + stats.pending
                + " delivered=" + stats.delivered;
        Log.i(TAG, status);
        setResult(Activity.RESULT_OK, status, null);
    }
}
