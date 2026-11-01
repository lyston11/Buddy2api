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
| `Accept-Encoding` | 不发 | `gzip, compress, deflate` | 同左（axios `httpAdapter` 每次自己补） |
| `x-private-data` | `true`（写死） | `false`（= 厂商默认） | `enableModelOptimization ? "false" : "true"` |
| `acp-connection-id` / `x-codebuddy-request` / `x-agent-purpose` | 塞进 chat | 不发 | 只出现在本地 ACP 通道 |
| `x-custom-channel-id` / `x-device-id-list` | 塞进 chat | 不发 | 只在 `/auth/state`（`AuthStateHeaderProvider`） |
| `x-no-*` / `x-domain` | 塞进 chat | 不发 | `x-no-*` 只在 `/auth/state` 首调；`x-domain` 只在 iOA 环境 |

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

## 2026-11-01：按两个发行版的可执行代码逐个字段复核

前面几轮的依据是 `app.asar` 里的 `main/*`，但**真正发 `/v2/chat/completions` 的是内置
CLI**（`cli/dist/codebuddy-headless.js` 的 `axiosToFetchAdapter`）。这一轮把两份发行版
（国内 `WorkBuddy.app` 5.5.6 / CLI 2.137.1；国际 `WorkBuddy AI.app` 5.7.6 / CLI 2.156.1）
都拆开，只统计**该函数体内**写进 chat 请求的头，得到确切的集合：

```
accept / content-type / accept-encoding / x-requested-with
x-conversation-id / x-conversation-request-id / x-conversation-message-id / x-request-id
x-agent-intent / x-agent-type
x-ide-type / x-ide-name / x-ide-version / x-private-data / x-product
authorization / user-agent
traceparent / b3 / x-b3-traceid / x-b3-parentspanid / x-b3-spanid / x-b3-sampled / x-trace-id
x-user-id（+ 企业账号的 x-enterprise-id / x-tenant-id / x-userinfo）
```

两个发行版在这上面完全一致。在此之前网关多发了 7 个头、少发 1 个、写错 1 个值：

- **多发**：`acp-connection-id`（本地 ACP 通道的 id）、`x-codebuddy-request`（服务端标记）、
  `x-agent-purpose`、`x-custom-channel-id` 与 `x-device-id-list`（`/auth/state` 一侧的
  `AuthStateHeaderProvider`）、`x-no-user-id` / `x-no-enterprise-id`（`/auth/state` 首调的
  「这几个字段为空」标记）、`x-domain`（iOA 环境才由 `applyIOADefaultHeaders` 补）。
  一个真实客户端不会在 chat 上发其中任何一个。
- **少发**：`accept-encoding: gzip, compress, deflate` —— chat 走 axios，它的 `httpAdapter`
  在每次请求上都会自己 `set("Accept-Encoding", ...)`。之前为了模仿 Node 裸 http 模块
  「什么都不发」，反而是缺了个真实客户端必带的头。
- **写错**：`x-private-data`。官方 `getCustomHeaders()` 的取值是
  `enableModelOptimization ? "false" : "true"`，该设置未配置时默认为 enabled，所以真实
  客户端发 `false`；网关此前写死 `true`，与厂商默认值正相反。可用
  `CB_GATEWAY_PRIVATE_DATA=true` 覆盖（对应「在客户端里关掉模型优化」的状态）。

### TLS 指纹依赖解释器，必须自检

JA3 的 cipher 列表由 `NODE_DEFAULT_CIPHERS` 决定，但 **`ec_point_formats` 由 OpenSSL
版本决定**：3.5.5 → `[3,0,1,2]`（与官方 Node v22.22.2 一致），3.6.2 → `[1,0]`
（md5 变成 `14cb250fd211…`）。官方客户端自带 Node 运行时是 OpenSSL 3.5.5，项目 `.venv`
也是 3.5.5，所以只有用 `.venv/bin/python` 启动才能对上；用系统 Python（3.6.2）启动会
静默换一枚指纹。为此：

- `upstream_transport.tls_fingerprint_md5()` 用 `ssl.MemoryBIO` 在内存里逼出 ClientHello
  现算 JA3（不联网），`fingerprint_warning()` 给出人话告警；
- `server.main()` 启动横幅里打印「出站指纹: 与官方客户端一致」，不一致时打整段警告；
- `tests/test_fingerprint_wire.py::test_tls_fingerprint_self_check_matches_reference`
  把这条钉进 CI（在错的解释器下会红，这正是想要的信号）。

### 线上复核（2026-11-01）

对 6 个真实账号（国内 3 + 国际 3）用新头集合各发 2 条 `deepseek-v4.1-flash` 流式请求：
全部 `200`，首行仍是 `: heartbeat`（桌面流水线）。同时做了对照组：整组删除上述任一头、
或只保留真实客户端的头集合，**上游都回 200** —— 说明这些差异目前不是硬性拦截条件，
但它们正是「账号级风控」（11140/11141/11142）会据以判定的信号，所以按真实客户端的
实际形态收敛，而不是等被拦了再改。
