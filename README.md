# Buddy2api 2.0

[English](README_EN.md) | 中文

> 把本机已经登录的消费级 AI 客户端，接成 OpenAI 兼容接口，给 Codex、OpenCode、Cherry Studio、NextChat 等用。默认打开 Work Buddy / CodeBuddy、QClaw、千问办公（QwenWork）、TraeWork 四个通道；管理页下拉选其中一个。一次请求只走一个通道。

当前版本 **2.1.9**。这个项目只适合本机自用，不要公开部署，也不要把登录凭据、API Key、数据库文件发给别人。

默认本机启动会自动打开管理页，无需填写管理 Token，重启后已有页面仍可直接使用。重复启动会打开已运行的同一实例；同一数据库不能同时由多个实例使用。后台服务可加 `--no-browser`。显式设置 `--admin-token` 或 `CB_GATEWAY_ADMIN_TOKEN` 时启用凭证管理模式；非本机监听必须设置该凭证，并在管理页设置中填写。客户端 API Key 和上游账号授权不受影响。

## 这是什么？

Buddy2api 在本机提供 `http://127.0.0.1:8787/v1`。你在官方客户端里登录并且还有额度，这个网关把本机登录导入进来，把请求转到对应厂商。普通客户端走 Chat Completions；Codex 走 `/v1/responses`，管理页把 Key 类型选成 Codex 时会做一轮内容清洗。

四个通道默认都开。没装、没登录的通道，账号页检测为空，不会自动入库。

```powershell
python -m buddy2api
```

| 通道 | 默认 | 本机登录位置 |
|---|---|---|
| WorkBuddy / CodeBuddy | 开 | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth` |
| QClaw | 开 | `%APPDATA%\QClaw` |
| 千问办公 QwenWork | 开 | `%APPDATA%\QwenWorkCN` |
| TraeWork | 开 | Windows：`%APPDATA%\TRAE SOLO CN\User\globalStorage`；macOS：`~/Library/Application Support/TRAE SOLO CN/User/globalStorage` |

路径不对时可用 `CB_AUTH_DIR`、`CB_QCLAW_AUTH_DIR`、`CB_QWENWORK_AUTH_DIR`、`CB_TRAEWORK_AUTH_DIR` 指定。四个通道的登录文件不要混在同一个目录。只要其中一家时，可设 `CB_GATEWAY_PROVIDERS=workbuddy` 收窄。

## 注意事项

按下面「安装与启动」即可。这几条是 2.0 里最容易踩空的：

1. **启动后账号页是空的，这是正常的。** 默认不再自动入库。到「账号」页：选通道 → 重新检测 → 一键导入。四个通道都能选。
2. **一把 API Key 只打一个通道。** 创建时必须选通道。WorkBuddy 的 Key 发 `auto` / `glm-5.2`；QwenWork 的 Key 发 `auto` 或 `qwork-advanced`；TraeWork 的 Key 发 `auto` 或 `qwen-3.7-plus`。通道和模型对不上会 400 或 403，不会帮你转到另一家。

   也可以**把一把 Key 钉在某个具体账号上**：创建或编辑 Key 时选一个账号，之后这把 Key 的请求只走那个账号，不再由调度挑选。想让某把 Key 单独消耗某个账号的额度时用它。绑定后如果该账号不可用（停用、冷却中、通道不符），请求会直接失败，**不会**静默换号——否则你会以为额度没动，其实已经在吃别的账号。不填就是默认的自动选号。

   注意：这种失败目前和「通道里没有可用账号」共用同一个错误（503 `channel_unavailable`），看到它时记得先确认是不是钉住的账号挂了。
3. **某个通道返回 503 `channel_unavailable`：** 这个通道还没导入可用账号。
4. **QClaw / QwenWork 请在 Windows 上直接跑 `python -m buddy2api`。** Linux Docker 读不了这两家用 DPAPI 加密的本机文件；管理页会写明这一点。WorkBuddy 可以继续用 Docker。
5. 本项目和聊天客户端最好在同一台电脑。客户端如果跑在 Docker 里，Base URL 填 `http://host.docker.internal:8787/v1`，不要填容器自己的 `127.0.0.1`。

## 安装与启动

还没装环境时按这几步走。已经有虚拟环境的，装完 `requirements.txt` 后执行 `python -m buddy2api` 即可。

### 1. 安装工具

1. [Git](https://git-scm.com/downloads)，Windows 保持默认选项
2. [Miniconda](https://docs.conda.io/projects/miniconda/en/latest/)，推荐 Python 3.12
3. 先打开并登录你要用的官方客户端（至少 Work Buddy / CodeBuddy）

装完后**重新打开** PowerShell、Windows Terminal 或 Anaconda Prompt：

```powershell
git --version
conda --version
```

找不到 `conda` 时，用开始菜单里的 **Anaconda Prompt / Miniconda Prompt**。也可以在那里执行 `conda init powershell`，关掉窗口再开。

### 2. 克隆项目

```powershell
git clone https://github.com/wicm84266964/Buddy2api.git
cd Buddy2api
Get-ChildItem README.md, requirements.txt, buddy2api
```

后面的命令都要在这个目录里执行。

### 3. 用 Conda 启动（推荐）

```powershell
conda create -n buddy2api python=3.12 -y
conda activate buddy2api
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m buddy2api
```

看到监听信息后，浏览器打开：

```text
http://127.0.0.1:8787
```

停止服务：回到终端按 `Ctrl+C`。下次开机后：

```powershell
cd <你的项目路径>\Buddy2api
conda activate buddy2api
python -m buddy2api
```

提示符前面应出现 `(buddy2api)`，再执行 `python -m pip`，避免装到系统 Python。

### 其他启动方式

- **脚本：** Windows 安装 Python 时勾选 Add Python to PATH，在项目目录执行 `.\scripts\start.bat`。Linux / macOS：`chmod +x scripts/start.sh && ./scripts/start.sh`。脚本优先用名为 `buddy2api` 的 Conda 环境，没有 Conda 才建 `.venv`。
- **Docker：** `powershell -ExecutionPolicy Bypass -File .\scripts\start-docker-win.ps1`。本机没有 WorkBuddy 登录目录时脚本仍会启动。容器下拉里仍有四个通道，但 QClaw / QwenWork 请用上面的 `python -m buddy2api`。TraeWork 登录文件不是 DPAPI，本机 `python -m buddy2api` 导入后 Docker 也能用库里的 token。

### 第一次打开网页之后

本机管理页自动授权，不用粘贴 Token，也不依赖管理 Cookie。

1. 打开「账号」，点击「一键导入全部通道」读取各启用通道的默认登录目录；也可先选择通道，再点击「导入当前通道」。自定义路径仅用于当前通道，留空使用默认目录，切换通道会清空路径。导入后逐通道显示结果，未登录的通道会跳过。
2. 点该账号的「测试」，能返回一句话就说明这条通道通了。
3. 打开「API Keys」，**先选同一个通道**再创建。给 Codex 用时 Key 类型选 Codex，接口用 `/v1/responses`。创建后可以再显示、复制完整 Key。
4. 在客户端里填：
   - Base URL：`http://127.0.0.1:8787/v1`
   - API Key：刚复制的 Key
   - 模型：WorkBuddy 用 `auto` 即可；QClaw 用 `auto`；千问办公用 `auto` 或 `qwork-advanced`；TraeWork 用 `auto` 或 `qwen-3.7-plus`。上游加了新模型时，到「模型配置」点「一键读取供应模型」；各通道目录分开保存，选错通道仍会 400/403。

管理页打不开或要远程访问时：

```powershell
$env:CB_GATEWAY_ADMIN_TOKEN="cb-admin-请换成足够长的随机值"
python -m buddy2api
```

### 更新

先 `Ctrl+C` 停掉正在跑的服务：

```powershell
cd <你的项目路径>\Buddy2api
git pull --ff-only
conda activate buddy2api
python -m pip install -r requirements.txt
python -m buddy2api
```

## 常见问题

- WorkBuddy 聚合响应（包括默认停转重试所用的工具续聊路径）在缺少完成标记时返回上游错误，不再把部分正文默认为正常 `stop`。明确的 `finish_reason` 后直接 EOF 仍被接受；仅收到 `[DONE]`、但正文没有结束原因时不会当作正常完成。此校验不能判定模型主动 `stop` 是否过早，也不保证解决所有长会话停转。

- `git` 或 `conda` 不是内部命令：关掉终端重开；Conda 用户改用 Miniconda Prompt。
- `No module named ...`：先 `conda activate buddy2api`，再 `python -m pip install -r requirements.txt`。
- 下载依赖很慢：确认能访问 PyPI，不要混用好几个 Python。
- 端口 8787 被占用：关掉旧的 Buddy2api，或 `python -m buddy2api --port 8788`。
- 网页里一个账号都没有：还没导入。选对通道再检测；登录目录不对就设 `CB_AUTH_DIR` / `CB_QCLAW_AUTH_DIR` / `CB_QWENWORK_AUTH_DIR`。
- 创建 Key 失败：没选通道。
- 客户端 503 `channel_unavailable`：这个 Key 绑定的通道还没有可用账号；如果这把 Key 钉了具体账号，也可能是钉住的账号当前不可用（停用 / 冷却中 / 通道不符）。改绑、或把绑定清成「不绑定」回到自动选号。
- 客户端 403 `key_channel_mismatch`：模型带了别的通道前缀，和当前 Key 不一致。
- 客户端 400 `unknown_model`：模型不属于这把 Key 的通道。换 Key，或改成该通道认识的 id。

### 账号很多时只打在一两个账号上？

选号看的是**最近一段时间内每个账号实际服务了多少请求**（默认 15 分钟窗口，`CB_GATEWAY_ROUTE_WINDOW_SECONDS` 可调），请求少的先用。所以短暂偏向某个账号是正常的，窗口滑过去就会自己纠回来。

账号表里的「请求」列是**终身累计**统计，只增不减，**不参与选路**。老账号的历史计数再高也不会让新账号被冷落。

选号顺序是「优先级 → 权重 → 窗口内请求数/权重少的先用」，同级账号之间还带一层粘性（同一模型尽量回上次的账号，保住 prompt cache），只有在粘住的账号比同级最空闲的多干了超过 1 个权重单位时才让位。

### 账号被限额后，其它账号也用不了？

上游限额（HTTP 429，WorkBuddy `code 6004`「使用量已超出频率限制」）是**按模型**算的，不是按账号：同一个账号在 deepseek 上被限额时，它在 glm 上照样能用。网关按这个语义处理：

- 被限额的账号只会从**该模型**的候选里去掉（`CB_GATEWAY_RATE_LIMIT_COOLDOWN_SECONDS` 可调冷却，默认 900 秒），不影响它服务其它模型。冷却到点会自动再试一次，仍被限就重新计时。
- 换号重试的账号数上限是 8（`CB_GATEWAY_MAX_ACCOUNT_ATTEMPTS` 可调）。这是关键：以前固定试 3 个账号，账号多于 3 个时，前 3 次可能全撞在限额账号上，健康账号从头到尾没被选中——表现就是「明明还有能用的账号，却一直请求失败」。
- 被限流的账号在冷却期内不会走「过期账号刷新」那条回退路径。
- 把 Key 钉在某个具体账号上时，该账号正在这个模型上被限额会直接失败（绑定语义是「只用这个账号」，不会反复撞墙）。

### 国际站工具续聊报 `code 11155`

`code 11155`（`the reasoning content from the previous turn must be passed back in thinking mode`）在国际版（`www.workbuddy.ai`）有一个独立成因，**与 `reasoning_content` 无关**：国际站在思考模式下校验的字段名是 `reasoning`，而它自己在流式增量里下发的却是 `reasoning_content`——两者同名不同向。

触发条件是「思考模式 + 请求带 `tools` + 最后一条消息不是 `user`（即续接一次未完成的回合）」，且最后一条 `user` 之后的第一条纯文本 assistant 消息（不带 `tool_calls`）缺 `reasoning` 或为空。最常见的就是工具回合续聊：`文本 assistant → tool_calls assistant → tool`。此时整请求会被上游拒绝。

网关会自动为那一条消息补上 `reasoning`（有真实 `reasoning_content` 就镜像过去，否则用单个空格占位），其它消息一律不动。用 `CB_GATEWAY_REASONING_PASSTHROUGH=off` 可关闭这项改写。

### 同一个模型，国际号和国内号计费不一样

国内版与国际版分开计费，而**「免费」是「模型 × 站点」的属性，不是站点的属性**：

| 模型 | 国际版 | 国内版 |
| --- | --- | --- |
| `deepseek-v4.1-flash` | 免费（实测 1750 次全部 0 扣费） | 收费（实测 497 次里 485 次扣费） |
| `glm-5.3` | 收费 | — |

所以默认的站点偏好是 **`auto`**：从历史请求日志里统计每个模型在两个站点的实际扣费，自动优先「不花钱 / 单次更便宜」的那一边。判定顺序是「收过费的次数少」优先，再比平均单次扣费；两边计费一样就不区分站点（免得白白损失负载均衡）；样本不足 5 次时也先不区分，等数据够了再定。

要手工指定，到「模型配置」→**站点偏好**，给模型选「国际版优先」「国内版优先」「自动」或「不区分」，也可以只设一个默认值给没单独配置的模型用。

这是纯优先级：偏好那一边没有可用账号、或者账号都试过了，会自动退回另一边，不会让请求无账号可用。

账号页每个账号名下方会标出它的站点（`国际版 · www.workbuddy.ai` / `国内版 · www.codebuddy.cn`），判定只看域名后缀。

## 从 1.4.x 升级

启动时会自动改数据库。旧 Key 视为绑在 `workbuddy` 上，原来的 `auto` / `glm-5.2` 还能用。

和 1.4 不同的地方：启动不再自动导入账号；空仓是 503 而不是普通 `server_error`；新建 Key 必须选通道；官方余额只显示积分，不把各厂数字加在一起。

## 客户端接入

| 字段 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | 管理页创建，已绑定通道；可选再绑定到某个账号 |
| 模型 | WorkBuddy：`auto` / `glm-5.2`。QClaw：`auto` 或 `qclaw/default`。QwenWork：`auto` 或 `qwork-advanced`。TraeWork：`auto` 或 `qwen-3.7-plus` |
| Stream | 建议开 |

接口：`/v1/chat/completions`、`/v1/responses`、`/v1/models`。没加前缀的 `auto` 走这把 Key 绑定的通道。Codex 用 Responses 接口；管理页选 Codex 类型的 Key 会按 Codex 特征 prompt 做清洗（其它客户端借用这把 Key、但没有 Codex 特征时不改写）。

### 模型容量与自动发现

「一键读取供应模型」会保存上游提供的输入/输出容量。`/v1/models` 返回 `context_window` 和 `max_output_tokens`；WorkBuddy 的 `maxInputTokens` / `maxOutputTokens` 会映射为这两个字段，不把官方客户端的 `contextWindow.defaultLength` 当成模型最大容量。

各通道缺失的容量字段分别默认显示 262,144 上下文 / 32,768 输出，`capacity_source` 标记每个字段来自 `catalog` 还是 `fallback`。默认值是配置兜底，不是上游保证；同名模型在不同通道的限制也可能不同。仅当目录中有已知输出限制时，网关会把显式的 `max_tokens` / `max_completion_tokens` 裁剪到该限制（Responses 的 `max_output_tokens` 经转换后同样适用）。未指定输出预算的请求不注入预算，未知容量不用于强制裁剪。达到输出上限仍可能以 `length` 结束，容量发现不能保证长任务永不截断。

目录里带思考档位的通道（目前只有 WorkBuddy）会把上游的 `supportsReasoning`、`reasoning.supportedEfforts`、默认档和 `canDisableThinking` 一并写进 `/v1/models`，裸 id 与 `workbuddy/` 前缀都带。上游没给档位列表就不编——QClaw、QwenWork、TraeWork 只按各自协议处理思考，不在模型列表里发明档位。部署后需要再点一次 WorkBuddy 的「一键读取供应模型」，旧目录里没有这些字段。

### 失败分类

请求日志的 `finish_reason` 对上游失败分为四类：`upstream_http`（上游返回了 HTTP 错误）、`upstream_disconnect`（连接被断开）、`incomplete_stream`（流提前结束，缺少 `[DONE]` 或 `finish_reason`）、`parse_error`（SSE 事件解析不了）。`error_msg` 带 `[类别]` 前缀；空正文写成 `upstream HTTP 502, empty body`，httpx 异常没有文案时保留异常类型名。`/v1/responses` 的 `error.code` 同样用这些类别。换号重试仍记 `retry`，正文里带类别前缀。日志筛选里的「错误」按 `status_code` 判定，中间重试行照常能查到。

### 思考强度

智能体可以在 Chat Completions 中发送顶层 `reasoning_effort`，在 Responses 中发送标准的 `reasoning: {"effort": "high"}`。网关也兼容 OpenCode、DSH、Cherry 和 Claude 风格的 `reasoning.effort`、`reasoningEffort`、`thinking.type`、`thinking.effort`、`output_config.effort`、`enable_thinking` 等写法。可用档位为 `none`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`、`ultra`；`off` 等同于 `none`。

```json
{
  "model": "deepseek-v4-pro",
  "messages": [{"role": "user", "content": "分析这个问题"}],
  "reasoning_effort": "high"
}
```

| 通道 | 实际能力 |
|---|---|
| WorkBuddy | DeepSeek V4 Pro/Flash 支持 `low` / `high` / `max`，标准档位会投影到这三档；未指定时默认 `high`，可用 `CB_GATEWAY_DEFAULT_REASONING_EFFORT=off` 关闭默认 |
| QClaw | 统一转换成 `reasoning_effort` 后透传；具体档位是否生效由所选上游模型决定，不额外注入默认值 |
| QwenWork | 协议只有 `is_reasoning` 开关；`none` 关闭，其它显式档位开启，无法区分多档强度 |
| TraeWork | 当前会话协议没有可验证的思考控制字段，因此暂不支持调档 |

Chat 流会保留 `reasoning_content`。Responses 流会转换成标准的 `response.reasoning_summary_*` 事件，仅有推理、没有最终正文的有效响应也会正常完成。

OpenCode 示例（WorkBuddy Key）：

```json
{
  "provider": {
    "workbuddy": {
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "sk-cb-你的key"
      },
      "models": {
        "auto": { "name": "WorkBuddy Auto" },
        "glm-5.2": { "name": "GLM-5.2" }
      }
    }
  }
}
```

```powershell
opencode run -m workbuddy/auto "你好"
```

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-cb-你的key" \
  -d '{"model":"auto","messages":[{"role":"user","content":"你好"}]}'
```

QwenWork、QClaw、TraeWork 各用自己那把 Key，不要混用。

## 启动参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 监听地址，本机用保持这个值 |
| `--port` | `8787` | 端口 |
| `--admin-token` | 不设置 | 本机默认自动授权；远程监听必须配置管理凭证 |
| `--no-admin-auth` | 关 | 显式启用本机自动授权，仍校验请求来源；只允许回环监听 |
| `--no-browser` | 关 | 不自动打开浏览器，适合后台服务 |

## 环境变量

| 变量 | 说明 |
|---|---|
| `CB_GATEWAY_PROVIDERS` | 启用哪些通道，逗号分隔。默认 `workbuddy,qclaw,qwenwork,traework`。只想留一家时再改 |
| `CB_GATEWAY_AUTO_IMPORT` | 设 `1` 则启动时自动导入。默认 `0` |
| `CB_GATEWAY_CHECKIN_GAP_MS` | 一键领取间隔，默认 `800` |
| `CB_GATEWAY_ROUTE_WINDOW_SECONDS` | 选路负载统计窗口，默认 `900`（15 分钟）。窗口内服务请求少的账号先用 |
| `CB_GATEWAY_RATE_LIMIT_COOLDOWN_SECONDS` | 账号被上游限额（429）后在该模型上的冷却时长，默认 `900`。按「账号 × 模型」记，不影响该账号服务其它模型 |
| `CB_GATEWAY_MAX_ACCOUNT_ATTEMPTS` | 一次请求最多换几个账号重试，默认 `8`。必须大于账号数，否则健康账号可能轮不到 |
| `CB_GATEWAY_REASONING_PASSTHROUGH` | 设为 `off` 可关闭对历史 assistant 消息的推理字段改写（`reasoning_content` 占位与 `reasoning` 补齐） |
| `CB_GATEWAY_DEFAULT_REASONING_EFFORT` | WorkBuddy DeepSeek V4 Pro/Flash 的默认思考强度，支持 `low` / `high` / `max`，默认 `high`；设为 `off` 可关闭默认值。Responses 的 `reasoning.effort` 或 Chat Completions 的 `reasoning_effort` 会覆盖它 |
| `CB_AUTH_DIR` | WorkBuddy 登录目录 |
| `CB_QCLAW_AUTH_DIR` | QClaw 登录目录 |
| `CB_QWENWORK_AUTH_DIR` | QwenWork 登录目录 |
| `CB_TRAEWORK_AUTH_DIR` | TraeWork `storage.json` 所在目录 |
| `CB_TRAEWORK_OS_INFO` | TraeWork 刷新时上报的 `OSInfo`。默认 Windows `windows`、macOS `mac`、Linux `linux` |
| `CB_TRAEWORK_DEVICE_NAME` | TraeWork 刷新时上报的设备名，默认取本机主机名/用户名 |
| `CB_HOST_AUTH_DIR` | Docker 脚本用的本机 WorkBuddy 目录 |
| `CB_GATEWAY_ADMIN_TOKEN` | 固定管理 Token |
| `CB_GATEWAY_DB_PATH` | 数据库路径 |
| `CB_GATEWAY_MASTER_KEY` | 跨系统搬数据库时的加密主密钥 |
| `CB_GATEWAY_LOG_RETENTION_DAYS` | 日志保留天数，默认 `90` |
| `CB_GATEWAY_USER_AGENT` | 只影响 WorkBuddy 出站头，默认 `CLI/2.109.2 CodeBuddy/2.109.2` |

## 数据和安全

- 账号 Token 写入前会加密。Windows 用系统 DPAPI。
- 不要把 `*.db`、登录目录、日志、带 Key 的截图发出去。
- 不要把服务绑到公网。保持 `127.0.0.1`。

## License

MIT
