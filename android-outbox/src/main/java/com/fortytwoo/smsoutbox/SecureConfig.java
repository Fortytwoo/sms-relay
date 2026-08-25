package com.fortytwoo.smsoutbox;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.nio.charset.StandardCharsets;
import java.security.KeyStore;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

final class SecureConfig {
    private static final String PREFS = "reliable_delivery_config";
    private static final String ENDPOINT = "endpoint";
    private static final String ENCRYPTED_API_KEY = "api_key_ciphertext";
    private static final String KEY_ALIAS = "sms_reliable_outbox_api_key";

    static final class Value {
        final String endpoint;
        final String apiKey;

        Value(String endpoint, String apiKey) {
            this.endpoint = endpoint;
            this.apiKey = apiKey;
        }
    }

    private SecureConfig() {
    }

    static void save(Context context, String endpoint, String apiKey) throws Exception {
        byte[] encrypted = encrypt(apiKey.getBytes(StandardCharsets.UTF_8));
        boolean saved = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit()
                .putString(ENDPOINT, endpoint)
                .putString(ENCRYPTED_API_KEY, Base64.encodeToString(encrypted, Base64.NO_WRAP))
                .commit();
        if (!saved) {
            throw new IllegalStateException("configuration_commit_failed");
        }
    }

    static Value load(Context context) {
        try {
            SharedPreferences preferences = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
            String endpoint = preferences.getString(ENDPOINT, "");
            String encoded = preferences.getString(ENCRYPTED_API_KEY, "");
            if (endpoint == null || endpoint.isEmpty() || encoded == null || encoded.isEmpty()) {
                return null;
            }
            byte[] apiKey = decrypt(Base64.decode(encoded, Base64.NO_WRAP));
            return new Value(endpoint, new String(apiKey, StandardCharsets.UTF_8));
        } catch (Exception ignored) {
            return null;
        }
    }

    private static SecretKey key() throws Exception {
        KeyStore keyStore = KeyStore.getInstance("AndroidKeyStore");
        keyStore.load(null);
        if (!keyStore.containsAlias(KEY_ALIAS)) {
            KeyGenerator generator = KeyGenerator.getInstance(
                    KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
            generator.init(new KeyGenParameterSpec.Builder(
                    KEY_ALIAS,
                    KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setRandomizedEncryptionRequired(true)
                    .build());
            generator.generateKey();
        }
        return ((KeyStore.SecretKeyEntry) keyStore.getEntry(KEY_ALIAS, null)).getSecretKey();
    }

    private static byte[] encrypt(byte[] plaintext) throws Exception {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, key());
        byte[] iv = cipher.getIV();
        byte[] ciphertext = cipher.doFinal(plaintext);
        byte[] result = new byte[1 + iv.length + ciphertext.length];
        result[0] = (byte) iv.length;
        System.arraycopy(iv, 0, result, 1, iv.length);
        System.arraycopy(ciphertext, 0, result, 1 + iv.length, ciphertext.length);
        return result;
    }

    private static byte[] decrypt(byte[] encrypted) throws Exception {
        int ivLength = encrypted.length == 0 ? 0 : encrypted[0] & 0xff;
        if (ivLength < 12 || encrypted.length <= 1 + ivLength) {
            throw new IllegalArgumentException("invalid_encrypted_configuration");
        }
        byte[] iv = new byte[ivLength];
        byte[] ciphertext = new byte[encrypted.length - 1 - ivLength];
        System.arraycopy(encrypted, 1, iv, 0, ivLength);
        System.arraycopy(encrypted, 1 + ivLength, ciphertext, 0, ciphertext.length);
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, iv));
        return cipher.doFinal(ciphertext);
    }
}

