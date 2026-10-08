<div align="center">

# 🗄️ 历史文档

<p align="center">
  重写前的设计文档，<b>只作存档</b>，不再维护、不再校对。
</p>

</div>

---

| 文件 | 原位置 | 内容 |
| --- | --- | --- |
| `framework-plan.md` | `python/docs/` | 最初的框架方案与迁移分期（F1–F7） |
| `framework-api.md` | `python/docs/` | 公共插件 API 的设计草案 |
| `architecture.md` | `python/docs/` | 模块职责与依赖方向约束 |
| `legacy-package-readme.txt` | `python/src/plux/REAMDE.txt` | 被放在包内部的说明文件（文件名拼写有误） |

阅读时请注意：

- 这些文档里的**相对链接全部失效**（它们相对于旧的 `refactored/python` 布局），指向的 `implementation.md`、`structure.md`、`../tools/Start-Plux.ps1`、`refactored/plugins/examples` 等路径在本仓库中都不存在。
- 文中出现的包路径 `refactored/python`、`refactored/plugins/examples` 对应现在的 `python` 与 `python/plugins/examples`。
- 描述的行为与当前实现可能有出入，**以 [当前文档](../README.md) 和源码为准**。

其中仍然成立的设计意图（依赖方向、事务边界、身份与恢复原则）已经被吸收进 [内部实现](../architecture-internals.md) 与 [架构约束](../specs/plugin-api-spec.md)。
