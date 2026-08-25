package com.fortytwoo.smsoutbox;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.UserManager;
import android.provider.Telephony;
import android.telephony.SmsMessage;
import android.telephony.SubscriptionManager;
import android.util.Log;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.List;

public final class SmsEventReceiver extends BroadcastReceiver {
    private static final String TAG = "SmsReliableOutbox";

    @Override
    public void onReceive(Context context, Intent intent) {
        UserManager userManager = (UserManager) context.getSystemService(Context.USER_SERVICE);
        if (userManager != null && !userManager.isUserUnlocked()) {
            return;
        }
        if (Telephony.Sms.Intents.SMS_RECEIVED_ACTION.equals(intent.getAction())) {
            enqueueBroadcast(context, intent);
        }
        start(context);
    }

    private static void enqueueBroadcast(Context context, Intent intent) {
        try {
            SmsMessage[] parts = Telephony.Sms.Intents.getMessagesFromIntent(intent);
            if (parts == null || parts.length == 0) {
                Log.w(TAG, "SMS broadcast contained no message parts");
                return;
            }
            String sender = parts[0].getOriginatingAddress();
            long receivedAt = parts[0].getTimestampMillis();
            StringBuilder content = new StringBuilder();
            for (SmsMessage part : parts) {
                if (part != null) {
                    String body = part.getMessageBody();
                    if (body != null) {
                        content.append(body);
                    }
                    if (part.getTimestampMillis() > 0) {
                        receivedAt = Math.min(receivedAt, part.getTimestampMillis());
                    }
                }
            }
            int subscriptionId = intent.getIntExtra(
                    SubscriptionManager.EXTRA_SUBSCRIPTION_INDEX,
                    intent.getIntExtra("subscription", intent.getIntExtra("sub_id", -1)));
            long durableId = stableBroadcastId(sender, content.toString(), receivedAt, subscriptionId);
            List<MessageRecord> records = new ArrayList<>();
            records.add(new MessageRecord(
                    durableId,
                    receivedAt,
                    sender,
                    content.toString(),
                    subscriptionId,
                    SmsScanner.simInfo(context, subscriptionId)));
            int inserted = new OutboxDatabase(context).enqueueWithoutAdvancingCursor(records);
            Log.i(TAG, "SMS broadcast persisted count=" + inserted + " durable_id=" + durableId);
        } catch (Throwable error) {
            Log.e(TAG, "unable to persist SMS broadcast: " + error.getClass().getSimpleName());
        }
    }

    private static long stableBroadcastId(
            String sender, String content, long receivedAt, int subscriptionId) throws Exception {
        String canonical = (sender == null ? "" : sender)
                + "\n" + (content == null ? "" : content)
                + "\n" + receivedAt
                + "\n" + subscriptionId;
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(
                canonical.getBytes(StandardCharsets.UTF_8));
        long value = 0L;
        for (int index = 0; index < 8; index++) {
            value = (value << 8) | (digest[index] & 0xffL);
        }
        value &= Long.MAX_VALUE;
        return value == 0L ? -Math.max(1L, receivedAt) : -value;
    }

    static void start(Context context) {
        Intent service = new Intent(context, ReliableDeliveryService.class);
        service.setAction(ReliableDeliveryService.ACTION_PROCESS);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.startForegroundService(service);
        } else {
            context.startService(service);
        }
    }
}
