# 出站指纹：怎么和官方客户端做到同形

上游对 `/v2/chat/completions` 有渠道校验（不通过回 `400 code 11128`）。2026-10-24 的
逐头实验摸清了「它今天认哪三个头」；2026-10-27 这一轮做的事是另一维度：
**让网关发出的字节在 TLS 层和 HTTP 层都长得像官方客户端**，而不是「头名字对得上」。

结论先说（都可复现，命令见文末）：

| 维度 | 改之前 | 改之后 | 官方客户端真值 |
| --- | --- | --- | --- |
| TLS JA3（md5） | `503dcc05…`（Python/OpenSSL 默认 + ALPN） | `46648fde…` | `46648fde…` |
| ALPN 扩展 | `http/1.1` | 无 | 无（`https.Agent` 不设 ALPNProtocols） |
| 头名大小写 | `X-IDE-Type` / `X-B3-Traceid`（httpx 归一化后的畸形形态） | 全小写 | 全小写（axios / fetch） |
| `Accept-Encoding` | `gzip, deflate` | 不发 | 不发 |
| `Origin` / `Referer` | 发 | 不发 | 不发 |
| `x-no-*` 取值 | `1` | `true` | `true` |
| 会话 id | uuid5（按内容哈希） | uuid7 | uuid7 |
| 消息 id | uuid4 hex | uuid7 hex | uuid7 hex |
| `x-conversation-request-id` | 每条 HTTP 一个 | 一轮 prompt 一个 | 一轮 prompt 一个 |
| `x-userinfo` | 不发 | 企业账号发 | `base64(JSON{uin,owner_uin,…})` |
| `x-device-id-list` | 不发 | 两个站点都发（见下节） | `{"qimei36":…}` |
| 连接 | 每个请求新建客户端（新 TCP+TLS） | 共享 keep-alive 客户端 | `https.Agent({keepAlive:true})` |
| Cookie | httpx 自带 jar | 空 jar（永不发 Cookie） | 无 jar |

## 证据从哪来

不是文档推测，是官方安装包里的实现 + 本机抓包：

- **发 chat 请求的是内置 CLI**：`app.asar` 的 `cli/dist/codebuddy-headless.js` 里，
  `ModelProvider` 用 **axios 1.16.1** + `https.Agent({keepAlive:true, keepAliveMsecs:2000})`
  发 `POST /v2/chat/completions`，TLS 参数只有 `rejectUnauthorized` 和可选的 CA bundle
  —— 即 **Node/OpenSSL、HTTP/1.1、无 ALPN、无 HTTP/2**。
- **运行时版本**：客户端自带的 Node 是 `~/.workbuddy-ai/binaries/node/versions/22.22.2-6/bin/node`
  （v22.22.2 / OpenSSL 3.5.5）。直接把 `tls.DEFAULT_CIPHERS` 抄进
  `upstream_transport.NODE_DEFAULT_CIPHERS`，本项目 venv 的 Python 也是 OpenSSL 3.5.5，
  于是两边的 ClientHello 在 cipher 列表、扩展顺序、曲线列表上完全一致。
- **头名小写**：axios 的 `AxiosHeaders` 用 `normalizeHeader()` 把 key 全转小写；
  桌面端主进程那条 fetch 路径用 `Headers.set()`，同样小写。真机上 Node 只会额外补
  `Host` / `Connection: keep-alive` / `Content-Length` 三个首字母大写的自动头。
- **id 语义**：`buildConversationHeaders()` 用 `uuidv7()`：
  `x-conversation-message-id = uuidv7 去掉短横线`、`x-request-id` 与它同值、
  `x-conversation-request-id` 是「本轮 prompt」的 id（工具循环里多次模型调用共用一个）、
  `x-agent-type` 由 `resolveAgentType()` 给出（主 agent 是 `main`）。
- **身份头**：`runIdentityHeaders()` 产出 `X-User-Id` / `X-Enterprise-Id` /
  `X-Auth-Method` / `X-Id-Source` / `X-Userinfo`；`X-Userinfo` 只在
  「有 uid 且（有企业 id 或有 idSource 或 有 method）」时才发，内容是
  `base64(JSON{uin, owner_uin, id_source, token_source})`。
- **设备头**：`main/module.app-server.js` 的 `AuthStateHeaderProvider` 固定发
  `X-Custom-Channel-Id: unknown`，有 qimei 时再发
  `X-Device-Id-List: {"qimei36":…}`。qimei 的来源见下一节。
- **`X-No-*` 取值**：`main/code-cache.js` 里是 `"X-No-User-Id": "true"`（不是 `1`）。

## qimei36（`X-Device-Id-List`）从哪来

国内版客户端自己算：`main/log-acl-guard.js` 里的 `QimeiDetector` 从
`app.asar.unpacked/cli/product.json` 读 `config.qimei36AppKey.darwin`，spawn
`main/qimei-helper.js`（`@tencent/qimei-node` + macOS framework），把结果写进
`~/.workbuddy/qimei-cache.json`。

国际版**没有这一步**：它的 `product.json` 里根本没有 `qimei36AppKey`，日志每天都是
`[QimeiDetector] appKey missing, skip`，所以永远不会生成缓存文件 —— 也就是说真实的
国际版客户端发出的请求里本来就没有这个头。但要发也不是没法发：

- qimei36 是**设备级** id。实测：把 `HOME` 指到空目录、用国内版 appKey 跑**国际版自带
  的** `main/qimei-helper.js`，返回的仍是这台机器原来那个 `c918e83b…`，与国内版缓存
  文件里的值完全一致。也就是说 appKey 只影响 SDK 初始化，设备号本身跟产品无关。
- 所以 `fingerprint.qimei36()` 的解析顺序是：`CB_GATEWAY_QIMEI36` 覆盖 →
  站点客户端缓存文件 → 网关自己的落盘缓存（`.debug/qimei36.json`）→
  **用官方自带的 helper + 任意一款客户端的 appKey 现算**（国际版缺 key 就用国内版的）。
  拿到的都是这台机器真实的 Turing 设备号，不是编出来的字符串。
- 现算走后台线程、8s 超时：组装请求头是同步路径，不能为了一个头把事件循环卡住；
  刚启动、还没算出来的那几条请求会像官方客户端 backfill 之前一样不带这个头，
  之后（以及之后所有进程，因为结果已落盘）都会带上。
- 排障开关：`CB_GATEWAY_QIMEI36=off` 整个摘掉这个头；
  `python scripts/fingerprint_probe.py --qimei` 看两个站点各自解析到哪一步。

## 代码落点

- `buddy2api/fingerprint.py`：uuid7 生成器、会话 id 缓存（uuid7，按会话稳定）、
  一轮 prompt 的 request id（ContextVar）、全小写且按官方拼装顺序插入的头、
  `x-userinfo` / `x-no-*` / 设备头、qimei36 的多级解析（缓存文件 → 落盘 → 官方 SDK 现算）。
- `buddy2api/upstream_transport.py`：`NODE_DEFAULT_CIPHERS` + `build_ssl_context()`
  （`_NoAlpnContext` 把 httpcore 的 `set_alpn_protocols` 变成 no-op）、
  `build_client()` / `shared_client()`（共享 keep-alive 客户端、摘掉 httpx 默认头、
  空 cookie jar、请求头排在自动头之前）。
- `buddy2api/wire_probe.py` + `scripts/fingerprint_probe.py`：本地自签 TLS 服务端抓
  自己发出的 ClientHello 与请求头，算 JA3 并与基准比。

## 残留差异（知道在哪，也知道为什么没改）

1. **`Host` 的位置**：h11 强制把 `Host` 写第一行（RFC 7230 的建议），Node 是把它和
   `Connection` / `Content-Length` 一起放在最后。要完全一致就得自己写 HTTP/1.1
   序列化，代价是重写流式/代理/超时/停转判定，收益只是头序 —— 不做，记在这里。
2. **TCP 层**：官方客户端是 Node socket，我们是 asyncio + OpenSSL；IP TTL、窗口缩放、
   TCP 时间戳这些内核级特征不归应用层管。
3. **Turing 设备令牌**：官方客户端会带 `X-Device-Token`（`getTuringDeviceToken()`，
   由 TuringShield 原生 SDK 产出）。这是反作弊 SDK 的产物，网关无法伪造，也**不去伪造**。
4. **内置 CLI 的真实 UA**：CLI 的 axios 在调用方没设 UA 时会填 `axios/1.16.1`；
   网关仍按上面实测过的 `WorkBuddy/…`（桌面流水线）与 `CLI/…`（CLI 流水线）发，
   因为这两条流水线都验证过 200。想试 axios 形态可以用
   `CB_GATEWAY_USER_AGENT=axios/1.16.1` 临时覆盖。
5. **ALPN 之外的安全能力**：没有 client certificate、没有 session ticket 复用到
   同一个 resumption 域 —— 都取决于服务端策略，不构成指纹差异。

## 怎么复现 / 怎么回归

```bash
python scripts/fingerprint_probe.py                # 桌面端 profile，逐项打印
python scripts/fingerprint_probe.py --channel cli
python scripts/fingerprint_probe.py --json         # 一致时退出码 0，可进 CI
python -m pytest tests/test_fingerprint_headers.py tests/test_fingerprint_wire.py
```

`tests/test_fingerprint_wire.py` 会把 JA3、`accept-encoding` 的有无、头名大小写、
python/httpx/axios 自报 UA 这些项钉死；`tests/test_fingerprint_headers.py` 钉住头集合、
头序、uuid 版本位与 id 语义。

## 线上验证记录（2026-10-27）

同一天用改后的头对真上游各发一条最小流式请求（不经过网关进程，直接走新代码路径）：

- 国际站（`www.workbuddy.ai`）：`200`，回包首行 `: heartbeat`（桌面流水线）；
- 国际站 + 现算出来的 `x-device-id-list`（补上国际版客户端自己缺的那一步）：`200`；
- 国际站 + `CB_GATEWAY_CHANNEL=cli`：`200`，无 heartbeat（CLI 流水线，渠道切换兜底可用）；
- 国内站（`www.workbuddy.cn`，带本机 qimei 的 `x-device-id-list`）：`200`。

`400 code 11128` 在这三次里都没有出现。
