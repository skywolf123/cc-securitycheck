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

## 文件

- `proxy.py`   —— 中转本体，仅 Python 标准库，零第三方依赖
- `start.bat`  —— Windows 一键启动，顶部是常用配置区

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

| 变量            | 默认值                                  | 说明                                   |
|-----------------|-----------------------------------------|----------------------------------------|
| `PROXY_HOST`    | `127.0.0.1`                             | 监听地址                               |
| `PROXY_PORT`    | `8008`                                  | 监听端口                               |
| `UPSTREAM`      | `https://api.deepseek.com/anthropic`    | DeepSeek Anthropic 兼容端点            |
| `MODEL_OVERRIDE`| 空                                      | 统一改写 model 字段；留空则透传 CC 的   |

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
