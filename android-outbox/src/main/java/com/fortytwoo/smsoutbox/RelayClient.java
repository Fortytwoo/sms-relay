package com.fortytwoo.smsoutbox;

import android.os.Build;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

import javax.net.ssl.HttpsURLConnection;

final class RelayClient {
    static final class Result {
        final boolean success;
        final boolean duplicate;
        final long relayId;
        final String error;

        Result(boolean success, boolean duplicate, long relayId, String error) {
            this.success = success;
            this.duplicate = duplicate;
            this.relayId = relayId;
            this.error = error;
        }
    }

    Result deliver(SecureConfig.Value config, MessageRecord message) {
        HttpsURLConnection connection = null;
        try {
            URL url = new URL(config.endpoint);
            if (!"https".equalsIgnoreCase(url.getProtocol())) {
                return new Result(false, false, 0, "endpoint_must_use_https");
            }
            JSONObject payload = new JSONObject();
            payload.put("type", "sms");
            payload.put("from", message.sender);
            payload.put("content", message.content);
            payload.put("received_at", formatReceivedAt(message.receivedAtMillis));
            payload.put("sim_info", message.simInfo);
            payload.put("device_name", Build.MANUFACTURER + " " + Build.MODEL);
            payload.put("app_version", "reliable-outbox/1.1.0");
            byte[] body = payload.toString().getBytes(StandardCharsets.UTF_8);

            connection = (HttpsURLConnection) url.openConnection();
            connection.setConnectTimeout(15_000);
            connection.setReadTimeout(20_000);
            connection.setRequestMethod("POST");
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json");
            connection.setRequestProperty("X-API-Key", config.apiKey);
            connection.setFixedLengthStreamingMode(body.length);
            connection.getOutputStream().write(body);

            int status = connection.getResponseCode();
            InputStream stream = status >= 200 && status < 300
                    ? connection.getInputStream()
                    : connection.getErrorStream();
            String response = readLimited(stream, 65_536);
            if (status < 200 || status >= 300) {
                return new Result(false, false, 0, "http_" + status);
            }
            JSONObject json = new JSONObject(response);
            if (!json.optBoolean("ok", false)) {
                return new Result(false, false, 0, "server_not_ok");
            }
            return new Result(
                    true,
                    json.optBoolean("duplicate", false),
                    json.optLong("id", 0L),
                    "");
        } catch (Exception error) {
            return new Result(false, false, 0, error.getClass().getSimpleName() + ":" + safe(error.getMessage()));
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    private static String formatReceivedAt(long millis) {
        return new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(new Date(millis));
    }

    private static String readLimited(InputStream input, int limit) throws Exception {
        if (input == null) {
            return "";
        }
        try (InputStream stream = input; ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[4096];
            int remaining = limit;
            while (remaining > 0) {
                int count = stream.read(buffer, 0, Math.min(buffer.length, remaining));
                if (count < 0) {
                    break;
                }
                output.write(buffer, 0, count);
                remaining -= count;
            }
            return output.toString("UTF-8");
        }
    }

    private static String safe(String value) {
        if (value == null) {
            return "unknown";
        }
        String normalized = value.replace('\n', ' ').replace('\r', ' ');
        return normalized.substring(0, Math.min(240, normalized.length()));
    }
}
