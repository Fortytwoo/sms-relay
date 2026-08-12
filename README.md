# SMS Relay

一个轻量、自托管的短信中转服务。Android 端可通过
[SmsForwarder](https://github.com/pppscn/SmsForwarder) 把短信发送到服务端；服务端保存短信、自动识别常见验证码，并提供飞书登录的网页收件箱。识别到验证码后，还可以把验证码推送到指定飞书群。

项目只使用 Python 标准库和原生浏览器 API，不依赖 Web 框架或前端构建工具。

## 功能

- 独立的 64 位写入与只读 API Key，支持 `X-API-Key` 和 Bearer Header。
- SQLite 持久化，并按规范化消息内容的 SHA-256 指纹去重。
- 自动识别 4–8 位数字或字母数字验证码。
- 自动把短信中第一个非空 `【…】` 签名提取为 `tag`，历史短信无需迁移即可返回标签。
- 根据受支持后台的 URL 精确识别小红书、快手、丁香、私域商城、微信小店和抖音商城。
- 飞书 OAuth 登录、管理员与 SQLite 动态访问控制。
- 从飞书只读同步企业组织架构，支持部门（含全部子部门）和个人授权。
- 验证码点击复制，显示接收短信的 SIM 卡槽和手机号。
- 可选的飞书群验证码通知，失败后后台重试。
- 响应式中文网页，支持搜索、分页和自动刷新。
- Docker 非 root、只读根文件系统、最小权限运行。

## 架构

```text
Android / SmsForwarder
        │ HTTPS + 64 位 API Key
        ▼
Nginx / Caddy / Traefik
        │ 127.0.0.1:8000
        ▼
SMS Relay ──────► SQLite
    │
    ├───────────► Feishu OAuth（网页登录）
    └───────────► Feishu Bot（可选群通知）
```

短信和验证码属于高敏感数据。请不要直接把应用端口暴露到公网，也不要提交 `.env`、SQLite 数据库、真实短信截图或生产反向代理配置。

## 快速开始

### 1. 准备配置

```bash
git clone https://github.com/Fortytwoo/sms-relay.git
cd sms-relay
cp .env.example .env
```

Windows PowerShell 使用：

```powershell
Copy-Item .env.example .env
```

分别生成三个独立的 64 字符随机值，填入 `.env`：

```bash
uv run python -c "import secrets; print(secrets.token_hex(32))"
uv run python -c "import secrets; print(secrets.token_hex(32))"
uv run python -c "import secrets; print(secrets.token_hex(32))"
```

必须修改的配置：

| 变量 | 用途 |
| --- | --- |
| `SMS_RELAY_API_KEY` | Android 端写入 API 使用的 64 字符密钥 |
| `SMS_RELAY_READ_API_KEY` | 外部程序读取短信使用的独立 64 字符密钥 |
| `SMS_RELAY_SESSION_SECRET` | 签名浏览器会话，必须与两个 API Key 不同 |
| `FEISHU_APP_ID` | 飞书自建应用 App ID |
| `FEISHU_APP_SECRET` | 飞书自建应用 App Secret |
| `FEISHU_REDIRECT_URI` | OAuth 回调完整 URL，例如 `https://relay.example.com/sms-relay/auth/callback` |
| `FEISHU_ADMIN_OPEN_IDS` | 不可在页面撤销的管理员 Open ID，多个值使用英文逗号分隔 |
| `FEISHU_ADMIN_UNION_IDS` | 可选；管理员 Union ID，适合跨应用身份审计 |
| `FEISHU_ALLOWED_OPEN_IDS` | 旧版兼容变量；新部署请留空并使用管理员变量 |
| `FEISHU_CHAT_ID` | 可选；接收验证码通知的群 Chat ID |

### 2. 配置飞书应用

在飞书开放平台创建企业自建应用：

1. 启用网页 OAuth 登录能力，并把 `FEISHU_REDIRECT_URI` 加入安全重定向 URL。
2. 开通读取当前登录用户基本信息、通讯录基本信息和部门组织架构所需权限，并把应用通讯录权限范围设为“全部成员”。
3. 如需群通知，启用机器人和发送群消息权限，把机器人加入目标群，再填写群 Chat ID。
4. 把初始管理员写入 `FEISHU_ADMIN_OPEN_IDS`。管理员首次进入“权限管理”时会自动启动组织架构同步。

普通用户不再写入环境变量。管理员可在页面勾选部门或人员；部门授权会递归包含全部子部门，并在下一次目录同步完成后自动跟随入职、调岗和离职变化。同步采用新快照事务替换，失败时保留最近一次成功目录。

飞书 App Secret、用户 Open ID 和群 Chat ID 均不要提交到仓库。

### 3. 启动服务

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8000/health
```

较旧的 Docker 环境可把 `docker compose` 替换为 `docker-compose`。默认只监听宿主机 `127.0.0.1:8000`，SQLite 数据保存在 `./data/sms-relay.db`。

预期健康响应：

```json
{"ok":true,"status":"healthy"}
```

### 4. 配置 HTTPS 反向代理

把 [nginx-location.conf](nginx-location.conf) 放进你的 HTTPS `server` 块。示例假设公开路径是 `/sms-relay/`，并代理到本机的 `127.0.0.1:8000`。

如果修改公开路径，请同时修改：

- Nginx location 前缀；
- `.env` 中的 `SMS_RELAY_COOKIE_PATH`；
- `FEISHU_REDIRECT_URI` 中的回调路径。

应用只应通过 HTTPS 对外服务，因为浏览器会话 Cookie 带有 `Secure` 属性。

## 配置 SmsForwarder

在 SmsForwarder 中创建 Webhook 发送通道：

- 请求方式：`POST`
- URL：`https://relay.example.com/sms-relay/v1/messages`
- Header：`Content-Type: application/json`
- Header：`X-API-Key: <SMS_RELAY_API_KEY>`
- 成功响应关键字：`"ok":true`

请求正文模板：

```json
{
  "type": "sms",
  "from": "{{FROM}}",
  "content": "{{SMS}}",
  "received_at": "{{RECEIVE_TIME}}",
  "sim_info": "{{CARD_SLOT}}",
  "device_name": "android-phone",
  "app_version": "{{APP_VERSION}}"
}
```

然后创建“转发全部短信”规则并选择该通道。`configure_smsforwarder.py` 可以直接更新兼容版本的 SmsForwarder Room 数据库；操作前务必停止应用并备份数据库：

```bash
SMS_RELAY_API_KEY='<64-character-secret>' uv run python configure_smsforwarder.py \
  /path/to/sms_forwarder.db \
  --webhook-url https://relay.example.com/sms-relay/v1/messages \
  --device-name android-phone
```

脚本会把 API Key 写入 SmsForwarder 数据库，因此数据库副本同样属于敏感文件，不能提交到仓库。

## API

| 方法与路径 | 鉴权 | 说明 |
| --- | --- | --- |
| `GET /health` | 无 | 只返回存活状态，不返回短信数量 |
| `POST /v1/messages` | 写入 API Key | 接收一条短信 |
| `GET /v1/messages?limit=50&before_id=123` | 飞书会话或只读 API Key | 按 ID 倒序分页读取历史短信 |
| `GET /v1/messages?limit=50&after_id=123` | 飞书会话或只读 API Key | 按 ID 正序获取游标之后的新短信 |
| `GET /v1/platforms/identify?url=...` | 飞书会话或只读 API Key | 根据页面 URL 返回标准平台 `tag` |
| `GET /auth/login` | 无 | 发起飞书 OAuth |
| `GET /auth/callback` | OAuth state | 处理飞书回调 |
| `GET /auth/session` | 飞书会话 | 返回当前登录用户 |
| `POST /auth/logout` | 无 | 清除当前浏览器会话 |
| `GET /v1/admin/directory` | 管理员会话 | 返回本地企业架构和同步状态 |
| `GET /v1/admin/directory/users` | 管理员会话 | 按部门或姓名分页查询成员 |
| `POST /v1/admin/directory/sync` | 管理员会话 + CSRF | 后台同步飞书企业架构 |
| `GET /v1/admin/access` | 管理员会话 | 返回当前部门与个人授权 |
| `PUT /v1/admin/access` | 管理员会话 + CSRF | 使用版本号原子更新授权 |

写入示例：

```bash
curl -X POST 'https://relay.example.com/sms-relay/v1/messages' \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: <64-character-secret>' \
  --data '{"type":"sms","from":"10086","content":"验证码：483921","sim_info":"SIM1_13800000000","device_name":"android-phone"}'
```

API Key 不支持 Query String，避免密钥进入浏览器历史和代理访问日志。

### 短信标签与平台识别

写入和读取短信时都会返回 `tag`、`sim_slot` 和 `sim_phone`。其中 `sim_phone`
是从 Android 上报的 SIM 信息中解析出的接收手机号。服务从短信正文中按顺序查找第一个非空的
`【…】`，去掉括号和首尾空白后作为标签。例如 `【小红书】验证码 483921` 的
`tag` 是 `小红书`；没有短信签名时返回空字符串。标签在读取时动态生成，因此
部署新版本后，已有历史短信也会立即带上 `tag`，不会改变正文或消息指纹。

页面 URL 可通过独立接口识别为相同口径的平台标签：

```bash
curl --get 'https://relay.example.com/sms-relay/v1/platforms/identify' \
  -H 'X-API-Key: <64-character-read-secret>' \
  --data-urlencode 'url=https://ark.xiaohongshu.com/app-order/order/query'
```

```json
{"ok":true,"recognized":true,"tag":"小红书"}
```

当前支持：

| 页面域名 | `tag` |
| --- | --- |
| `ark.xiaohongshu.com` | `小红书` |
| `s.kwaixiaodian.com` | `快手` |
| `zhaoshang.dxycare.com` | `丁香` |
| `portal.maiscrm.com` | `私域商城` |
| `store.weixin.qq.com` | `微信小店` |
| `fxg.jinritemai.com`、`doudian.douyinec.com` | `抖音商城` |

识别使用解析后的完整 hostname 精确匹配；相似域名不会命中。未知平台返回
`{"ok":true,"recognized":false,"tag":""}`。

### 增量获取新短信

客户端保存已成功处理的最后一个消息 ID，并通过 `after_id` 继续读取：

```bash
curl 'https://relay.example.com/sms-relay/v1/messages?after_id=123&limit=50' \
  -H 'X-API-Key: <64-character-read-secret>'
```

```json
{
  "ok": true,
  "count": 2,
  "messages": [
    {"id": 124, "sender": "10086", "content": "第一条新短信"},
    {"id": 125, "sender": "10086", "content": "第二条新短信"}
  ],
  "next_after_id": 125,
  "has_more": false
}
```

- 返回消息按 `id` 升序排列；`after_id=0` 可从最早消息开始读取。
- 只有整批消息处理成功后，才持久化 `next_after_id`；失败时使用原游标重试。
- `has_more=true` 时应立即使用新的游标读取下一页；否则可按业务需要轮询。
- 没有新短信时返回空数组，`next_after_id` 保持为请求中的 `after_id`。
- `after_id` 与 `before_id` 不能同时使用。

## 本地开发与测试

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/)；前端语法检查还需要 Node.js。

```bash
uv run python -m unittest discover -s tests -v
node --check web/app.js
```

项目结构：

```text
app.py                       HTTP API、OAuth、SQLite 与飞书通知
access_control.py            企业目录快照、授权规则与审计
web/                         无构建步骤的网页收件箱
tests/                       标准库 unittest 测试
configure_smsforwarder.py    SmsForwarder 数据库配置辅助脚本
compose.yaml                 本地安全默认的容器部署
nginx-location.conf          HTTPS 反向代理 location 示例
```

## 安全说明

- 为写入 Key、只读 Key 与 Session Secret 使用三个独立、随机生成的值。
- 只配置受信任的飞书管理员，并定期检查页面授权和目标群成员。
- 权限写入使用 CSRF token 与乐观版本控制；每次浏览器请求都会重新校验当前权限，撤权后旧会话立即失效。
- 仅通过 HTTPS 暴露服务，容器端口保持绑定在 loopback。
- 限制 `data/` 的宿主机文件权限，并制定短信保留和删除策略。
- 发现凭据误提交时，删除文件并不足够，必须立即轮换对应凭据。

安全问题请参阅 [SECURITY.md](SECURITY.md)。

## License

[MIT](LICENSE)
