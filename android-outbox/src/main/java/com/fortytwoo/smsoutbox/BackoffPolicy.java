package com.fortytwoo.smsoutbox;

public final class BackoffPolicy {
    private static final long[] DELAYS_MS = {
            30_000L,
            120_000L,
            600_000L,
            1_800_000L,
            3_600_000L,
            6 * 3_600_000L
    };

    private BackoffPolicy() {
    }

    public static long delayMillis(int attemptsAfterFailure) {
        int index = Math.max(1, attemptsAfterFailure) - 1;
        return DELAYS_MS[Math.min(index, DELAYS_MS.length - 1)];
    }
}

