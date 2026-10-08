<div align="center">

# 🧯 疑难排查

[![Approach](https://img.shields.io/badge/Approach-现象%20→%20原因%20→%20处理-orange.svg?style=flat-square)](#)
[![Evidence](https://img.shields.io/badge/Evidence-数据库%20%2B%20日志-important.svg?style=flat-square)](#0-先收集事实)

<p align="center">
  每一条都对应实现里的一个具体行为，绝大多数问题都能在状态表与日志里找到直接证据。
</p>

</div>

---

## 0. 先收集事实

> [!TIP]
> 90% 的问题在这一节就能定性：先看连接快照判断「原生侧通不通」，再看数据库状态判断「业务侧对不对」。

```powershell
plux --config <配置> status          # 队列/处理器/任务的分布
plux --config <配置> check           # 配置与插件声明是否仍然有效
```

```python
# 运行期最有用的一行：原生侧到底什么状态
print(application.observer.connection())
# ConnectionSnapshot(account=None, native_session=None, phase='disconnected',
#                    can_send=False, issues=('command_pipe_error',))
```

再翻观察日志里的控制事件：

```powershell
Select-String -Path runtime\...\observer-4.1.13.12.jsonl -Pattern 'command_pipe|observer_disabled|native_sender_disabled|dropped|hook_error'
```

---

## 1. 启动与配置

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `configuration_error: unknown fields xxx` | 键名拼错或多写 | 对照 [配置规范](specs/configuration-spec.md)；框架拒绝未知键 |
| `another Plux runtime owns ...` | 同一数据库已有运行时（或上次没退干净） | 确认没有残留进程；必要时删除 `.runtime.lock` 文件（先确认无人持有） |
| `enabled observer requires an account and explicit allowed_targets` | 开了原生连接但账号/白名单为空 | 填 `policy.account` 与 `policy.allowed_targets` |
| `cannot load plugin entrypoint ...: No module named 'x'` | 插件包没安装到这个解释器 | `python -m pip install -e <包目录>`，并确认用的就是同一个 Python |
| `entrypoint is not a Plugin class` / `requires a static PluginManifest` | 入口指向了函数或缺 `manifest` | 入口必须是 `Plugin` 子类且带类级 manifest |
| `plugin x requires API 0.2; this runtime provides 0.1` | manifest 的 `api` 与框架不符 | 对齐 `API_VERSION` |
| `plugin x requires: media` | `capabilities` 声明了平台没有的能力 | 从能力全集里选，或去掉该声明 |
| `duplicate or reserved plugin namespace` | 两个插件用同一个 `namespace`，或用了 `platform` | 改命名空间 |
| `database has a newer migration version` | 用旧代码打开了新结构的库 | 换回匹配的代码版本，或恢复备份 |

---

## 2. 收不到消息

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `ingested` 一直是 0 | 观察日志不存在 | 框架**不会**创建它，需要 C++ 侧写入或先建空文件 |
| `ingested` 是 0 但文件明明有内容 | 配置指向了别的 `runtime_root` | `plux ... check` 的输出里核对 `observer_log` 实际路径 |
| 有 `ingested` 但 `processed` 为 0 | 记录被判为无效信封 | 查 `plux_issues` 里的 `invalid_envelope` / `unsupported_schema` |
| 群消息完全没进队列 | 内容缺少 `发送者:` 前缀，无法确定 actor，方向为 `unknown` | 这是有意的：不可归属的群消息只留档 |
| 图片/语音消息是 `UnknownMessage` | 内容读取失败或 `msg_type` 不在 1/3/34 | 看 `quality.issues` 与 `*_read` 诊断 |
| 重启后旧消息又被回答了一次 | 日志被截断/替换导致偏移归零 | 正常行为受 `event_key` 去重保护；若确实重复，检查日志是否被轮转成同名新文件 |

---

## 3. 插件不执行

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `processed` 在涨但没有任何回复 | 消息是启动前就在日志里的历史 | 先启动运行时、再追加日志内容；这是 backlog 边界 |
| 命令在群聊里不触发 | `group_requires_mention = true` 且没有显式 @ 本账号 | 真实 @ 一次，或把该群加入例外策略 |
| 命令完全不触发，`allow_unknown_history = false` | 事件 schema 2 无法证明消息实时 | 确认日志写入时序可信后打开该开关 |
| 事件处理器收不到消息 | `message_type` 不匹配 | `EventSpec(message_type=TextMessage)` 只收文本消息 |
| 事件处理器收到了自己发的消息 | 事件按类型订阅，不区分方向 | 在 handler 里判断 `message.identity.direction` |
| handler 每轮都在重试 | handler 抛了确定性异常 | 看 `plux_handler_runs.error_code`；把环境性失败改成语义化拒绝 |
| `services cannot perform business IO during plugin construction or registration` | 在 `__init__`/`register` 里访问了服务 | 把 IO 移到 `start()` 或 handler |

---

## 4. 发不出消息

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `delivery: blocked` | 连接不可发送：账号未验证、发送未启用或没有能力 | 看 `connection().issues` 与 `mode`；确认 C++ 侧 `WECHATBOT_NATIVE_SEND=1` |
| `issues=('account_not_verified',)` | `policy.account` 与登录账号不一致 | 用精确的 wxid，不要用昵称 |
| 一直停在 `queued` | 目标是白名单外的会话，或没有能力 | 把会话加进 `allowed_targets`；图片/语音需要 `media_send_enabled` |
| `reply account or target is not allowed` | 入队时账号或目标不符 | 同上 |
| 回复变成 `expired` | 超过 `ttl_seconds`（默认 300 秒）仍未投递 | 检查投递循环是否在跑；必要时缩短轮询间隔 |
| 状态是 `unknown` | 结果不确定（传输中断/响应不符/后端 unknown） | **不要**自动重发；用 `messages.receipt()` 看尝试与证据，必要时人工确认 |
| `native_sender_unavailable` | 后端没启用发送或没有原生后端 | 检查 C++ 侧的发送开关与注入状态 |
| `busy` / `rate_limited` | 后端正在发送或处于冷却 | 降低发送频率；这些是临时的，下一轮会重试（请求仍是 `queued`） |
| `capacity_unavailable` | 后端去重表满 | 重启后端或减少重复请求 |

---

## 5. 媒体发送

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `invalid_native_payload` | 资产内容与摘要不符、容器不受支持（非 PNG/JPEG/SILK）、尺寸超限 | 用 `assets.resolve()` 确认资产可用；图片必须是 PNG/JPEG，语音必须是 SILK |
| `media_path_not_allowed` | 文件不在媒体根目录的直接子级、文件名不是摘要、扩展名不符 | 不该出现：交付层会自动导出为 `<sha256>.<ext>`。若出现，检查 `paths.outbound` 与 C++ 侧 `WECHATBOT_MEDIA_ROOT` 是否一致 |
| `media_format_invalid` | 语音时长与 SILK 帧算出的时长不一致 | 插件必须提供真实时长（20 的整数倍，≤60000） |
| `media_hash_mismatch` | 文件在计算后被改动 | 资产是不可变的；检查是否有外部进程在改这些文件 |
| 图片/语音能力一直不可用 | 后端没启用媒体发送 | C++ 侧需要 `WECHATBOT_MEDIA_SEND=1`，Python 侧需要 `observer.media_send_enabled = true` |

---

## 6. 任务与计划

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 计划任务启动后立刻跑了一次 | 没有历史时隙记录时当前时隙视为到期 | 正常行为；需要「等满一个周期」就在 `work` 里按业务时间判断 |
| 任务停在 `uncertain` | 非幂等任务崩溃后无法确认外部动作 | 提供 `recover` 回调做核验，或人工确认后用 `tasks` 接口处理 |
| 任务 `failed` / `max_attempts` | `work` 连续失败达到上限 | 看 `error_code`；修因后重新入队（`task_key` 相同会被视为同一任务） |
| `task key collision` | 同一 `task_key` 提交了不同载荷 | 让 key 包含输入身份（例如 `digest:{event_key}`） |
| `task_version_mismatch` | 部署后 `input_version`/`result_version` 与存量任务不一致 | 这是有意的保护：不要自动把旧输入套进新规则 |
| 取消没生效 | 任务已越过检查点 | 取消是合作式的；`work` 后取消会落到 `uncertain` |

---

## 7. 数据、内容与迁移

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `applied migration content changed` | 改了已经执行过的迁移语句 | 新增一个版本号，不要改历史 |
| `migration versions must start at 1 and be contiguous` | 版本号跳号 | 补齐或用新插件命名空间 |
| `catalog version already has different content` | 同一版本号下内容变了 | 改 `CatalogSpec.version` 发布新版本 |
| `catalog reference has not been loaded` | 引用了不存在的版本 | 确认该版本在本进程启动时被 `load` 过 |
| 内容目录读不到文件 | `CatalogSpec.default` 用了相对路径 | 用 `Path(__file__).resolve().parent / ...` |
| `snapshot revision changed` | 并发更新或 `expected_revision` 过期 | 重新读取快照再更新 |
| `asset staging must run outside a write transaction` | 在 `atomic` handler 里 stage/publish | 移到 `start()` 或两次循环之间 |

---

## 8. 备份与恢复

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `backup must run outside a write transaction` | 在事务里调了备份 API | 只通过 CLI 备份；运行时先停 |
| `FileExistsError` | 备份/恢复目标已存在 | 换一个新路径；框架从不覆盖 |
| `backup asset ... missing or changed during backup` | 备份期间资产被改动或删除 | 停止运行时后备份 |
| `backup database checksum mismatch` | 备份被改动 | 重新备份 |
| 恢复后资产路径不对 | 恢复只还原数据库里记录的相对路径 | 用 `--assets` 指定与数据库记录匹配的根目录 |

---

## 9. 停机与锁

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `StopReport.stopped = False` | 还有线程没结束 | 报告里的 `active` 会指明是哪个 worker/drain；不要强制关闭数据库 |
| 停机后进程还在 | 插件 `stop()` 阻塞 | 插件不该在 `stop()` 里做长 IO；超时预算见 `runtime.stop_timeout_seconds` |
| `cannot close database with an active transaction` | 有事务没退出 | 通常是插件把 `uow` 存下来跨作用域用了 |
| CLI 退出码 1 | 健康检查有错误或停机未完成 | 看输出里的 `errors` |

---

## 10. 性能与吞吐

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 回复积压、发得慢 | 一轮只投递一条，且每次投递前都握手 | 调小 `poll_seconds`；或把多个回复合并成一条 |
| CPU 占用随积压上升 | 每轮都在重试 `failed` 处理器 | 修掉确定性失败 |
| 数据库变大 | 原始记录与证据是全量留档的 | 用 `maintain` 看容量；原始记录需要定期归档（框架不自动删） |
| 内存增长 | 事件按类型订阅、消息体较大 | 限制 `batch_size`；不要在插件里缓存整段历史 |

---

## 11. 回归检查

改动之后的推荐顺序：

```powershell
plux --config python\config\platform\plux.example.toml check     # 声明与资源
plux --config python\config\platform\plux.empty.toml run --once  # 空框架启停
```

再用一段临时 JSONL 驱动 `python/plugins/examples` 里的插件，确认摄取、路由、回复、任务、停机都在预期状态。注意日志要在运行时启动**之后**追加。
