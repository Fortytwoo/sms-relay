# 中央认证接入

SMS Relay 只使用中央 OAuth 认证，不再包含直接飞书 OAuth、本地 Open ID/Union ID
白名单、企业目录同步或部门/个人登录授权。应用入口权限完全由
`feishu-auth-service` 的客户端 grant 决定。

## 固定客户端合同

| 配置 | 生产值 |
| --- | --- |
| `AUTH_ISSUER` | `https://auth.midi.lizhijian.xyz` |
| `AUTH_CLIENT_ID` | `sms-relay-web` |
| `AUTH_AUDIENCE` | `sms-relay-api` |
| `AUTH_SCOPES` | `sms-relay:access` |
| `AUTH_REDIRECT_URI` | `https://api.midi.lizhijian.xyz/sms-relay/auth/callback` |
| `AUTH_POST_LOGOUT_REDIRECT_URI` | `https://api.midi.lizhijian.xyz/sms-relay/?auto_sso=off` |

客户端是 public client，不配置 `client_secret`。除 issuer 外的协议端点优先从
`/.well-known/oauth-authorization-server` 读取。

2026-08-26 从 `s1` 实测的生产 metadata 尚未返回文档声明的
`introspection_endpoint`。适配器因此仅在该字段缺失时，回退到同 issuer 下的文档
固定路径 `/auth/introspect`；若 metadata 返回该字段，则仍以 metadata 为准，并拒绝
跨 origin 端点。中央服务补齐 metadata 后，应用无需改配置。

## 浏览器登录流程

1. `GET /auth/login` 在 SQLite 创建 5 分钟、单次使用的 transaction。
2. 服务端生成 `state`、浏览器绑定值和 PKCE verifier，只把 `state`、S256 challenge
   发给中央 authorize endpoint。
3. 浏览器只保存 `Secure`、`HttpOnly`、host-only、`SameSite=Lax` transaction Cookie。
4. callback 拒绝重复参数，校验 transaction 浏览器绑定、单次 state 和精确 issuer。
5. 后端使用 code + verifier 兑换 token，再使用固定 issuer/audience introspection。
6. 只有 `active=true`、`clientId=sms-relay-web`、scope 包含
   `sms-relay:access` 且存在稳定主体时才创建应用 Session。
7. 浏览器 Session Cookie 只包含随机 opaque handle；中央 access/refresh token 和主体
   快照只保存在服务端 SQLite。

callback transaction 在首次有效消费时删除，因此重复 callback 不会再次兑换 code。
错误响应和日志不包含 code、state、verifier、token 或中央返回的原始错误描述。

## 受保护接口

网页 Session 访问 `/auth/session`、`/v1/messages` 或
`/v1/platforms/identify` 时，后端执行中央 introspection。默认只缓存 5 秒正向结果：

- `active=false`、client 不匹配、scope 不足或主体无效：删除本地 Session并返回未授权；
- 中央超时、5xx、响应无法解析：fail closed，返回 `503`，不降级到 API 白名单；
- access token 临近过期：在进程级锁内串行 refresh；
- refresh 成功：access/refresh token 在同一 SQLite 事务中原子轮换；
- `invalid_grant`：立即删除应用 Session，要求重新登录。

短信写入和程序化读取仍使用彼此独立的 64 字符 API Key。这两类 API Key 是设备和
服务间合同，不参与浏览器 OAuth，也不能用写入 Key 读取短信。

## 退出

`POST /auth/logout` 先向中央 revocation endpoint 撤销当前 refresh grant，成功后才删除
SQLite Session和浏览器 Cookie。若中央不可用，返回 `503` 并保留 Session，页面不得
显示“已退出”。该退出只影响 SMS Relay 应用，保留中央 SSO。

## 上线前置条件

1. 中央注册表中的 `sms-relay-web` 合同与上表逐字一致。
2. 中央侧先为正式用户/部门建立 SMS Relay grant。
3. 只对 `sms-relay-web` 启用动态授权策略；grant 尚未准备时不得启用。
4. `ys` 必须能通过 HTTPS 访问 issuer 的 metadata、token、introspection 和 revoke。
5. 使用一个授权用户完成 callback、refresh、revoke；使用一个未授权用户完成拒绝验收。
6. 确认 Nginx 不记录 callback query，应用日志不含 OAuth 材料。

2026-08-26 只读核对 `s1` 运行容器时，动态策略全局开关仍为 `false`，精确
client 列表仅包含 `author-order-web`，尚未包含 `sms-relay-web`。因此本仓库代码完成
不等于中央入口授权已经切换，必须按上述顺序单独执行中央 grant 与策略启用。

当前实测 `ys -> auth.midi.lizhijian.xyz:443` 在 TLS 握手阶段被
`139.196.114.210` 重置，而 `s1` 本机可通过其代理访问。因此在把本改动部署到 `ys`
之前，必须先打通该 backchannel；应用不会为此降级到旧鉴权。

2026-08-26 双端包头抓取进一步确认：S1 正常回复 SYN-ACK，但公网链路在带 SNI
ClientHello 后向 S1 注入来自 `ys` 方向的 RST；同一 IP 不带 SNI 时 TLS 可建立。
如该跨云链路策略短期无法解除，可设置：

```env
AUTH_BACKCHANNEL_IP=139.196.114.210
```

该配置只改写服务端 metadata/token/introspection/revoke 的传输地址；浏览器 authorize
URL、OAuth issuer、callback和 token 中的 issuer 均继续使用
`https://auth.midi.lizhijian.xyz`。Python 连接固定 IP 时不发送域名 SNI，并严格校验
公开 CA 签发的 `IP Address:139.196.114.210` SAN证书；加密后的 HTTP Host仍是原
issuer。配置只接受 literal IP，metadata端点仍必须与 issuer同源。禁止关闭 CA校验、
使用自签名证书或把该值改成任意代理主机。
