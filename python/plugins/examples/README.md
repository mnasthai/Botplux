<div align="center">

# 🧩 Plux 独立插件样板

[![Distribution](https://img.shields.io/badge/Distribution-plux--example--plugins-blue.svg?style=flat-square)](#)
[![Import](https://img.shields.io/badge/Import-plux.api%20only-critical.svg?style=flat-square)](../../docs/specs/plugin-api-spec.md)

<p align="center">
  八个只依赖 <code>plux.api</code> 的独立插件，覆盖公共 API 的主要用法。
</p>

</div>

---

## 📚 示例清单

| 入口 | 演示的能力 |
| :--- | :--- |
| `plux_plugins.examples:HelpPlugin` | `stateless` 命令、最小插件结构 |
| `plux_plugins.examples:CalculatorPlugin` | 业务拒绝（`Outcome.rejected` + 回复）、输入约束 |
| `plux_plugins.examples:CounterPlugin` | 仓储工厂绑定事务、插件迁移、`atomic` 命令、后台任务 `work`/`commit`、内容目录版本绑定 |
| `plux_plugins.examples.echo:EchoPlugin` | 消息事件订阅、`requires_command_policy` 如何挡掉历史回放与未 @ 的群消息 |
| `plux_plugins.examples.keywords:KeywordPlugin` | 从 TOML 内容目录加载规则、`resources` 声明、启动时冻结版本 |
| `plux_plugins.examples.poll:PollPlugin` | 持久状态快照、`expected_revision` 条件更新、只读 `stateless` 命令 |
| `plux_plugins.examples.poster:PosterPlugin` | 图片资产 `stage`/`publish`、媒体回复、事务外发布 |
| `plux_plugins.examples.digest:DigestPlugin` | 定时计划 + 后台任务、`history` 查询、发送前读取当前原生会话 |

每个插件都是独立入口，由配置显式列出；`enabled = false` 时只是不加载，数据与资产都保留。

---

## 🧰 共用助手

`support.py` 汇总了回复构造的三个助手：

| 助手 | 语义 |
| :--- | :--- |
| `reply(context, ...)` | 构造 `ReplyIntent`；没有可回复的会话时返回 `None` |
| `respond(context, ...)` | 返回 `Outcome`；构造不出来时降级为 `Outcome.rejected("native_session_unavailable")` |
| `reject(context, code, text=...)` | 业务拒绝，同时尽可能把原因回给发送者 |

> [!IMPORTANT]
> 它们的存在是为了避免「handler 抛异常」。异常会被当作可重试失败，输入每个周期都会重试；环境性失败必须表达成业务拒绝。

---

## 🔗 相关文档

| 文档 | 说明 |
| :--- | :--- |
| 📘 [插件开发参考](../../../docs/guide/plugin-development.md) | 契约与常见错误的完整说明 |
| 📗 [快速上手](../../../docs/guide/getting-started.md) | 安装、配置与首次运行 |
| 📐 [公共 API 规范](../../../docs/specs/plugin-api-spec.md) | 字段级契约 |

配置见 `python/config/platform/plux.example.toml`（加载全部八个示例，发送默认关闭）。
