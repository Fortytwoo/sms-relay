package com.fortytwoo.smsoutbox;

import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.telephony.SubscriptionInfo;
import android.telephony.SubscriptionManager;

import java.util.ArrayList;
import java.util.List;

final class SmsScanner {
    private static final Uri INBOX_URI = Uri.parse("content://sms/inbox");
    private static final String[] PROJECTION = {
            "_id", "address", "body", "date", "sub_id"
    };

    private SmsScanner() {
    }

    static long latestSmsId(Context context) {
        try (Cursor cursor = context.getContentResolver().query(
                INBOX_URI,
                new String[]{"_id"},
                null,
                null,
                "_id DESC")) {
            return cursor != null && cursor.moveToFirst() ? cursor.getLong(0) : 0L;
        }
    }

    static List<MessageRecord> scanAfter(Context context, long afterId) {
        List<MessageRecord> result = new ArrayList<>();
        try (Cursor cursor = context.getContentResolver().query(
                INBOX_URI,
                PROJECTION,
                "_id > ?",
                new String[]{Long.toString(Math.max(0L, afterId))},
                "_id ASC")) {
            if (cursor == null) {
                return result;
            }
            while (cursor.moveToNext()) {
                int subscriptionId = cursor.isNull(4) ? -1 : cursor.getInt(4);
                result.add(new MessageRecord(
                        cursor.getLong(0),
                        cursor.getLong(3),
                        cursor.getString(1),
                        cursor.getString(2),
                        subscriptionId,
                        simInfo(context, subscriptionId)));
            }
        }
        return result;
    }

    @SuppressWarnings("deprecation")
    static String simInfo(Context context, int subscriptionId) {
        String slot = "SIM";
        String carrier = "";
        String phone = "";
        try {
            SubscriptionManager manager = (SubscriptionManager) context.getSystemService(
                    Context.TELEPHONY_SUBSCRIPTION_SERVICE);
            SubscriptionInfo info = manager == null
                    ? null
                    : manager.getActiveSubscriptionInfo(subscriptionId);
            if (info != null) {
                int index = info.getSimSlotIndex();
                if (index >= 0) {
                    slot = "SIM" + (index + 1);
                }
                CharSequence displayCarrier = info.getCarrierName();
                carrier = displayCarrier == null ? "" : clean(displayCarrier.toString());
                phone = clean(info.getNumber());
            }
        } catch (SecurityException ignored) {
            // Permissions are granted by the managed ADB deployment. Keep delivery functional
            // even when a carrier hides the phone number.
        }
        return slot + "_" + carrier + "_" + phone;
    }

    private static String clean(String value) {
        return value == null
                ? ""
                : value.replace('_', ' ').replace('\n', ' ').replace('\r', ' ').trim();
    }
}
