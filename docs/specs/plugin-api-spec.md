<div align="center">

# 📐 公共 API 规范

[![API](https://img.shields.io/badge/API-0.1-blue.svg?style=flat-square)](#)
[![Import](https://img.shields.io/badge/Import-plux.api%20only-critical.svg?style=flat-square)](#1-错误)

<p align="center">
  <code>plux.api</code> 是插件唯一允许导入的框架入口。
</p>

</div>

---

```python
from plux.api import ...   # 只从这里导入
```

插件**不允许**导入 `plux.runtime`、`plux.data`、`plux.messaging`、`plux.adapters` 或其他插件的内部模块。

---

## 1. 错误

| 类型 | `code` | 语义 | 框架是否主动抛出 |
| --- | --- | --- | --- |
| `PluxError` | `plux_error` | 基类 | — |
| `ConfigurationError` | `configuration_error` | 配置、装配、处理器返回值不合法 | 是 |
| `ConflictError` | `conflict` | 身份或版本冲突（内容目录、快照修订、注册重名） | 是 |
| `ResourceMissing` | `resource_missing` | 引用的内容或资产不存在 | 是 |
| `ResourceNotReady` | `resource_not_ready` | 存在但不可用（摘要不符、备份被改动） | 是 |
| `UnsupportedCapability` | `unsupported_capability` | 插件声明的能力平台不提供 | 是 |
| `InvalidScope` | `invalid_scope` | 事务作用域、命名空间或生命周期不匹配 | 是 |
| `InfrastructureError` | `infrastructure_error` | 基础设施故障 | 否（保留给插件） |
| `UncertainResult` | `uncertain_result` | 结果不确定 | 否（保留给插件） |

约定：**业务拒绝不是异常**，用 `Outcome.rejected(code, ...)` 表达。异常用于环境与装配问题。CLI 把 `PluxError`、`OSError`、`ValueError`、`sqlite3.Error` 统一映射为退出码 `2`。

---

## 2. 不可变模型

所有模型都是 frozen dataclass，时间必须是带时区的 UTC（`require_utc` 会校验）。

### 2.1 身份与引用

| 类型 | 字段 |
| --- | --- |
| `MessageIdentity` | `account`、`conversation`、`actor`、`native_session=None`、`direction="inbound"` |
| `MessageRef` | `event_key`、`message_id=None` |
| `MemberRef` | `account`、`member_id`、`conversation=None` |
| `AssetRef` | `asset_id`、`version`、`kind`、`sha256` |
| `CatalogRef` | `namespace`、`version`、`name=None` |
| `RequestRef` | `request_id` |
| `MessageIdentity.direction` | `inbound` / `self_sync` / `outbound_request` / `unknown` |

### 2.2 消息

| 类型 | 关键字段 |
| --- | --- |
| `BaseMessage` | `event_key`、`identity`、`observed_at`、`quality` |
| `TextMessage` | `text`、`mentions`、`quote` |
| `ImageMessage` | `asset`、`media_key`、`width`、`height` |
| `VoiceMessage` | `asset`、`media_key`、`duration_ms`、`transcription` |
| `UnknownMessage` | `raw_type`、`raw_ref`、`issues` |
| `ContentQuality` | `status`、`mentions_status`、`history_status`、`issues`、`ingestion_status` |

`ContentQuality` 的取值：

| 字段 | 取值 |
| --- | --- |
| `status` | `ok`（内容与关键字段都完整）/ `uncertain` |
| `mentions_status` | `explicit_self` / `explicit_other` / `unknown` |
| `history_status` | `realtime` / `backlog` / `unknown` |
| `ingestion_status` | `new` / `backlog` / `unknown` |

`issues` 是字符串元组，例如 `group_sender_prefix_missing`、`object_validation_failed`、`missing_observed_time`、`content_unavailable`、`unsafe_msg_source_xml`。内容不可信时消息退化为 `UnknownMessage`，不会变成空文本。

### 2.3 回复与回执

| 类型 | 字段 |
| --- | --- |
| `ReplyIntent` | `reply_key`、`account`、`conversation`、`native_session`、`text`、`asset`、`mentions`、`quote`、`ttl_seconds=300`、`source_event_key`、`duration_ms=0` |
| `Outcome` | `status`、`result`、`replies`、`tasks`、`error_code` |
| `RequestRef` | `request_id` |
| `Attempt` | `attempt_id`、`status`、`started_at`、`finished_at`、`error_code` |
| `Receipt` | `request_id`、`status`、`attempts`、`evidence`、`updated_at`、`error_code` |

`Outcome` 只能由三个构造器产生：

```python
Outcome.success(result=None, *, replies=(), tasks=())
Outcome.rejected(error_code, *, replies=())
Outcome.noop()
```

`status` 只能是 `success` / `rejected` / `noop`；其他值会让框架抛 `ConfigurationError` 并回滚当前事务。

`ReplyIntent` 的完整约束见 [插件开发参考 §4.2](../guide/plugin-development.md#42-replyintent-的硬约束)。

### 2.4 连接快照

| 字段 | 说明 |
| --- | --- |
| `account` | 原生侧报告的账号；未验证时为 `None` |
| `native_session` | 本次观察会话 |
| `sampled_at` | 采样时间 |
| `source` | 来源，例如 `hello_media` / `command_pipe_ready` / `observer_start` |
| `phase` | `disconnected` / `discovered` / `read_only` / `ready` |
| `capabilities` | 可用能力子集 |
| `can_send` | 账号已验证 **且** 发送已启用 **且** 至少一个能力可用 |
| `generation` | 会话更替计数 |
| `issues` | 例如 `account_not_verified`、`command_pipe_error`、`native_sender_disabled` |

> [!IMPORTANT]
> `can_send` 为真不代表某条具体消息一定能发出：目标白名单、媒体能力、频控与有效期都可能在投递时拒绝。**能力、握手成功、当前可发送是三件不同的事。**

### 2.5 查询与分页

| 类型 | 字段 |
| --- | --- |
| `HistoryQuery` | `account`、`conversation`、`limit=50`、`cursor=None` |
| `Page` | `items`、`next_cursor` |
| `MemberSnapshot` | `ref`、`display_name`、`sampled_at`、`source`（`missing` 表示查不到） |

`limit` 允许 1..200；`account` 必须等于当前账号，否则 `ValueError`。

### 2.6 数据声明

| 类型 | 字段 |
| --- | --- |
| `Migration` | `version`、`statements` |
| `CatalogSnapshot` | `ref`、`format_version`、`digest`、`data`、`sources` |
| `StateSnapshot` | `key`、`structure_version`、`revision`、`data`、`deadline`、`catalog`、`assets`、`durability="persistent"` |
| `TaskIntent` | `task_key`、`task_type`、`payload`、`not_before`、`deadline`、`catalog`、`assets` |
| `CatalogSpec` | `name`、`version`、`default`、`format_version=1`、`validator`、`override` |

`StateSnapshot.data` 冻结后写入：映射变成只读映射，列表变成元组。`durability` 只接受 `"persistent"`。

### 2.7 登记声明

```python
PluginManifest(plugin_id, version="0.1.0", api="0.1", dependencies=(), capabilities=frozenset(),
               namespace=None, database_domain="main", migrations=(), config_validator=None,
               catalogs=(), resources=())

CommandSpec(handler_id, trigger, handler, mode="stateless", priority=0, database_domain="main")
EventSpec(handler_id, handler, message_type=BaseMessage, predicate=None, mode="atomic",
          serial_key=None, database_domain="main", requires_command_policy=False)
ScheduleSpec(schedule_id, task_type, interval_seconds, payload=None, timezone="UTC",
             missed_policy="coalesce")
TaskSpec(task_type, work, commit, prepare=None, recover=None, idempotent=False,
         max_attempts=1, timeout_seconds=60.0, concurrency_key=None,
         input_version=1, result_version=1)
```

登记时会被拒绝的组合：

| 声明 | 结果 |
| --- | --- |
| `database_domain != "main"` | `UnsupportedCapability` |
| `CommandSpec.mode` 不是 `stateless`/`atomic` | `ConfigurationError` |
| `EventSpec.serial_key` 非空 | `ConfigurationError`（串行调度器未实现） |
| `TaskSpec.concurrency_key` 非空 | `ConfigurationError`（键解析器未实现） |
| `ScheduleSpec.timezone != "UTC"` | `ConfigurationError` |
| `ScheduleSpec.missed_policy` 不在 `skip`/`coalesce`/`catch_up` | `ConfigurationError` |
| 同 `trigger` 且同 `priority` 的命令 | `ConflictError` |
| 同插件内重复 `handler_id` | `ConflictError` |
| `schedule.task_type` 没有对应 `TaskSpec` | `ConfigurationError` |

---

## 3. 服务

`PluginServices` 是注入到插件构造函数的视图：

```python
PluginServices(messages, data, tasks, clock, logger, config=None)
```

### 3.1 `messages: MessageServices`

| 方法 | 说明 |
| --- | --- |
| `get(event_key, uow=None) -> BaseMessage \| None` | 按稳定身份取消息 |
| `history(query, uow=None) -> Page` | 倒序稳定游标分页 |
| `member(ref, uow=None) -> MemberSnapshot` | 成员资料；缺失返回 `source="missing"` |
| `media(media_key, uow=None) -> Mapping \| None` | 媒体处理记录 |
| `receipt(request_id, uow=None) -> Receipt` | 回复状态、尝试与证据；不存在抛 `KeyError` |
| `reply_reference(reply_key, uow=None) -> RequestRef \| None` | 反查本插件提交过的回复 |
| `input_status(uow=None, *, exclude_event_key=None) -> Mapping` | 待处理积压概览 |
| `connection() -> ConnectionSnapshot` | 内存快照，无 IO |
| `enqueue(intent, uow) -> RequestRef` | 在有效事务内手工入队 |
| `cancel(request_id, uow) -> bool` | 撤销尚未尝试的回复 |

不传 `uow` 时方法自己开一个短事务；传了就必须是当前作用域内、属于同一数据库的事务，否则 `ValueError`/`InvalidScope`。

### 3.2 `data: DataServices`

```python
DataServices(database, catalogs, assets, snapshots, maintenance)
```

| 端口 | 方法 |
| --- | --- |
| `database` | `transaction()`、`migrate(namespace, migrations)`；`domain`、`capabilities` |
| `catalogs` | `load(name, *, version, default, override, validator, format_version) -> CatalogSnapshot`、`get(name, version=None) -> CatalogSnapshot` |
| `assets` | `stage(source, *, kind, max_bytes) -> str`、`publish(staging_id) -> AssetRef`、`resolve(ref) -> Path`、`retain(ref, reference, uow)`、`release(ref, reference, uow)` |
| `snapshots` | `get(key, uow=None)`、`put(snapshot, *, expected_revision, uow)`、`delete(key, *, expected_revision, uow)` |
| `maintenance` | `inspect() -> Mapping`、`collect(*, limit, older_than) -> Mapping` |

`DataServices.repository_factory(factory) -> RepositoryFactory`，其 `bind(uow)` 只在有效事务里返回仓储实例。

插件的 `database.migrate` 只能使用自己的命名空间；正常做法是在 manifest 里声明迁移，由启动流程执行。

### 3.3 `tasks: TaskServices`

| 方法 | 说明 |
| --- | --- |
| `enqueue(intent, uow) -> str` | 在有效事务内受理任务；同一 `task_key` 载荷不同会抛 `ConfigurationError` |
| `get(task_key) -> Mapping \| None` | 任务投影 |
| `cancel(task_key) -> bool` | 合作式取消（实现提供的额外方法） |

### 3.4 `clock` / `logger` / `config`

- `clock.now()` 返回带时区的 UTC 时间。
- `logger` 是 `logging.Logger`（`plux.plugin.<plugin_id>`），支持 `info/warning/error`。
- `config` 是 `config_validator` 的返回值；没有校验器时是配置原文的冻结副本。

---

## 4. 上下文

```python
PluginContext(plugin_id, call_id, event_key, account, conversation, actor,
              native_session, observed_at, permissions=frozenset(), uow=None, cancellation=Event())
```

| 字段 | 说明 |
| --- | --- |
| `call_id` | 本次执行身份；消息调用是 `事件键:插件:处理器`，任务调用是 `task:<插件>:<任务键>` |
| `event_key` | 稳定输入身份；任务调用等于任务键 |
| `account` / `conversation` / `actor` | 调用身份；任务调用为 `None` |
| `native_session` | 本次调用的原生会话；任务调用为 `None` |
| `observed_at` | 输入观察时间 |
| `permissions` | 已验证权限（当前版本为空集） |
| `uow` | 有效事务作用域，可能为 `None` |
| `cancellation` | 框架的合作式取消信号 |

规则：

1. **不要保存 `uow`**，离开处理作用域即失效；也不要把它传给其他线程。
2. `PluginContext` 由框架构造，插件只读取。
3. 需要长期保存的引用（`services`、内容快照、资产引用）不包含事务。

---

## 5. 插件基类

```python
class Plugin(ABC):
    manifest: PluginManifest
    def __init__(self, services: PluginServices) -> None
    @abstractmethod
    def register(self, registry: PluginRegistry) -> None
    def start(self) -> None
    def stop(self) -> None
```

`PluginRegistry` 提供 `command(spec)`、`event(spec)`、`schedule(spec)`、`task(spec)`。注册阶段的服务被门禁包住，任何业务 IO 都会抛 `ConfigurationError`。

---

## 6. 版本与兼容

- `API_VERSION` 参与加载校验：插件 manifest 的 `api` 与之不等就拒绝加载。
- 0.x 阶段允许调整公共 API，但会在文档与变更记录里说明迁移方式。
- 插件版本、配置格式、内容版本、快照结构版本、迁移版本、原生 wire 版本**各自独立**，不互相推导。
