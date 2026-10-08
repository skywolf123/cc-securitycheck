# cc-securitycheck

本地 DeepSeek 中转，解决 Claude Code **auto 模式安全分类器被拦**的问题。

## 背景

DeepSeek（V4）是推理模型，其 Anthropic 兼容层**默认开 thinking 且 thinking 消耗 max_tokens 预算**，
与 Anthropic「不写 thinking 字段 = 不推理、预算分离」的语义不同。

auto 模式的安全分类器调用只给 64/256 token，effort=high 时 thinking 把预算烧光，
吐不出 `<severity>` 结论，分类器 **fail-closed** 拦截所有有副作用的工具（Bash/Write/Edit/Agent），
报误导性错误：`... is temporarily unavailable, so auto mode cannot determine the safety of ...`。

本中转把**分类器请求的 thinking 强制关掉**，其余请求**原样透传**——思考强度完全跟随
Claude Code 内部的 effort 配置（high 也可用了）。

## 两条根因（同类症状，先判别再动手）

auto 模式报 `... is temporarily unavailable, so auto mode cannot determine the safety of ...`
时，背后至少有两条独立根因。**本工具只解决根因一**；根因二非本工具范围，
但了解两者差异能让你在 1 分钟内定位问题。

### 根因一：模型侧——推理模型把分类器的 token 预算全烧在 thinking 上（本工具解决）

- auto 模式的安全分类器是每次审批有副作用工具前的**一次独立 LLM 调用**，
  `max_tokens` 只有 64/256，system 要求输出 `<severity>N</severity>`。
- DeepSeek（V4）是推理模型，其 Anthropic 兼容层**默认开 thinking，且 thinking 消耗 max_tokens 预算**——
  与 Anthropic「不写 thinking 字段 = 不推理、预算分离」的语义不同。
- 因此 `effort=high` 时 thinking 把 64/256 token 全烧光，`<severity>` 结论永远吐不出，
  分类器 **fail-closed** 拦截所有有副作用工具，并报误导性的 "temporarily unavailable"。
- **时间特征：确定性**——同一输入 100% 复现，直到 effort/thinking 改变。
- **判别信号**：切 `effort=medium`/`low` 立即恢复，即此根因；走本中转后日志出现
  `[CLASSIFIER->NO_THINK]` 且正常出 verdict，亦可证实。

### 根因二：服务端——分类器 serving 间歇性不可用（非本工具范围）

- 分类器由 `claude-opus-4-8[1m]` 之类的服务端路径承载，曾出现数小时级的**间歇性 brownout**
  （详见 anthropics/claude-code #74248；同类 issue：#49535 #67542 #68387）。
- **时间特征：间歇性 flapping**——同一命令在秒级间隔内成败交替，与命令内容无关；
  分类器调用**无客户端重试/退避**，直接 fail-closed；状态页可能无反映（sub-threshold）。
- **应对**：等待或在健康间隙操作；或临时用 `permissions.allow` 白名单绕过
  （注意：改 `settings.json` 本身也被分类器门控，可能要多试几次）。

### 一分钟判别表

| 判别维度 | 根因一（模型侧） | 根因二（服务端） |
|---|---|---|
| 时间特征 | 确定性，同一输入 100% 复现 | 间歇性，同一输入秒级成败交替 |
| 切换 `effort=medium` | 立即恢复 | 无影响 |
| 走本中转 | 恢复（`CLASSIFIER->NO_THINK`） | 无影响 |
| 错误文案 | 相同 | 相同 |

## 文件

- `proxy.py`      —— 中转本体，仅 Python 标准库，零第三方依赖
- `start.bat`     —— Windows 一键启动，顶部是常用配置区
- `.env.example`  —— 配置示例，复制为 `.env` 即可生效

## 使用

### 1. 启动中转

```bash
python proxy.py
```

或双击 `start.bat`。默认监听 `http://127.0.0.1:8008`。

### 2. 配置 Claude Code，把流量指向本地中转

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8008
ANTHROPIC_AUTH_TOKEN=<你的 DeepSeek API key>
ANTHROPIC_MODEL=deepseek-v4-flash[1M]   # 或用 /model 切换
```

之后 CC 的所有请求都走本地中转。中转会转发你的 API key，因此**只监听 127.0.0.1，
不要暴露到局域网/公网**。

## 配置（环境变量，全部有默认值）

三种来源，优先级从高到低：

1. **真实环境变量** —— 临时覆盖调试用，如 `PROXY_PORT=9000 python proxy.py`
2. **`start.bat` 顶部配置区** —— Windows 双击启动时生效
3. **同目录 `.env`** —— 复制 `.env.example` 为 `.env` 后修改（`.env` 已被 gitignore）

| 变量            | 默认值                                  | 说明                                   |
|-----------------|-----------------------------------------|----------------------------------------|
| `PROXY_HOST`    | `127.0.0.1`                             | 监听地址                               |
| `PROXY_PORT`    | `8008`                                  | 监听端口                               |
| `UPSTREAM`      | `https://api.deepseek.com/anthropic`    | DeepSeek Anthropic 兼容端点            |
| `MODEL_OVERRIDE`| 空                                      | 统一改写 model 字段；留空则透传 CC 的   |

> `.env` 由 `proxy.py` 用标准库自行读取（零依赖），路径取脚本所在目录，与启动时的
> cwd 无关；已存在的环境变量不会被 `.env` 覆盖。
>
> 注意：这里的 `.env` 只服务 `proxy.py`。CC 自己的 `ANTHROPIC_*` 变量由 CC 进程读取，
> 需配在 `~/.claude/settings.json` 的 `env` 字段或启动 CC 前的 shell 环境里。

## 行为

- **安全分类器请求**（`/v1/messages` 且 system 含 `severity` **或** `max_tokens <= 256`）
  → 注入 `thinking: {"type": "disabled"}`，不推理、直接出 verdict。
- **其余请求** → 原样透传，thinking 跟随 CC 的 effort 配置。

## 验证

启动后每笔请求打一行日志：

```
[CLASSIFIER->NO_THINK] POST /v1/messages -> 200
[PASSTHROUGH] POST /v1/messages -> 200
```

分类器请求应显示 `CLASSIFIER->NO_THINK` 并正常返回 verdict；主模型调用显示 `PASSTHROUGH`。

## 模型说明

- DeepSeek **flash 已更新到 0731 版本，当前能力反而强于 pro**，默认推荐 flash。
- 模型名透传不改，由 CC 的 `ANTHROPIC_MODEL` / `/model` 决定；也可用 `MODEL_OVERRIDE`
  在代理侧强制统一（如切换 flash/pro 时不用动 CC 配置）。
