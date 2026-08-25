package com.fortytwoo.smsoutbox;

public final class BackoffPolicyTest {
    private BackoffPolicyTest() {
    }

    public static void main(String[] args) {
        expect(BackoffPolicy.delayMillis(0) == 30_000L, "zero attempts uses first delay");
        expect(BackoffPolicy.delayMillis(1) == 30_000L, "first retry");
        expect(BackoffPolicy.delayMillis(2) == 120_000L, "second retry");
        expect(BackoffPolicy.delayMillis(3) == 600_000L, "third retry");
        expect(BackoffPolicy.delayMillis(100) == 21_600_000L, "delay is capped");
        System.out.println("BackoffPolicyTest: PASS");
    }

    private static void expect(boolean value, String name) {
        if (!value) {
            throw new AssertionError(name);
        }
    }
}

