<div align="center">

# 🧪 构建与开发

[![Build](https://img.shields.io/badge/Build-无编译步骤-success.svg?style=flat-square)](#)
[![Tests](https://img.shields.io/badge/Tests-unittest%20·%2054%20cases-informational.svg?style=flat-square)](#3-测试)

<p align="center">
  Plux 是纯 Python 包，运行时只依赖标准库；这里覆盖环境、安装、测试与仓库现状。
</p>

</div>

---

## 1. 环境

| 需求 | 说明 |
| --- | --- |
| Python | >= 3.11（使用 `tomllib`、`X \| Y` 类型语法、`dataclass` 关键字参数） |
| 操作系统 | 开发与数据能力跨平台；**发送通道只支持 Windows**（本地命名管道） |
| 第三方依赖 | **无**。框架与示例插件都不引入运行时依赖 |
| C++ 工具链 | 只有需要跨语言联调时才需要（构建 C++ 仓库的测试宿主） |

---

## 2. 安装

```powershell
python -m venv .venv
& '.\.venv\Scripts\Activate.ps1'

python -m pip install -e python
python -m pip install -e python\plugins\examples
```

两个分发包：

| 目录 | 分发名 | 内容 |
| --- | --- | --- |
| `python/` | `plux-framework` | `plux` 包与控制台脚本 `plux` |
| `python/plugins/examples/` | `plux-example-plugins` | `plux_plugins.examples.*` |

不想安装时，把两个 `src` 目录放进 `PYTHONPATH` 也能工作：

```powershell
$env:PYTHONPATH = (Resolve-Path python\src), (Resolve-Path python\plugins\examples\src) -join ';'
python -m plux --config python\config\platform\plux.example.toml check
```

---

## 3. 测试

测试位于 `python/tests/plux/`，使用标准库 `unittest`。**必须**用 `-t` 指向同一目录，因为该目录不是包：

```powershell
$env:PYTHONPATH = (Resolve-Path python\src), (Resolve-Path python\plugins\examples\src) -join ';'
python -m unittest discover -s python\tests\plux -t python\tests\plux -p 'test_*.py'
```

单个模块：

```powershell
python -m unittest discover -s python\tests\plux -t python\tests\plux -p 'test_messaging.py'
```

### 3.1 测试模块

| 模块 | 覆盖 |
| --- | --- |
| `test_messaging.py` | 摄取、偏移与代次、去重、质量标记、引用/提醒/媒体载荷、幂等、结果分类 |
| `test_data.py` | 迁移摘要、内容目录、快照并发、资产生命周期、维护回收、备份恢复 |
| `test_runtime.py` | 装配、服务门禁、消费标记、任务状态机、监督与停机 |
| `test_application.py` | 通过公共 API 的端到端集成（使用示例插件包） |
| `test_recovery_boundaries.py` | 重启恢复、协议边界、跨模块依赖纪律、分发包声明 |
| `test_observer_ipc.py` | **跨语言**：与 C++ 测试宿主真实握手（需要宿主，默认跳过） |

### 3.2 跨语言联调

`test_observer_ipc.py` 会启动 C++ 仓库的测试宿主当命名管道靶场。没有宿主时它自动跳过；指定宿主时执行：

```powershell
$env:WECHATBOT_TEST_PIPE_HOST = '<C++ 仓库>\build\observer-cmake\Release\bin\ObserverReaderTests.exe'
python -m unittest discover -s python\tests\plux -t python\tests\plux -p 'test_observer_ipc.py'
```

也可以手动起靶场，用 `--serve-command-pipe` 固定会话为 `test-session`，15 秒后自行退出：

```powershell
& '<C++ 仓库>\build\observer-cmake\Release\bin\ObserverReaderTests.exe' --serve-command-pipe '\\.\pipe\wechatbot-test'
```

它证明的是：帧格式、hello 握手字段、会话身份、以及我们构造的每个请求都能被真实的 C++ 解析器接受。它**不证明**：Hook 偏移正确、注入成功、真实微信能收到消息。那需要真机。

### 3.3 用手写 JSONL 驱动插件

最有效的开发循环：起一个指向临时目录的运行时，先跑一轮固定 backlog 边界，再追加记录。

```python
from plux.runtime.bootstrap import Application
from plux.runtime.config import load_config

config = load_config("plux.toml")                  # observer.enabled = true
app = Application(config).start(background=False)
config.paths.observer_log.write_text("", encoding="utf-8")
app.step()                                          # 边界固定在当前（空）文件末尾
config.paths.observer_log.write_text(record + "\n", encoding="utf-8")   # 现在追加的都算实时
print(app.step())                                   # {"ingested":1,"processed":1,...}
app.stop()
```

要点：`observer_log` 文件必须先存在；`observer_start` 记录提供 `native_session`；群消息内容要带 `发送者:` 前缀。

---

## 4. 代码约定

| 约定 | 原因 |
| --- | --- |
| `plux.api` 不导入任何实现模块 | 公共入口必须与实现解耦（有测试断言） |
| 插件只导入 `plux.api` | 同一断言覆盖示例包 |
| 业务拒绝用 `Outcome`，异常留给环境问题 | 异常在处理器里意味着「下轮重试」 |
| IO 放在写事务之外 | 长文件操作不占写锁；`stage`/`publish` 会主动拒绝 |
| 数据库结构变更走 `Migration` | 摘要校验能发现被改过的历史迁移 |
| 新字段加在 dataclass 末尾或带默认值 | 保持位置参数的兼容性 |
| 时间一律带时区 | `require_utc` 会拒绝裸 `datetime` |

---

## 5. 仓库现状

本仓库是 Plux 的运行时代码加上一组示例插件，另外保留了旧实现的参考文件。

```
README.md                     入口
docs/                         文档：指南 · 规范 · 内部实现 · 排查 · 构建
python/                       框架与示例插件
plugins/                      旧 wechat_receiver 插件源码，仅作参考
```

遗留物（**不要**当作当前契约）：

| 路径 | 说明 |
| --- | --- |
| `plugins/*.py` | 旧实现，导入的是 `wechat_receiver`，Plux 不会加载 |
| `python/tests/test_*.py`（顶层 63 个，`tests/plux/` 之外） | 旧实现的测试；其中 61 个导入 `wechat_receiver` / `wechat_controller`，这两个包不在本仓库，因此无法运行 |
| `python/config.example.toml` | 旧实现的配置格式，与当前 TOML 规范完全不同 |
| `python/config/platform/plux.xiuxian.toml` | 业务包 `plux_plugins.xiuxian` 不在本仓库，仅作接入示例 |
| `python/src/plux_framework.egg-info/` | 安装残留，已被 `.gitignore` 排除 |

---

## 6. 已知的测试缺陷

> [!WARNING]
> 下面这些是**当前真实存在**的缺陷，不是「可能」。跑测试前先读一遍，避免把环境问题误判成代码回归。

诚实记录，便于后续清理：

- `test_runtime.py` 没有 `if __name__ == "__main__": unittest.main()`，直接 `python test_runtime.py` 会静默退出 0，什么都没跑。请用上文的 `unittest discover`。
- `python/tests/plux/` 没有 `__init__.py`，`unittest discover -s python\tests\plux`（不带 `-t`）会报 `Start directory is not importable`。
- `test_observer_ipc.py` 的宿主默认路径指向另一个工作区，必须用 `WECHATBOT_TEST_PIPE_HOST` 覆盖。
- 示例插件目录在框架项目内部（`python/plugins/examples`），而旧测试假设它与框架项目平级；测试里用 `_examples_root()` 兼容了两种布局。

---

## 7. 验证记录

在 Python 3.13.2 / Windows 上实测（本文档写就时的状态）：

| 项目 | 结果 |
| --- | --- |
| `python -m unittest discover ...`（54 个用例） | **全部通过** |
| `test_observer_ipc.py`（对真实 C++ 测试宿主） | **2/2 通过** |
| `plux check`（空配置、示例配置） | 通过，且不创建数据库 |
| `plux env` | 通过，输出与 C++ 侧环境变量对齐 |
| `plux run --once` + `status` | 正常启停，状态可读 |
| 八个示例插件端到端（摄取 → 路由 → 回复 → 任务 → 停机） | 全部符合预期，11 条回复入队（含 1 条图片）、2 个任务提交 |
| 文本/富文本/图片/语音载荷经真实 C++ 解析器 | 全部通过请求解码与媒体描述校验 |

**没有做**：注入真实微信、用真实账号收发。因此 Hook 行为、真实送达、真实媒体格式兼容性都仍未验证。
