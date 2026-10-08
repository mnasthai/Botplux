<div align="center">

# ⚙️ 配置规范

[![Format](https://img.shields.io/badge/Format-TOML-blue.svg?style=flat-square)](#)
[![Validation](https://img.shields.io/badge/Validation-未知字段即拒绝-critical.svg?style=flat-square)](#9-常见配置错误)

<p align="center">
  配置错误永远在启动前暴露，不会留到运行期。
</p>

</div>

---

## 1. 顶层结构

```toml
[paths]                    # 目录与文件位置
[observer]                 # 与 IRIS 原生后端的连接与发送开关
[policy]                   # 账号、目标与门禁策略
[runtime]                  # 调度节奏与停机预算
[[plugins]]                # 插件入口（可重复）
```

---

## 2. `[paths]`

全部可省略，默认值由 `runtime_root` 推导。相对路径以 **TOML 文件所在目录** 为基准。

| 键 | 默认值 | 类型 | 说明 |
| --- | --- | --- | --- |
| `runtime_root` | `runtime` | 目录 | 运行数据根 |
| `database` | `<root>/plux/platform.sqlite3` | 文件 | 主数据库 |
| `observer_log` | `<root>/observer-<target_version>.jsonl` | 文件 | 收信观察日志（C++ 侧写入） |
| `inbound` | `<root>/media/inbound` | 目录 | 收到的媒体 |
| `outbound` | `<root>/media/outbound` | 目录 | 待发媒体（同时是原生媒体根） |
| `staging` | `<root>/plux/staging` | 目录 | 未发布资产 |
| `cache` | `<root>/plux/cache` | 目录 | 缓存 |

校验规则：

- 拒绝盘符相对路径（`C:foo`）、设备路径前缀（`\\?\`、`\\.\`）、Windows 非法字符、保留设备名（`CON`、`NUL`、`COM1`…）以及以空格或点结尾的路径段。
- `database` 与 `observer_log` 不能是同一个文件。
- `inbound`、`outbound`、`staging`、`cache` 四个目录必须互不相同，且不能互相包含。
- `database` / `observer_log` 不能是这些目录本身或它们的祖先。
- 已存在的路径类型必须匹配：那两个必须是文件，其余必须是目录。

启动时 `create()` 会创建数据库父目录、`runtime_root`、`inbound`、`outbound`、`staging`、`cache`；**观察日志不会被创建**，它由 C++ 侧写入。

---

## 3. `[observer]`

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 是否读取观察日志并允许使用命令管道 |
| `send_enabled` | `false` | 是否投递回复 |
| `media_send_enabled` | `false` | 是否允许图片/语音回复 |
| `quote_enabled` | `false` | 是否允许引用回复 |
| `pipe` | 未设置 | 固定命令管道路径；不设置时从 `command_pipe_ready` 事件发现 |
| `expected_session` | 未设置 | 固定观察会话，用于拒绝跨运行实例的请求 |
| `target_version` | `"4.1.13.12"` | 目标后端版本，**只接受这一个值** |
| `timeout_seconds` | `5.0` | 单次管道交换的总超时 |

依赖关系（违反即配置错误）：

- `send_enabled` / `media_send_enabled` / `quote_enabled` 任一为真都要求 `enabled = true`；
- `media_send_enabled` / `quote_enabled` 还要求 `send_enabled = true`；
- `enabled = true` 时 `policy.account` 与 `policy.allowed_targets` 都不能为空；
- `pipe` 必须是 `\\.\pipe\` 下的单个本地组件，不含 `/` 与 `..`，长度 ≤ 256。

---

## 4. `[policy]`

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `account` | `""` | 本机登录账号（内部 ID，精确匹配） |
| `allowed_targets` | `[]` | 允许回复的会话白名单；**留空等于什么都发不出去** |
| `allowed_actors` | `[]` | 允许触发处理器的发送者白名单；留空表示不限制 |
| `group_requires_mention` | `true` | 群聊必须显式 @ 本账号才执行命令与受策略约束的事件 |
| `allow_unknown_history` | `false` | 允许在「无法证明消息是实时」的情况下执行命令 |

`account` 与三个列表的元素都必须匹配 `[A-Za-z0-9_.@-]{1,256}`，列表内不允许重复。目标会话额外允许 `@chatroom` 后缀。

关于 `allow_unknown_history`：事件 schema 2 不携带「这条消息是实时还是历史」的证明，因此默认关闭命令执行。只有在你确认日志的写入时序可信时才打开它。

---

## 5. `[runtime]`

| 键 | 默认值 | 约束 |
| --- | --- | --- |
| `poll_seconds` | `0.2` | 有限正数 |
| `stop_timeout_seconds` | `10.0` | 有限正数 |
| `batch_size` | `100` | 1..10000 的整数，单轮最多处理的记录/消息数 |

`poll_seconds` 同时是调度周期与回复投递周期：一轮最多投递一条回复。

---

## 6. `[[plugins]]`

```toml
[[plugins]]
entrypoint = "mybot.greeting:GreetingPlugin"
enabled = true
config = { maximum = 1000000, conversation = "room@chatroom" }
```

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `entrypoint` | 必填 | `module:Class`，模块点分、类名单个标识符；必须是 `Plugin` 子类且带类级 `PluginManifest` |
| `enabled` | `true` | 为假时不加载，但数据保留 |
| `config` | `{}` | 交给插件的 `config_validator`；结果冻结后通过 `services.config` 提供 |

顺序即加载顺序（依赖关系会再做拓扑排序）。停用插件不会删除它的表、快照或资产。

> [!WARNING]
> **TOML 细节**：`[plugins.config]` 子表形式只作用于**最后一个** `[[plugins]]` 元素。多个插件都要配置时，用内联表 `config = { ... }` 更不容易出错。

---

## 7. 环境变量导出

```powershell
plux --config <配置> env
```

输出（供 C++ 注入器 / 启动器使用）：

| 变量 | 来源 |
| --- | --- |
| `WECHATBOT_RUNTIME_ROOT` | `paths.runtime_root` |
| `WECHATBOT_MEDIA_ROOT` | `paths.outbound` |
| `WECHATBOT_MEDIA_INBOUND_ROOT` | `paths.inbound` |
| `WECHATBOT_COMMAND_PIPE` | `observer.enabled` → `1`/`0` |
| `WECHATBOT_NATIVE_SEND` | `observer.send_enabled` |
| `WECHATBOT_MEDIA_SEND` | `observer.media_send_enabled` |
| `WECHATBOT_EXPERIMENTAL_QUOTE` | `observer.quote_enabled` |
| `WECHATBOT_SEND_MODE` | 固定 `continuous` |
| `WECHATBOT_SEND_ACCOUNT` | `policy.account` |
| `WECHATBOT_SEND_TARGETS` | `policy.allowed_targets`，用 `;` 连接 |

前提：`observer_log` 必须恰好等于 `<runtime_root>/observer-<target_version>.jsonl`，否则 `env` 直接报错。这条限制是刻意的——原生侧把日志文件名写死在这个位置，两边不一致会导致最隐蔽的一类「收不到消息」。

---

## 8. 随仓库提供的配置

| 文件 | 用途 |
| --- | --- |
| `python/config/platform/plux.empty.toml` | 空框架：无插件、无原生连接。用于验证安装 |
| `python/config/platform/plux.example.toml` | 加载全部八个示例插件，发送关闭 |
| `python/config/platform/plux.xiuxian.toml` | 业务插件（`plux_plugins.xiuxian`）接入示例，该包不在本仓库 |

`python/config.example.toml` 是旧 `wechat_receiver` 实现的遗留物，字段与本规范完全不同，**不要使用**。

---

## 9. 常见配置错误

| 报错 | 原因 |
| --- | --- |
| `config: unknown fields xxx` | 拼错或多余的键 |
| `observer.target_version: only 4.1.13.12 is supported` | 写了别的版本 |
| `sending requires observer.enabled; media/quote sending also requires send_enabled` | 开关之间的依赖没满足 |
| `enabled observer requires an account and explicit allowed_targets` | 开了原生连接却没填账号或白名单 |
| `media, staging and cache roots must be separate` | 目录互相包含或重复 |
| `paths.database: wrong filesystem type` | 该是文件的位置存在同名目录（或反之） |
| `IRIS fixes its log filename under runtime_root` | 自定义了 `observer_log` 却想导出环境变量 |
| `plugins[0].entrypoint must be module:Class` | 入口格式不对 |
