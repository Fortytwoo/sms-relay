# SMS Reliable Outbox

Android 设备侧持久化短信投递组件，作为 `cn.ppps.forwarder` 的可靠性补偿层：

- 持久化扫描系统短信收件箱，应用或设备重启后继续处理。
- `SMS_RECEIVED` 广播到达时先同步落入 SQLite Outbox，再启动网络投递，避免依赖系统短信库轮询。
- 只有服务端返回 HTTP 2xx 且 JSON `ok=true` 才确认投递成功。
- 网络断开或 DNS/HTTP 异常时使用 `30s / 2m / 10m / 30m / 1h / 6h` 指数退避。
- 网络重新验证后立即唤醒队列，并以 5 秒间隔串行投递。
- 服务端继续使用既有 `message_key` 防止原转发 App 与本组件产生重复消息。
- API Key 使用 Android Keystore AES-GCM 加密后存入应用私有目录，不写入源码、APK、通知或日志。

## 构建

```powershell
.\build.ps1
```

产物：

- `build/dist/sms-reliable-outbox-v1.1.0.apk`
- `build/dist/sms-reliable-outbox-systemizer-v1.1.0.zip`

Magisk systemizer 会把 APK 作为私有系统应用挂载，并每 5 分钟校验短信权限、后台 AppOps、Doze 白名单和前台服务。设备实际部署时仍配合 `sms-forwarder-keepalive` 的 `system_server` Hook。

## 配置

配置广播只允许持有系统 `android.permission.DUMP` 的调用方访问，普通第三方应用不能读取或修改配置。

```powershell
$env:SMS_RELAY_API_KEY = '<64-character-write-key>'
adb shell am broadcast `
  -n com.fortytwoo.smsoutbox/.ConfigReceiver `
  -a com.fortytwoo.smsoutbox.CONFIGURE `
  --es endpoint 'https://api.example.com/sms-relay/v1/messages' `
  --es api_key $env:SMS_RELAY_API_KEY
```

不传 `bootstrap_after_id` 时以当前最新短信为基线，不回放历史短信。需要恢复指定游标后的漏转记录时添加：

```powershell
--el bootstrap_after_id 515
```

## 状态

```powershell
adb shell am broadcast `
  -n com.fortytwoo.smsoutbox/.ConfigReceiver `
  -a com.fortytwoo.smsoutbox.STATUS
```

状态仅返回游标、待处理数和已投递数，不输出短信内容或 API Key。
