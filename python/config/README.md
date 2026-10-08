<div align="center">

# ⚙️ Plux 配置

[![Format](https://img.shields.io/badge/Format-TOML-blue.svg?style=flat-square)](#)
[![Base](https://img.shields.io/badge/相对路径-配置文件所在目录-orange.svg?style=flat-square)](#)

<p align="center">
  三份运行配置：空框架、全部示例、业务接入示例。
</p>

</div>

---

## 📂 随仓库提供的配置

| 文件 | 用途 |
| :--- | :--- |
| `platform/plux.empty.toml` | 空框架：无插件、无原生连接。用于验证安装与目录推导 |
| `platform/plux.example.toml` | 加载全部八个示例插件；原生连接与发送默认关闭 |
| `platform/plux.xiuxian.toml` | 业务插件（`plux_plugins.xiuxian`）的接入示例，该包不在本仓库 |

```powershell
plux --config config\platform\plux.example.toml check     # 只校验，不产生运行时 IO
plux --config config\platform\plux.example.toml run --once
```

---

## 🧭 四层职责

| 段 | 决定什么 |
| :--- | :--- |
| `[paths]` | 数据落在哪里（运行根、数据库、观察日志、媒体目录） |
| `[observer]` | 通不通原生：连接开关、发送开关、媒体与引用开关、管道路径 |
| `[policy]` | 谁能触发、能发给谁：账号、目标白名单、群聊 @ 要求、历史容忍度 |
| `[[plugins]]` | 加载什么：`module:Class` 入口、启用开关、插件自己的 `config` |

插件配置由插件类上的 `config_validator` 校验，校验结果通过 `services.config` 提供。

> [!WARNING]
> 上一级目录的 `python/config.example.toml` 是旧 `wechat_receiver` 实现的遗留文件，字段与当前规范无关，**不要使用**。

字段全集、校验规则、路径推导与环境变量导出见 [配置规范](../../docs/specs/configuration-spec.md)。
