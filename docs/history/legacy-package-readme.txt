# Plux Python 框架

2026-10-08：Plux 0.1 已完成方案中的 **F1–F5**。框架包为 `plux`，公共入口为 `plux.api`，分发包名为 `plux-framework`；原生后端继续使用 IRIS。框架提供数据、消息、任务与插件运行能力，帮助、计算器和持久计数以独立插件验证接入。

当前使用说明与边界见 [实现与运行指南](docs/implementation.md)、[配置说明](config/README.md) 和 [独立样板插件](../plugins/examples/README.md)。设计依据保留在 [框架方案](docs/framework-plan.md)、[公共 API 设计](docs/framework-api.md)、[架构约束](docs/architecture.md) 与 [当前目录](docs/structure.md)。实际公共契约见 [plux.api 源码](src/plux/api/__init__.py)。

框架运行时仅依赖 Python >=3.11 标准库。使用仓库现有虚拟环境：

```powershell
& '.\.venv\Scripts\python.exe' -m pip install --no-deps -e '.\refactored\python' -e '.\refactored\plugins\examples'
& '.\.venv\Scripts\plux.exe' --config '.\refactored\python\config\platform\plux.example.toml' check
& '.\.venv\Scripts\plux.exe' --config '.\refactored\python\config\platform\plux.example.toml' run --once
```

也可使用 [Start-Plux.ps1](../tools/Start-Plux.ps1) 从源码运行。示例默认关闭原生连接与发送，所有路径按配置文件目录解析。修仙业务（F6）及旧生产数据处理（F7）尚未迁入；AI、记忆与广播旧实现不进入新框架。