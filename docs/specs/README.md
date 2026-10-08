<div align="center">

# 📑 协议与契约规范

[![Plugin API](https://img.shields.io/badge/Plugin%20API-0.1-blue.svg?style=flat-square)](#)
[![Native Protocol](https://img.shields.io/badge/Protocol-v1-orange.svg?style=flat-square)](#)
[![Event Schema](https://img.shields.io/badge/Event%20Schema-v2-green.svg?style=flat-square)](#)

<p align="center">
  插件、部署者、原生后端三侧的对外承诺。实现改动必须同步更新这里。
</p>

</div>

---

## 📚 规范清单

| 规范 | 约束的边界 | 核心内容 |
| :--- | :--- | :--- |
| 📐 [**公共 API 规范**](plugin-api-spec.md) | 插件 ⇄ 框架 | 模型字段、服务端口、上下文语义、错误分类、登记声明的拒绝规则 |
| ⚙️ [**配置规范**](configuration-spec.md) | 部署者 ⇄ 框架 | TOML 全字段、路径推导与校验、环境变量矩阵、常见配置错误 |
| 🔌 [**原生协议规范**](native-protocol-spec.md) | 框架 ⇄ IRIS 后端 | 组帧、命令字段、响应形态、错误码全表、事件日志字段、媒体契约 |

---

## 🔢 三套版本互不替代

| 契约 | 当前值 | 校验点 |
| :--- | :--- | :--- |
| 公共 API | `API_VERSION = "0.1"` | 插件 manifest 的 `api` 不匹配即拒绝加载 |
| 配置格式 | 无独立版本号 | 未知字段一律拒绝，所以新增字段向后兼容、改语义不兼容 |
| 原生 wire | `protocol_version = 1` | 不匹配时后端返回 `unsupported_protocol` |
| 事件日志 | `schema_version = 2` | 不匹配记为 `unsupported_schema`，留档但不产生消息 |

插件版本、内容版本、快照结构版本、数据库迁移版本同样各自独立，**不互相推导**。

---

## 🎯 权威来源

| 规范 | 权威实现 |
| :--- | :--- |
| 公共 API | `python/src/plux/api/__init__.py` 的导出清单 |
| 配置 | `python/src/plux/runtime/config.py` |
| 原生协议 | `python/src/plux/adapters/wechat_observer/` |

事件字段的权威描述在 C++ 后端的 `docs/specs/event-protocol-spec.md` 与 `command-protocol-spec.md`。本目录只描述 Plux **实际读取与发送**的部分，不复制后端全部字段。

---

## 🔗 相关文档

| 文档 | 说明 |
| :--- | :--- |
| 📘 [插件开发参考](../guide/plugin-development.md) | 契约的实战用法与常见错误 |
| 📗 [快速上手](../guide/getting-started.md) | 从零跑通并接入 IRIS |
| 💡 [内部实现](../architecture-internals.md) | 规范背后的实现原理与设计取舍 |
| 🧯 [疑难排查](../troubleshooting.md) | 违反契约时的具体报错与处理 |

---

## ⚠️ 发现不一致时

规范与实现不符时，按这个顺序确认，再反馈：

1. 先确认后端版本是 **4.1.13.12**（版本不符时后端起不来，行为差异无从谈起）；
2. 再核对观察日志里 `observer_start` 的会话与 `command_pipe_ready` 的 `protocol_version`；
3. 然后带上具体的请求/响应或事件样本，以及 `python -m plux --config <配置> check` 的输出。
