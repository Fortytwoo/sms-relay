# 多邮箱接码（腾讯企业邮箱）

多个邮箱与短信共用 SQLite、验证码/提取码识别、平台标签、飞书通知和网页收件箱。短信原有写入、60 秒去重和鉴权保持兼容。

## 配置

腾讯企业邮箱使用 `imap.exmail.qq.com:993`（SSL/TLS），用户名为完整邮箱地址。`password` 填邮箱密码；开启安全登录时需使用**客户端专用密码**。账号需允许 IMAP 登录。参见[腾讯云客户端说明](https://main.qcloudimg.com/raw/document/product/pdf/613_46019_cn.pdf)。

可在网页登录后进入“邮箱配置”页面添加多个邮箱；配置会原子写入 `data/mailboxes.json`，保存后立即更新收信线程。也可复制 `mailboxes.example.json` 到该路径手工编辑；`data/` 已被 Git 忽略。不要修改示例文件来保存真实凭据。

```json
[
  {
    "id": "work-a",
    "address": "account-a@example.com",
    "host": "imap.exmail.qq.com",
    "password": "replace-with-mailbox-or-client-password"
  },
  {
    "id": "work-b",
    "address": "account-b@example.com",
    "host": "imap.exmail.qq.com",
    "password": "replace-with-mailbox-or-client-password"
  }
]
```

| 字段 | 说明 |
| --- | --- |
| `id` | 稳定唯一标识，1–64 位英文字母、数字、`_`、`-`；不要随意修改 |
| `address` | 接收邮箱地址，不能重复；按该地址区分账号，不依赖邮件 `To`（支持别名/BCC） |
| `host` | IMAP 主机，腾讯企业邮箱填 `imap.exmail.qq.com` |
| `password` | 邮箱密码或客户端专用密码，只在服务端使用 |
| `username` | 可选，默认等于 `address` |
| `port` | 可选，默认 `993`；始终使用 TLS 并验证证书和 hostname |
| `folder` | 可选，默认 `INBOX`；非 ASCII 文件夹使用 IMAP modified UTF-7 名称 |
| `start_from` | 默认 `latest`：首次成功连接建立游标，只收后续新邮件；`all`：首次读取历史邮件，**也会触发历史验证码通知** |
| `smtp_host` | 可选，SMTP 主机；填写后可测试发信 |
| `smtp_port` | 默认 `465` |
| `smtp_security` | `ssl`（默认）或 `starttls`；始终验证 TLS 证书与主机名 |
| `smtp_username` | 可选，默认使用 IMAP 用户名 |
| `smtp_password` | 可选，默认使用 IMAP 密码；编辑时留空保留原值 |

Docker 的 `.env` 设置容器内路径，现有 `./data:/data` 挂载可直接读取：

```dotenv
SMS_RELAY_MAILBOXES_FILE=/data/mailboxes.json
```

未设置此变量时默认使用与 SQLite 同目录的 `mailboxes.json`（容器内为 `/data/mailboxes.json`）。目录需允许容器用户 `10001:10001` 创建/替换文件；文件限制其他用户读取，Linux 权限建议 `600`。网页保存会以 `600` 权限原子替换文件，无需重启；手工编辑文件后仍需重启。游标保存在 SQLite。生产变更仍按仓库的备份和部署规则执行。

本地运行使用本机路径（另需 README 中原有 Key/OAuth 配置）：

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:SMS_RELAY_MAILBOXES_FILE = 'G:\project\code\sms-relay\data\mailboxes.json'
uv run python app.py
```

## 接收与恢复

- 各账号独立保存 `UIDVALIDITY` 和 UID 游标，轮流同步，账号之间间隔 5 秒，每轮后等待 30 秒，每账号每轮最多 50 封。单个账号失败会继续其他账号。这是轮询，接收延迟受邮箱数量、积压和网络超时影响。
- 登录被拒绝后至少等待 5 分钟，其他同步故障至少等待 30 秒再重试，连续失败逐步延长到最多 1 小时；退避时间存入数据库，重启不会清空。恢复成功后自动恢复正常轮询。
- 只读选择文件夹并使用 `BODY.PEEK[]`，不标记已读、不删除或移动邮件；已读邮件也能接收。
- 原始邮件上限 2 MiB，超出时跳过并累计 `skipped_count`；正文保存前 32 Ki 字符，主题最多 2048 字符。附件不保存，MIME 正文优先纯文本，否则将 HTML 转文本，不执行 HTML 或加载远端图片。
- 主题与正文共同参与验证码、提取码和 `【…】` 标签识别，保留大小写。无验证码的邮件也入库，但不通知飞书。截断后的正文不参与识别。
- 邮件按 `recipient + source_message_id` 去重；内置标识包含账号/连接身份、`UIDVALIDITY` 和 UID。不同邮箱、不同邮件独立保存，不依赖可能缺失或重复的 `Message-ID` 邮件头。
- 入库成功后才推进游标，提交后崩溃重试仍只保留一条消息；只有首次入库触发通知，通知失败复用后台重试。
- `UIDVALIDITY` 改变时暂停该账号并显示 `uidvalidity_changed`，避免静默重放历史验证码。核实远端文件夹重建后，可更换该账号 `id` 并保留 `start_from=latest`，重启建立新游标；如需补取重建期间邮件，可明确选择 `all`，注意历史通知和新 UID 可能造成重复。

## 网页与 API

### 邮箱配置与连通性测试

网页登录后进入“邮箱配置”，可新增、编辑、删除最多 100 个邮箱。配置接口仅接受中央 OAuth 会话，读写 API Key 均不能管理邮箱；跨来源写入会被拒绝。列表和编辑接口不返回密码，只显示是否已设置；编辑时密码留空表示沿用原值。删除配置不会删除已入库邮件或历史游标。

每个邮箱可分别点击“测试收信”和“测试发信”：收信测试以只读模式登录 IMAP，读取收件箱最新邮件的主题头（空收件箱则只验证打开文件夹），不改变同步游标或已读状态；发信测试通过 SMTP 向该邮箱自身发送一封固定测试邮件。测试失败仅返回归类错误，不回显服务商响应或凭据。发信测试成功表示 SMTP 接受了邮件，不保证邮件最终送达；可随后查看收件箱或运行收信测试。真实邮件服务连通性需在配置凭据后验收。

### 邮件验证码与飞书卡片

除了原有关键词，邮件还支持“输入/使用以下代码完成验证”等明确验证动作，读取随后同一行或第一个非空行的 4–8 位字母数字代码。保留大小写；有多个不同候选码时不自动选取。普通登录成功提醒、日期和客服电话不作为验证码。

短信和邮件验证码统一发送到原配置飞书群，使用 JSON 2.0 卡片，显示验证码、接收邮箱/手机号、来源、平台（有标签时）、邮件主题和接收时间。字段使用普通文本，邮件正文不放进卡片。飞书客户端需支持卡片 2.0（V7.20+）。

卡片“复制验证码”按钮打开本站 `/copy?message_id=<id>`：通过现有中央 OAuth 会话读取验证码后尝试自动复制；未登录时先登录，浏览器拒绝自动复制时可点页内按钮。URL 只包含记录 ID，不含验证码、邮箱或 API Key。飞书按钮的原生行为只有打开链接/回调，不能承诺所有客户端都无需登录或二次点击，参见[官方按钮文档](https://open.feishu.cn/document/feishu-cards/card-json-v2-components/interactive-components/button)。

`GET /v1/messages/<id>/code` 使用现有只读 Key 或中央 OAuth 会话，只返回该条消息的验证码和必要元数据。写入 Key 不可读取，网页和 API 响应不缓存。新规则不会自动把历史 `skipped` 消息重新推送；历史补发必须限定已授权的消息 ID，避免重复通知。

网页支持全部消息/短信/邮件及接收邮箱筛选；邮件详情显示主题和邮箱，验证码可点击复制。搜索匹配当前已加载记录中的邮箱、主题、发送方、标签和正文。

`GET /v1/messages` 默认返回短信和邮件；仅需短信的客户端加 `message_type=sms`。筛选可组合 `before_id`、`after_id`、`limit`：

```http
GET /v1/messages?message_type=email&recipient=account-a%40example.com&after_id=0&limit=50
X-API-Key: <SMS_RELAY_READ_API_KEY>
```

每组筛选条件独立保存增量游标；切换邮箱后从 `after_id=0` 开始。

`GET /v1/mailboxes` 使用只读 Key 或中央 OAuth 会话，只返回 `id`、`address`、`last_success_at`、`last_error`、`skipped_count`、`next_retry_at`（Unix 秒，0 表示无需退避）。未完成首次同步时成功时间为空；`mailbox_auth_failed` 表示 IMAP 登录被拒绝，检查服务开启状态和客户端密码；`mailbox_sync_failed` 表示连接、协议或本地入库异常，检查网络和数据库可写性。不会返回密码、登录用户名或服务商原始错误。`/health` 只证明 HTTP 存活，不代表同步成功。

也可使用写入 Key 向 `POST /v1/messages` 投递邮件：

```json
{
  "type": "email",
  "from": "no-reply@example.com",
  "recipient": "account-a@example.com",
  "subject": "【示例平台】验证码",
  "content": "Your OTP is a7C91d",
  "received_at": "2026-09-22T10:00:00+08:00",
  "source_message_id": "stable-provider-delivery-id"
}
```

`recipient` 和 `source_message_id` 必填，重试沿用标识、新邮件使用新标识。手工投递和内置 IMAP 标识不同，不会自动跨来源合并。

## 验证边界

`uv run python -m unittest discover -s tests -v` 使用 fake IMAP/OAuth、通知替身与临时 SQLite，覆盖多账号隔离、故障恢复、重启游标、事务去重、MIME、大小写、迁移和鉴权。真实账号连接和飞书送达需配置凭据后单独验收，本地通过不等于线上接码成功。
