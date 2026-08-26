# SMS Relay

一个轻量、自托管的短信中转服务。Android 端可通过
[SmsForwarder](https://github.com/pppscn/SmsForwarder) 把短信发送到服务端；服务端保存短信、自动识别常见验证码，并提供接入中央 OAuth 的网页收件箱。识别到验证码后，还可以把验证码推送到指定飞书群。

项目只使用 Python 标准库和原生浏览器 API，不依赖 Web 框架或前端构建工具。

## 功能

- 独立的 64 位写入与只读 API Key，支持 `X-API-Key` 和 Bearer Header。
- SQLite 持久化；同一发送方、正文和消息类型在 60 秒设备接收时间漂移内按同一短信去重。
- 自动识别 4–8 位数字或字母数字验证码，以及 4–32 位字母数字解压密码；所有值均保留短信中的原始大小写。
- 自动把短信中第一个非空 `【…】` 签名提取为 `tag`，历史短信无需迁移即可返回标签。
- 根据受支持后台的 URL 精确识别小红书、快手、丁香、私域商城、微信小店和抖音商城。
- 标准 OAuth 2.0 Authorization Code + PKCE `S256`，由中央授权系统统一决定应用入口权限。
- BFF 服务端 Session：中央 access/refresh token 只保存在 SQLite，浏览器仅持有随机 opaque Cookie。
- 每次受保护访问执行短缓存 introspection；中央撤权、站点停用或服务不可用时 fail closed。
- 验证码点击复制，显示接收短信的 SIM 卡槽和手机号。
- 可选的飞书群验证码通知（包含短信 `tag` 平台标签），失败后后台重试。
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
    ├───────────► Central OAuth（网页登录、introspection、refresh、revoke）
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

分别生成两个独立的 64 字符随机值，填入 `.env`：

```bash
uv run python -c "import secrets; print(secrets.token_hex(32))"
uv run python -c "import secrets; print(secrets.token_hex(32))"
```

必须修改的配置：

| 变量 | 用途 |
| --- | --- |
| `SMS_RELAY_API_KEY` | Android 端写入 API 使用的 64 字符密钥 |
| `SMS_RELAY_READ_API_KEY` | 外部程序读取短信使用的独立 64 字符密钥 |
| `AUTH_ISSUER` | 中央认证 issuer；生产为 `https://auth.midi.lizhijian.xyz` |
| `AUTH_CLIENT_ID` | 已注册 public client；本项目为 `sms-relay-web` |
| `AUTH_AUDIENCE` | introspection 固定 audience；本项目为 `sms-relay-api` |
| `AUTH_SCOPES` | 必须具备的入口 scope；本项目为 `sms-relay:access` |
| `AUTH_REDIRECT_URI` | 已在中央注册的精确 HTTPS callback URI |
| `AUTH_POST_LOGOUT_REDIRECT_URI` | 已在中央注册的精确退出后 URI |
| `AUTH_BACKCHANNEL_IP` | 可选；跨云链路按SNI重置时使用的固定中央认证IP，仍严格校验公开IP SAN证书 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 可选；仅用于群机器人通知，不参与登录 |
| `FEISHU_CHAT_ID` | 可选；接收验证码通知的群 Chat ID |

### 2. 配置中央授权

SMS Relay 不再直接接入飞书 OAuth，也不在本地维护 Open ID、Union ID、部门或个人登录白名单。中央认证必须预先注册并授权本项目的固定客户端合同：

```text
issuer:       https://auth.midi.lizhijian.xyz
client_id:    sms-relay-web
audience:     sms-relay-api
scope:        sms-relay:access
redirect_uri: https://api.midi.lizhijian.xyz/sms-relay/auth/callback
```

中央侧需要先为正式用户或部门建立 grant，再为 `sms-relay-web` 启用动态授权策略。grant 尚未准备时不要直接启用策略，否则普通用户会被默认拒绝。应用只接受中央 introspection 中 `active=true`、`clientId=sms-relay-web` 且 scopes 包含 `sms-relay:access` 的主体，不再执行第二套本地白名单。

完整登录、服务端 Session、refresh/revoke、失败关闭语义和上线前置条件见 [docs/AUTHENTICATION.md](docs/AUTHENTICATION.md)。

当前 `ys` 到中央认证公网IP的带SNI TLS会被跨云链路重置。部署到该主机时可设置
`AUTH_BACKCHANNEL_IP=139.196.114.210`，仅将服务端 metadata/token/introspection/
revoke传输改为固定IP；浏览器授权URL和OAuth issuer保持域名。该模式不会关闭CA校验，
详情及抓包边界见上述认证文档。

如需群通知，再在飞书开放平台启用机器人和发送群消息权限，把机器人加入目标群，并填写 `FEISHU_APP_ID`、`FEISHU_APP_SECRET` 和 `FEISHU_CHAT_ID`。这些凭据只用于消息推送，不参与网页登录，也不要提交到仓库。

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
- 中央注册表与 `.env` 中的 `AUTH_REDIRECT_URI`、`AUTH_POST_LOGOUT_REDIRECT_URI`。

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

### 可靠投递 Outbox

`android-outbox/` 提供设备侧持久化补偿层。它在收到 `SMS_RECEIVED` 广播时先把短信同步写入私有 SQLite Outbox，只有服务端返回 HTTP 2xx 且 JSON `ok=true` 后才确认成功；DNS、网络和 5xx 故障按指数退避，并在网络重新验证后立即补传。

原 SmsForwarder 和 Outbox 可以同时启用。服务端以短信类型、发送方、正文和设备接收时间作为投递身份，忽略客户端版本、设备名和 SIM 展示格式差异；两个客户端上报的设备接收时间即使相差不超过 60 秒，也只会入库和推送一次。设备安装、动态 Key 配置与 Magisk systemizer 说明见 [`android-outbox/README.md`](android-outbox/README.md)。

## API

| 方法与路径 | 鉴权 | 说明 |
| --- | --- | --- |
| `GET /health` | 无 | 只返回存活状态，不返回短信数量 |
| `POST /v1/messages` | 写入 API Key | 接收一条短信 |
| `GET /v1/messages?limit=50&before_id=123` | 中央 OAuth 应用会话或只读 API Key | 按 ID 倒序分页读取历史短信 |
| `GET /v1/messages?limit=50&after_id=123` | 中央 OAuth 应用会话或只读 API Key | 按 ID 正序获取游标之后的新短信 |
| `GET /v1/platforms/identify?url=...` | 中央 OAuth 应用会话或只读 API Key | 根据页面 URL 返回标准平台 `tag` |
| `GET /auth/login` | 无 | 生成 state/PKCE 并跳转中央认证 |
| `GET /auth/callback` | OAuth state + issuer + transaction Cookie | 兑换中央 token 并创建 BFF Session |
| `GET /auth/session` | 中央 OAuth 应用会话 | introspection 后返回当前用户 |
| `POST /auth/logout` | 应用会话 | 先撤销中央 refresh grant，再清除应用 Session |

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
app.py                       HTTP API、BFF Session、SQLite 与飞书通知
central_auth.py              中央 OAuth metadata、PKCE、token、introspection 与 revoke
web/                         无构建步骤的网页收件箱
tests/                       标准库 unittest 测试
android-outbox/              Android 持久化 Outbox、构建脚本与 Magisk systemizer
configure_smsforwarder.py    SmsForwarder 数据库配置辅助脚本
compose.yaml                 本地安全默认的容器部署
nginx-location.conf          HTTPS 反向代理 location 示例
```

## 安全说明

- 为写入 Key 和只读 Key 使用两个独立、随机生成的 64 字符值。
- 登录入口权限只在中央授权系统配置；应用不得恢复本地 Union ID/Open ID 白名单形成双策略。
- 中央 token 只保存在服务端 SQLite，浏览器 Cookie 仅含随机句柄；中央不可用、撤权或 introspection inactive 时拒绝受保护访问。
- 仅通过 HTTPS 暴露服务，容器端口保持绑定在 loopback。
- 严格限制 `data/` 的宿主机文件权限；数据库同时包含短信与中央 token，并应制定保留、备份和删除策略。
- 发现凭据误提交时，删除文件并不足够，必须立即轮换对应凭据。

安全问题请参阅 [SECURITY.md](SECURITY.md)。

## License

[MIT](LICENSE)
