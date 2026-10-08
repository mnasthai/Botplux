<div align="center">

# 📗 快速上手

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg?style=flat-square&logo=python)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/Dependencies-仅标准库-success.svg?style=flat-square)](#)

<p align="center">
  从零到「插件能回一条消息」，再接到 IRIS 原生后端。
</p>

</div>

---

## 1. 安装

Plux 只依赖 Python 标准库，但对 Python 版本有要求（>= 3.11）。

```powershell
# 建议使用虚拟环境，避免污染系统 Python
python -m venv .venv
& '.\.venv\Scripts\Activate.ps1'

python -m pip install -e python
python -m pip install -e python\plugins\examples
```

安装后有两个入口：

- 控制台脚本 `plux`
- 等价模块调用 `python -m plux`（虚拟环境未激活时用这个更稳）

> [!TIP]
> 不想安装时，把两个 `src` 目录放进 `PYTHONPATH` 也能工作：`$env:PYTHONPATH = (Resolve-Path python\src), (Resolve-Path python\plugins\examples\src) -join ';'`。两种方式在 [构建与开发](../development/building.md#2-安装) 里都验证过。

验证：

```powershell
plux --version
plux --config python\config\platform\plux.empty.toml check
```

`check` 的输出里 `valid: true` 与 `plugins: []` 表示空框架配置正确。

---

## 2. 配置

配置是 TOML，所有相对路径都以 **TOML 文件所在目录** 为基准。最小的可用配置长这样：

```toml
[paths]
runtime_root = "../../../runtime/demo"

[observer]
enabled = false

[policy]
account = ""
allowed_targets = []

[runtime]
poll_seconds = 0.2
```

派生出的目录（可直接用 `plux ... check` 查看实际值）：

| 名称 | 默认值 | 用途 |
| --- | --- | --- |
| `runtime_root` | `<配置目录>/runtime` | 运行数据根 |
| `database` | `<runtime_root>/plux/platform.sqlite3` | 主数据库 |
| `observer_log` | `<runtime_root>/observer-<版本>.jsonl` | 收信观察日志 |
| `inbound` / `outbound` | `<runtime_root>/media/{inbound,outbound}` | 收到的媒体 / 待发媒体 |
| `staging` / `cache` | `<runtime_root>/plux/{staging,cache}` | 未发布资产 / 缓存 |

以 `python\config\platform\plux.example.toml` 为起点改一份自己的配置即可。全部字段与校验规则见 [配置规范](../specs/configuration-spec.md)。

---

## 3. 空跑一轮

```powershell
plux --config python\config\platform\plux.example.toml run --once
```

输出是这一步做完的事：

```json
{ "ingested": 0, "processed": 0, "task_worked": false, "delivery": "idle" }
```

| 字段 | 含义 |
| --- | --- |
| `ingested` | 本轮从观察日志读入并落库的记录数 |
| `processed` | 本轮完成的消息数（含被策略过滤掉的） |
| `task_worked` | 本轮是否推进了一个后台任务或定时计划 |
| `delivery` | 本轮投递结果：`idle` / `accepted` / `rejected` / `unknown` / `blocked` |

此时 `runtime_root` 下会出现数据库与资产目录：

```
runtime/demo/plux/platform.sqlite3
runtime/demo/plux/platform.sqlite3.runtime.lock
```

---

## 4. 观察日志与会话

Plux 不主动连微信；它 tail 由 C++ 观察者写的 JSONL。文件本身必须存在（哪怕是空文件），框架才会去读：

```powershell
New-Item -ItemType File -Path runtime\demo\observer-4.1.13.12.jsonl -Force
```

> [!WARNING]
> 启动时日志里已有的内容会被当作 **历史**：命令与受策略约束的事件不会对它们执行（这是防止重启后把旧消息重新答一遍）。只有启动之后追加的记录才会被当作实时消息处理。

日志里两类记录很重要：

| 记录 | 作用 |
| --- | --- |
| `observer_start` | 声明本次观察会话 ID；会话变化会让旧的待发回复失效 |
| `command_pipe_ready` | 声明命令管道路径与协议版本；Plux 据此发现发送通道 |
| `command_pipe_error` / `native_sender_disabled` / `observer_disabled` | 明确告诉你原生侧不可用或未启用 |

---

## 5. 接入 IRIS

前提：C++ 后端已构建并注入微信 4.1.13.12。步骤如下。

### 5.1 对齐目录与环境变量

先打印框架认为正确的环境变量，把它们交给注入器 / 启动器：

```powershell
plux --config <你的配置> env
```

```json
{
  "WECHATBOT_RUNTIME_ROOT": "...\\runtime\\demo",
  "WECHATBOT_MEDIA_ROOT": "...\\runtime\\demo\\media\\outbound",
  "WECHATBOT_MEDIA_INBOUND_ROOT": "...\\runtime\\demo\\media\\inbound",
  "WECHATBOT_COMMAND_PIPE": "1",
  "WECHATBOT_NATIVE_SEND": "1",
  "WECHATBOT_MEDIA_SEND": "0",
  "WECHATBOT_EXPERIMENTAL_QUOTE": "0",
  "WECHATBOT_SEND_MODE": "continuous",
  "WECHATBOT_SEND_ACCOUNT": "wxid_xxx",
  "WECHATBOT_SEND_TARGETS": "friend;room@chatroom"
}
```

`observer_log` 必须恰好等于 `<runtime_root>/observer-<target_version>.jsonl`，否则 `env` 会直接报错——这是刻意的，避免两边的日志路径静默错位。

### 5.2 填账号与白名单

```toml
[observer]
enabled = true
send_enabled = true
# media_send_enabled = true      # 需要发图片/语音时
# quote_enabled = true           # 需要引用回复时

[policy]
account = "wxid_你的登录账号"       # 必须精确等于登录 wxid
allowed_targets = ["好友wxid", "群ID@chatroom"]
allowed_actors = []                # 留空表示不限制发送者
```

约束（启动时校验，违反直接失败）：

- `account` 与 `allowed_targets` 在 `observer.enabled = true` 时都不可为空；
- `media_send_enabled` / `quote_enabled` 要求 `send_enabled`；三者都要求 `enabled`；
- 只有 `allowed_targets` 里的会话能被回复，留空就是「什么都发不出去」。

### 5.3 启动

```powershell
plux --config <你的配置> run
```

运行期每秒做一轮：读日志 → 调度插件 → 推进任务 → 投递一条回复。`Ctrl+C` 触发优雅停机。

### 5.4 确认原生侧状态

```powershell
plux --config <你的配置> status
```

`status` 只读数据库。真正的连接状态在运行期由 `health()` 暴露，也可以在插件里用 `services.messages.connection()` 查询：

```python
snapshot = self.services.messages.connection()
snapshot.account          # 原生侧报告的账号
snapshot.native_session   # 本次观察会话，回复必须绑定它
snapshot.can_send         # 账号已验证 + 发送已启用 + 至少一个能力可用
snapshot.capabilities     # {"send_text", "send_group_text", "send_mention", "send_quote", "send_image", "send_voice"}
snapshot.issues           # 例如 ("account_not_verified",)
```

`can_send` 为 `False` 时投递会停在 `blocked`，回复留在队列里等下一次机会。

---

## 6. 常见第一次踩坑

| 现象 | 原因 |
| --- | --- |
| 命令没反应，`processed` 却有数字 | 消息在启动前就写在日志里，被当作历史。启动后再追加 |
| `delivery: blocked` | `can_send` 为假：账号未验证、发送未启用或 `allowed_targets` 不含目标 |
| `plux: configuration_error: ...` | 配置字段或取值越界；错误信息会指明具体位置 |
| `another Plux runtime owns ...` | 同一数据库已被另一个进程持有运行时锁 |
| 图片/语音被拒绝 | 媒体格式或时长不符合后端契约，见 [原生协议规范](../specs/native-protocol-spec.md#4-媒体契约) |

---

## 下一步

- 写自己的插件：[插件开发参考](plugin-development.md)
- 运维与备份：[运行与运维](operations.md)
- 出问题：[疑难排查](../troubleshooting.md)
