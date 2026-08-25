package com.fortytwoo.smsoutbox;

final class MessageRecord {
    final long smsId;
    final long receivedAtMillis;
    final String sender;
    final String content;
    final int subscriptionId;
    final String simInfo;

    MessageRecord(
            long smsId,
            long receivedAtMillis,
            String sender,
            String content,
            int subscriptionId,
            String simInfo) {
        this.smsId = smsId;
        this.receivedAtMillis = receivedAtMillis;
        this.sender = sender == null ? "" : sender;
        this.content = content == null ? "" : content;
        this.subscriptionId = subscriptionId;
        this.simInfo = simInfo == null ? "" : simInfo;
    }
}

