# SMS Relay 短信读取 API

## 服务地址

```text
https://api.midi.lizhijian.xyz/sms-relay
```

健康检查：

```http
GET /health
```

健康检查不需要鉴权，正常响应：

```json
{"ok":true,"status":"healthy"}
```

## 鉴权

短信读取接口使用独立的 64 字符只读 API Key。生产环境 Key 不写入本公开文档，请通过安全渠道取得并保存到环境变量 `SMS_RELAY_READ_API_KEY`。

推荐请求头：

```http
X-API-Key: <SMS_RELAY_READ_API_KEY>
```

也支持 Bearer 鉴权：

```http
Authorization: Bearer <SMS_RELAY_READ_API_KEY>
```

API Key 不支持放入 URL Query String，避免凭据进入浏览器历史和代理访问日志。写入 Key 与只读 Key 权限隔离，写入 Key 不能调用短信读取接口。

## 增量获取新短信

```http
GET /v1/messages?after_id=0&limit=50
```

```bash
export SMS_RELAY_READ_API_KEY='<64-character-read-key>'

curl 'https://api.midi.lizhijian.xyz/sms-relay/v1/messages?after_id=0&limit=50' \
  -H "X-API-Key: ${SMS_RELAY_READ_API_KEY}"
```

参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `after_id` | integer | 否 | 返回 `id` 大于该值的短信；首次增量读取使用 `0` |
| `limit` | integer | 否 | 每页数量，范围 `1-200`，默认 `50` |

增量响应按 `id` 升序排列：

```json
{
  "ok": true,
  "count": 2,
  "messages": [
    {
      "id": 124,
      "received_at": "2026-08-11T03:30:00+00:00",
      "message_type": "sms",
      "sender": "10690000",
      "content": "【小红书】您的验证码是 483921",
      "tag": "小红书",
      "source_received_at": "2026-08-11T11:29:58+08:00",
      "sim_info": "SIM1_13800000000",
      "device_name": "android-phone",
      "app_version": "3.3.1",
      "message_key": "...",
      "lark_push_status": "sent",
      "lark_push_attempts": 1,
      "lark_pushed_at": "2026-08-11T03:30:01+00:00",
      "verification_code": "483921",
      "sim_slot": "SIM1",
      "sim_phone": "13800000000"
    }
  ],
  "next_after_id": 125,
  "has_more": false
}
```

客户端游标规则：

1. 首次请求使用 `after_id=0`。
2. 整批短信处理成功后，持久化 `next_after_id`。
3. 下一次请求将已保存的值作为 `after_id`。
4. `has_more=true` 时立即继续读取下一页。
5. 没有新短信时返回空数组，`next_after_id` 保持为请求中的 `after_id`。

## 获取最近短信与历史翻页

不传 `after_id` 时，按 `id` 倒序返回最近短信：

```bash
curl 'https://api.midi.lizhijian.xyz/sms-relay/v1/messages?limit=20' \
  -H "X-API-Key: ${SMS_RELAY_READ_API_KEY}"
```

继续向更早记录翻页：

```bash
curl 'https://api.midi.lizhijian.xyz/sms-relay/v1/messages?before_id=100&limit=20' \
  -H "X-API-Key: ${SMS_RELAY_READ_API_KEY}"
```

`before_id` 与 `after_id` 不能同时使用。普通历史查询的响应只包含 `ok`、`count` 和 `messages`，不包含增量游标字段。

## 短信字段

| 字段 | 说明 |
| --- | --- |
| `id` | 单调递增的短信 ID，可作为增量游标 |
| `received_at` | 服务端接收时间 |
| `message_type` | 消息类型，短信通常为 `sms` |
| `sender` | 短信发送方号码或签名 |
| `content` | 短信正文 |
| `tag` | 短信正文中第一个非空 `【…】` 的内容；没有签名时为空字符串 |
| `source_received_at` | Android 设备上报的接收时间 |
| `sim_info` | Android 端原始 SIM 信息 |
| `device_name` | 上报设备名称 |
| `app_version` | 上报应用版本 |
| `message_key` | 用于去重的消息指纹 |
| `verification_code` | 自动识别的验证码，保留短信原始大小写；未识别时为空字符串 |
| `sim_slot` | 自动解析的卡槽，如 `SIM1`、`SIM2` |
| `sim_phone` | 自动解析的接收手机号 |
| `lark_push_status` | 飞书推送状态 |
| `lark_push_attempts` | 飞书推送尝试次数 |
| `lark_pushed_at` | 飞书推送成功时间 |

`POST /v1/messages` 的成功响应同样包含 `tag`、`sim_slot` 和 `sim_phone`，调用方
不需要再次读取列表即可获得本次短信的平台标签和接收手机号：

```json
{
  "ok": true,
  "id": 124,
  "duplicate": false,
  "message_key": "...",
  "tag": "小红书",
  "sim_slot": "SIM1",
  "sim_phone": "13800000000",
  "lark_push_status": "sent"
}
```

## 平台 URL 识别

客户端可把当前页面 URL 交给服务端，获得与短信 `tag` 相同口径的平台名称：

```http
GET /v1/platforms/identify?url=<URL 编码后的页面地址>
```

```bash
curl --get 'https://api.midi.lizhijian.xyz/sms-relay/v1/platforms/identify' \
  -H "X-API-Key: ${SMS_RELAY_READ_API_KEY}" \
  --data-urlencode 'url=https://ark.xiaohongshu.com/app-order/order/query'
```

识别成功：

```json
{"ok":true,"recognized":true,"tag":"小红书"}
```

当前支持的完整 hostname 与标准 `tag`：

| hostname | `tag` |
| --- | --- |
| `ark.xiaohongshu.com` | `小红书` |
| `s.kwaixiaodian.com` | `快手` |
| `zhaoshang.dxycare.com` | `丁香` |
| `portal.maiscrm.com` | `私域商城` |
| `store.weixin.qq.com` | `微信小店` |
| `fxg.jinritemai.com`、`doudian.douyinec.com` | `抖音商城` |

未知地址返回 `{"ok":true,"recognized":false,"tag":""}`。识别按 URL 解析后的
完整 hostname 精确匹配，相似域名不会命中。缺少 `url` 时返回 `400`。

## 状态码

| HTTP 状态码 | 响应示例 | 说明 |
| --- | --- | --- |
| `200` | `{"ok":true,...}` | 请求成功 |
| `400` | `{"ok":false,"error":"invalid_query"}` | 参数格式错误，或同时传入 `before_id` 和 `after_id` |
| `401` | `{"ok":false,"error":"unauthorized"}` | API Key 缺失、错误或权限不匹配 |
| `404` | `{"ok":false,"error":"not_found"}` | 接口路径不存在 |

## PowerShell 示例

```powershell
$headers = @{ 'X-API-Key' = $env:SMS_RELAY_READ_API_KEY }
$uri = 'https://api.midi.lizhijian.xyz/sms-relay/v1/messages?after_id=0&limit=50'
$result = Invoke-RestMethod -Method Get -Uri $uri -Headers $headers
$result.messages
```
