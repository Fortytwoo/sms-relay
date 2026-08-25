#!/system/bin/sh

PKG="com.fortytwoo.smsoutbox"
SERVICE="com.fortytwoo.smsoutbox/.ReliableDeliveryService"

until [ "$(getprop sys.boot_completed)" = "1" ]; do
  sleep 5
done

while true; do
  if pm path "$PKG" >/dev/null 2>&1; then
    for permission in \
      android.permission.READ_SMS \
      android.permission.RECEIVE_SMS \
      android.permission.READ_PHONE_STATE \
      android.permission.READ_PHONE_NUMBERS \
      android.permission.POST_NOTIFICATIONS; do
      pm grant --user 0 "$PKG" "$permission" >/dev/null 2>&1 || true
      pm set-permission-flags --user 0 "$PKG" "$permission" user-set user-fixed \
        >/dev/null 2>&1 || true
    done

    cmd appops set --uid "$PKG" READ_SMS allow >/dev/null 2>&1 || true
    cmd appops set --uid "$PKG" RECEIVE_SMS allow >/dev/null 2>&1 || true
    cmd appops set "$PKG" READ_SMS allow >/dev/null 2>&1 || true
    cmd appops set "$PKG" RECEIVE_SMS allow >/dev/null 2>&1 || true
    cmd appops set "$PKG" RUN_IN_BACKGROUND allow >/dev/null 2>&1 || true
    cmd appops set "$PKG" RUN_ANY_IN_BACKGROUND allow >/dev/null 2>&1 || true
    cmd deviceidle whitelist +"$PKG" >/dev/null 2>&1 || true

    if [ "$(getprop sys.user.0.ce_available)" = "true" ]; then
      am start-foreground-service -n "$SERVICE" -a com.fortytwoo.smsoutbox.PROCESS \
        >/dev/null 2>&1 || true
    fi
  fi
  sleep 300
done

