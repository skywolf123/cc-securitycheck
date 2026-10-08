# cc-securitycheck

**让 Claude Code 在非原生模型上也能用好 auto mode。**

Claude Code 的安全分类器（auto 模式每次审批有副作用工具前的那次独立调用）走的是
Anthropic 路径。换成 DeepSeek、GLM、MiniMax 等非原生模型后，分类器几乎必然
**fail-closed** —— 拦截所有有副作用的工具（Bash / Write / Edit / Agent），并抛出一句
误导性错误：

```
... is temporarily unavailable, so auto mode cannot determine the safety of ...
```

结果是 auto 模式**基本不可用**：每个命令都要你手动批准，或者被迫关掉 auto。
本中转把这个体验修好 —— 装上之后 auto 模式在非原生模型上**与原生一样顺**。

## 一句话原理

CC 的分类器调用只求一个 `<block>yes|no</block>` 结论，但非原生模型是推理模型，
会把推理写在结论前面；一旦输出被截断或结论前有杂质，CC 就解析不到 verdict → fail-closed。

本中转对**分类器请求**做两件事（其余请求原样透传，思考强度完全跟随 CC 的 effort 配置）：

1. **去掉 `max_tokens`** —— 不再限制输出长度，让模型推完再出结论，从根上消除截断；
2. **补 `thinking: {"type": "disabled"}`** —— 能识别该字段的上游可跳过推理，直接出结论。

再对**分类器响应**做一件事：

3. **截取 `<block>` 及之后的内容** —— 丢掉模型写在 verdict 前面的推理/解释，
   让 CC 拿到的永远是「以 `<block>` 开头」的干净文本。

> **关键：不换端点、不做协议转换。** 三步都作用在 Anthropic `/v1/messages` 上，
> 所以不依赖上游对 OpenAI 协议的实现、也不随上游模型映射变化而失效。

## 实测效果（2026-10-08，cyzlab 网关 + GLM 后端）

| | 装本中转前 | 装后 |
|---|---|---|
| 分类器输出 | 散文 / `<block>否</block>` / 空（截断） | `<block>no</block>` |
| output tokens | 677 ~ 942，或截断到 64 | **7** |
| thinking 内容 | 有（塞满预算） | **0 字节** |
| auto 模式 | 每条命令 fail-closed | 正常放行 |

## 两条根因（同类症状，先判别再动手）

auto 模式报 `... is temporarily unavailable, so auto mode cannot determine the safety of ...`
时，背后至少有两条独立根因。**本工具只解决根因一**；根因二非本工具范围，
但了解两者差异能让你在 1 分钟内定位问题。

### 根因一：模型侧——输出被截断 / 混入推理，verdict 解析不到（本工具解决）

- 分类器 system 明确要求 `Your ENTIRE response MUST begin with <block>`，
  但推理模型会先写推理；若输出再被 `max_tokens` 截断，`<block>` 永远吐不出来。
- 分类器 **fail-closed** 拦截所有有副作用工具，并报误导性的 "temporarily unavailable"。
- **时间特征：确定性**——同一输入 100% 复现。
- **判别信号**：走本中转后日志出现 `[CLASSIFIER-TRIM] ... trimmed` 且正常出 verdict，即可证实。

### 根因二：服务端——分类器 serving 间歇性不可用（非本工具范围）

- 分类器由服务端路径承载，曾出现数小时级的**间歇性 brownout**
  （详见 anthropics/claude-code #74248；同类 issue：#49535 #67542 #68387）。
- **时间特征：间歇性 flapping**——同一命令在秒级间隔内成败交替，与命令内容无关；
  分类器调用**无客户端重试/退避**，直接 fail-closed；状态页可能无反映（sub-threshold）。
- **应对**：等待或在健康间隙操作；或临时用 `permissions.allow` 白名单绕过
  （注意：改 `settings.json` 本身也被分类器门控，可能要多试几次）。

### 一分钟判别表

| 判别维度 | 根因一（模型侧） | 根因二（服务端） |
|---|---|---|
| 时间特征 | 确定性，同一输入 100% 复现 | 间歇性，同一输入秒级成败交替 |
| 切换 `effort=medium` | 恢复 | 无影响 |
| 走本中转 | 恢复（日志显示 `trimmed`） | 无影响 |
| 错误文案 | 相同 | 相同 |

## 文件

- `proxy.py`      —— 中转本体，仅 Python 标准库，零第三方依赖
- `start.bat`     —— Windows 一键启动
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
ANTHROPIC_AUTH_TOKEN=<你的上游 API key>
ANTHROPIC_MODEL=<你的模型名>          # 或用 /model 切换
```

之后 CC 的所有请求都走本地中转。中转会转发你的 API key，因此**只监听 127.0.0.1，
不要暴露到局域网/公网**。

## 配置（环境变量，全部有默认值）

三种来源，优先级从高到低：

1. **真实环境变量** —— 临时覆盖调试用，如 `PROXY_PORT=9000 python proxy.py`
2. **同目录 `.env`** —— 复制 `.env.example` 为 `.env` 后修改（`.env` 已被 gitignore）
3. **代码内置默认值**

| 变量                          | 默认值                               | 说明 |
|-------------------------------|--------------------------------------|------|
| `PROXY_HOST`                  | `127.0.0.1`                          | 监听地址 |
| `PROXY_PORT`                  | `8008`                               | 监听端口 |
| `UPSTREAM`                    | `https://api.deepseek.com/anthropic` | 上游 Anthropic 兼容端点 |
| `MODEL_OVERRIDE`              | 空                                   | 统一改写 model 字段；留空则透传 CC 的 |
| `CLASSIFIER_CAP_MAX_TOKENS`   | `1`（开）                            | 分类器**去掉 `max_tokens`**，避免截断 |
| `CLASSIFIER_NO_THINK`         | `1`（开）                            | 分类器补 `thinking:{"type":"disabled"}` |

两个分类器开关都可独立置 `0`/`false`/`off` 关闭，用于对照排查。

> `.env` 由 `proxy.py` 用标准库自行读取（零依赖），路径取脚本所在目录，与启动时的
> cwd 无关；已存在的环境变量不会被 `.env` 覆盖。
>
> 注意：这里的 `.env` 只服务 `proxy.py`。CC 自己的 `ANTHROPIC_*` 变量由 CC 进程读取，
> 需配在 `~/.claude/settings.json` 的 `env` 字段或启动 CC 前的 shell 环境里。

## 分类器是怎么被认出来的

判定只看 **system 提示里的两条独有标记**（大小写不敏感，命中任一即可）：

| 标记 | 来源 |
|---|---|
| `you are a security monitor` | 分类器 system 首句 |
| `must begin with <block>`     | 分类器的输出契约 |

**不看 `max_tokens`、也不看 `severity` 之类的泛词** —— 泛词会被项目自身的
`CLAUDE.md` 内容误伤，而 `max_tokens <= N` 这种阈值判定又极易把正常的小请求
误当成分类器（静默改写正常请求，比漏判更难排查）。

> 取舍：**宁可漏判，不可误判**。漏判只让分类器回到 fail-closed（报错显眼、已知）；
> 误判则会把改写施加到正常请求上（静默改坏数据）。
>
> 若 CC 将来改了 prompt 措辞导致漏判，日志里会显示 `命中=?` —— 一眼可查，
> 更新上面的标记即可。

## 验证

启动后每笔**分类器**请求打三行日志（其余请求不打日志）：

```
[2026-10-08 22:06:00.545] [CLASSIFIER-IN] model=glm-5.3-flash max_tokens=2112 stream=False 命中=you are a security monitor thinking=<未设置>
[2026-10-08 22:06:10.852] [CLASSIFIER-TRIM] verdict 位于第 0 块，丢 0 字前缀，保留 17B：块[text] 正文17B think0B out=7 stop=end_turn
[2026-10-08 22:06:10.852] [CLASSIFIER-OUT] -> 200 trimmed 块[text] 正文17B think0B out=7 stop=end_turn
```

| 字段 | 含义 |
|---|---|
| `命中=` | 命中了哪条标记（`?` = 一条都没中，判定可能已失效） |
| `thinking=` | CC **原样**发来的 thinking 字段（`<未设置>` = CC 没带，需中转补） |
| `块[...]` | 改写前响应里的 block 类型；出现 `thinking` 说明模型没听话、输出了推理 |
| `think…B` | thinking 块内容字节数（0 = 未输出推理） |
| `out=…` / `stop=…` | 上游报的 token 数与结束原因（`stop=max_tokens` = 又被截断了） |
| `最后一段` | `trimmed` / `verdict=MISSING` / `pass-through(非JSON)` |

三种结果的含义：

- **`trimmed`** —— 正常，verdict 已截取。
- **`verdict=MISSING`** —— 响应里找不到 `<block>`，**原样放行**交给 CC（fail-closed，
  不比不透传更差）。频繁出现说明模型没按契约输出，或标记词已过期。
- **`pass-through(非JSON)`** —— 上游返回了非 JSON（网关偶发空 body），原样透传。

## 边界与已知限制

- **只处理非流式分类器响应**（实测 CC 的分类器调用 `stream=false`）。
  若将来变成流式，中转会走透传，不截取。
- **只对分类器改写**。主模型请求一律原样透传 —— 中转**不碰**你的思考强度配置。
- **不解决根因二**（服务端 brownout），那种情况等或加白名单。
- 上游若无视 `thinking:disabled`（`think…B > 0` 可见），**不影响正确性** ——
  响应侧的截取仍然生效，只是没能省下推理的开销。

## 模型说明

- 模型名透传不改，由 CC 的 `ANTHROPIC_MODEL` / `/model` 决定；也可用 `MODEL_OVERRIDE`
  在代理侧强制统一。
- 上游若是「单模型名多链路」网关，同一请求名可能被路由到不同后端 —— 日志里的
  `served=`（早期版本）与 `块[...]` 可帮你判断实际行为是否稳定。

