package com.fortytwoo.smsoutbox;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;

import java.util.ArrayList;
import java.util.List;

final class OutboxDatabase extends SQLiteOpenHelper {
    private static final String DATABASE_NAME = "reliable-outbox.db";
    private static final int DATABASE_VERSION = 1;
    private static final String CURSOR_KEY = "last_scanned_sms_id";

    static final class PendingItem {
        final long rowId;
        final MessageRecord message;
        final int attempts;

        PendingItem(long rowId, MessageRecord message, int attempts) {
            this.rowId = rowId;
            this.message = message;
            this.attempts = attempts;
        }
    }

    static final class Stats {
        final long lastScannedSmsId;
        final int pending;
        final int delivered;

        Stats(long lastScannedSmsId, int pending, int delivered) {
            this.lastScannedSmsId = lastScannedSmsId;
            this.pending = pending;
            this.delivered = delivered;
        }
    }

    OutboxDatabase(Context context) {
        super(context, DATABASE_NAME, null, DATABASE_VERSION);
        setWriteAheadLoggingEnabled(true);
    }

    @Override
    public void onConfigure(SQLiteDatabase db) {
        super.onConfigure(db);
        db.setForeignKeyConstraintsEnabled(true);
    }

    @Override
    public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)");
        db.execSQL(
                "CREATE TABLE outbox ("
                        + "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                        + "sms_id INTEGER NOT NULL UNIQUE,"
                        + "received_at_ms INTEGER NOT NULL,"
                        + "sender TEXT NOT NULL,"
                        + "content TEXT NOT NULL,"
                        + "subscription_id INTEGER NOT NULL,"
                        + "sim_info TEXT NOT NULL,"
                        + "state TEXT NOT NULL DEFAULT 'pending',"
                        + "attempts INTEGER NOT NULL DEFAULT 0,"
                        + "next_attempt_at_ms INTEGER NOT NULL DEFAULT 0,"
                        + "last_error TEXT NOT NULL DEFAULT '',"
                        + "created_at_ms INTEGER NOT NULL,"
                        + "delivered_at_ms INTEGER NOT NULL DEFAULT 0"
                        + ")");
        db.execSQL(
                "CREATE INDEX idx_outbox_due "
                        + "ON outbox(state, next_attempt_at_ms, sms_id)");
    }

    @Override
    public void onUpgrade(SQLiteDatabase db, int oldVersion, int newVersion) {
        throw new IllegalStateException("Unsupported database upgrade " + oldVersion + " -> " + newVersion);
    }

    synchronized long getLastScannedSmsId() {
        SQLiteDatabase db = getReadableDatabase();
        try (Cursor cursor = db.query(
                "metadata", new String[]{"value"}, "key = ?",
                new String[]{CURSOR_KEY}, null, null, null)) {
            if (!cursor.moveToFirst()) {
                return 0L;
            }
            try {
                return Long.parseLong(cursor.getString(0));
            } catch (NumberFormatException ignored) {
                return 0L;
            }
        }
    }

    synchronized void resetCursor(long smsId) {
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            db.delete("outbox", "state = 'pending'", null);
            putMetadata(db, CURSOR_KEY, Long.toString(Math.max(0L, smsId)));
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    synchronized int enqueueAndAdvance(List<MessageRecord> messages) {
        return enqueue(messages, true);
    }

    synchronized int enqueueWithoutAdvancingCursor(List<MessageRecord> messages) {
        return enqueue(messages, false);
    }

    private int enqueue(List<MessageRecord> messages, boolean advanceCursor) {
        if (messages.isEmpty()) {
            return 0;
        }
        SQLiteDatabase db = getWritableDatabase();
        int inserted = 0;
        long maxId = getLastScannedSmsId();
        db.beginTransaction();
        try {
            long now = System.currentTimeMillis();
            for (MessageRecord message : messages) {
                ContentValues values = new ContentValues();
                values.put("sms_id", message.smsId);
                values.put("received_at_ms", message.receivedAtMillis);
                values.put("sender", message.sender);
                values.put("content", message.content);
                values.put("subscription_id", message.subscriptionId);
                values.put("sim_info", message.simInfo);
                values.put("state", "pending");
                values.put("attempts", 0);
                values.put("next_attempt_at_ms", 0);
                values.put("last_error", "");
                values.put("created_at_ms", now);
                long row = db.insertWithOnConflict(
                        "outbox", null, values, SQLiteDatabase.CONFLICT_IGNORE);
                if (row != -1L) {
                    inserted++;
                }
                maxId = Math.max(maxId, message.smsId);
            }
            if (advanceCursor) {
                putMetadata(db, CURSOR_KEY, Long.toString(maxId));
            }
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
        return inserted;
    }

    synchronized List<PendingItem> due(long nowMillis, int limit) {
        List<PendingItem> result = new ArrayList<>();
        SQLiteDatabase db = getReadableDatabase();
        try (Cursor cursor = db.query(
                "outbox",
                new String[]{
                        "id", "sms_id", "received_at_ms", "sender", "content",
                        "subscription_id", "sim_info", "attempts"
                },
                "state = 'pending' AND next_attempt_at_ms <= ?",
                new String[]{Long.toString(nowMillis)},
                null,
                null,
                "sms_id ASC",
                Integer.toString(Math.max(1, limit)))) {
            while (cursor.moveToNext()) {
                MessageRecord message = new MessageRecord(
                        cursor.getLong(1),
                        cursor.getLong(2),
                        cursor.getString(3),
                        cursor.getString(4),
                        cursor.getInt(5),
                        cursor.getString(6));
                result.add(new PendingItem(cursor.getLong(0), message, cursor.getInt(7)));
            }
        }
        return result;
    }

    synchronized void markDelivered(long rowId, long nowMillis) {
        ContentValues values = new ContentValues();
        values.put("state", "delivered");
        values.put("delivered_at_ms", nowMillis);
        values.put("last_error", "");
        getWritableDatabase().update("outbox", values, "id = ?", new String[]{Long.toString(rowId)});
    }

    synchronized void markFailed(long rowId, int attempts, long nextAttemptAt, String error) {
        ContentValues values = new ContentValues();
        values.put("attempts", attempts);
        values.put("next_attempt_at_ms", nextAttemptAt);
        values.put("last_error", safeError(error));
        getWritableDatabase().update("outbox", values, "id = ?", new String[]{Long.toString(rowId)});
    }

    synchronized void pruneDelivered(long olderThanMillis) {
        getWritableDatabase().delete(
                "outbox",
                "state = 'delivered' AND delivered_at_ms > 0 AND delivered_at_ms < ?",
                new String[]{Long.toString(olderThanMillis)});
    }

    synchronized Stats stats() {
        SQLiteDatabase db = getReadableDatabase();
        int pending = scalarCount(db, "state = 'pending'");
        int delivered = scalarCount(db, "state = 'delivered'");
        return new Stats(getLastScannedSmsId(), pending, delivered);
    }

    private static int scalarCount(SQLiteDatabase db, String selection) {
        try (Cursor cursor = db.query(
                "outbox", new String[]{"COUNT(*)"}, selection,
                null, null, null, null)) {
            return cursor.moveToFirst() ? cursor.getInt(0) : 0;
        }
    }

    private static void putMetadata(SQLiteDatabase db, String key, String value) {
        ContentValues values = new ContentValues();
        values.put("key", key);
        values.put("value", value);
        db.insertWithOnConflict("metadata", null, values, SQLiteDatabase.CONFLICT_REPLACE);
    }

    private static String safeError(String error) {
        if (error == null) {
            return "unknown";
        }
        String normalized = error.replace('\n', ' ').replace('\r', ' ');
        return normalized.substring(0, Math.min(512, normalized.length()));
    }
}
