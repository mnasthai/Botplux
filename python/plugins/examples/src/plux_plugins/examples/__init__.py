"""Independent business examples. Framework dependencies use only plux.api."""
from __future__ import annotations
import ast
import math
import operator
from typing import Any, Mapping

from plux.api import (CatalogSpec, CommandSpec, ConfigurationError, Migration,
                      Outcome, Plugin, PluginContext, PluginManifest,
                      ReplyIntent, TaskIntent, TaskSpec)
from .support import reject, respond

def _help_text() -> str:
    return ("/help — 命令说明\n/calc 1 + 2 * 3 — 计算\n/count — 持久计数\n"
            "/count-later — 后台计数\n/kw — 关键词规则\n/vote <选项> — 投票\n"
            "/poll — 查看票数\n/poster — 发送示例图片\n/digest — 立即生成摘要")

class HelpPlugin(Plugin):
    manifest = PluginManifest("help")
    def register(self, registry) -> None:
        registry.command(CommandSpec("help", "/help", self.help))

    def help(self, argument: str, context: PluginContext) -> Outcome:
        return respond(context, text=_help_text())

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}

def calculate(expression: str) -> int | float:
    if not expression or len(expression) > 256:
        raise ValueError("表达式需要为 1–256 个字符")
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 80:
        raise ValueError("表达式过长")
    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = node.value
        elif isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            value = _BIN[type(node.op)](visit(node.left), visit(node.right))
        elif isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            value = _UNARY[type(node.op)](visit(node.operand))
        else:
            raise ValueError("仅支持数字、括号与 + - * / // %")
        if not math.isfinite(value) or abs(value) > 1e15:
            raise ValueError("数值超出范围")
        return value
    return visit(tree)

class CalculatorPlugin(Plugin):
    manifest = PluginManifest("calculator")
    def register(self, registry) -> None:
        registry.command(CommandSpec("calculate", "/calc", self.calculate))

    def calculate(self, argument: str, context: PluginContext) -> Outcome:
        try:
            result = calculate(argument)
        except (ValueError, SyntaxError, ArithmeticError) as exc:
            return reject(context, "invalid_expression", text=f"无法计算：{exc}")
        return respond(context, text=str(result), result=result)

def _counter_config(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    if set(raw) - {"maximum"}:
        raise ConfigurationError("counter config supports only maximum")
    maximum = raw.get("maximum", 1000000)
    if type(maximum) is not int or not 1 <= maximum <= 1000000000:
        raise ConfigurationError("counter maximum must be an integer in 1..1000000000")
    return {"maximum": maximum}

def _counter_rules(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    if raw != {"step": 1}:
        raise ConfigurationError("counter content requires step=1")
    return raw

class CounterRepository:
    def __init__(self, uow) -> None:
        self.uow = uow

    def increment(self, account: str, conversation: str, actor: str, maximum: int, step: int) -> int | None:
        row = self.uow.execute(
            "SELECT value FROM example_counter WHERE account=? AND conversation=? AND actor=?",
            (account, conversation, actor)).fetchone()
        value = (row[0] if row else 0) + step
        if value > maximum:
            return None
        self.uow.execute("""INSERT INTO example_counter(account,conversation,actor,value) VALUES (?,?,?,?)
            ON CONFLICT(account,conversation,actor) DO UPDATE SET value=excluded.value""",
            (account, conversation, actor, value))
        return value

class CounterPlugin(Plugin):
    manifest = PluginManifest(
        "counter", capabilities=frozenset({"transactions", "tasks", "catalogs"}),
        config_validator=_counter_config,
        migrations=(Migration(1, ("""CREATE TABLE example_counter (
            account TEXT NOT NULL, conversation TEXT NOT NULL, actor TEXT NOT NULL,
            value INTEGER NOT NULL, PRIMARY KEY(account,conversation,actor))""",)),),
        catalogs=(CatalogSpec("rules", "1", {"step": 1}, validator=_counter_rules),),
    )

    def __init__(self, services) -> None:
        super().__init__(services)
        self.repositories = services.data.repository_factory(CounterRepository)
        self.rules = None

    def register(self, registry) -> None:
        registry.command(CommandSpec("count", "/count", self.count, mode="atomic"))
        registry.command(CommandSpec("count-later", "/count-later", self.count_later, mode="atomic"))
        registry.task(TaskSpec("increment", self.work, self.commit, idempotent=True, max_attempts=3))

    def start(self) -> None:
        self.rules = self.services.data.catalogs.get("rules").data

    def count(self, argument: str, context: PluginContext) -> Outcome:
        value = self.repositories.bind(context.uow).increment(
            context.account, context.conversation, context.actor,
            self.services.config["maximum"], self.rules["step"])
        if value is None:
            return reject(context, "maximum_reached", text="已达到计数上限")
        return respond(context, text=f"计数：{value}", result=value)

    def count_later(self, argument: str, context: PluginContext) -> Outcome:
        message = self.services.messages.get(context.event_key, context.uow)
        return Outcome.success(tasks=(TaskIntent(f"increment:{context.event_key}", "increment", {
            "account": context.account, "conversation": context.conversation, "actor": context.actor,
            "native_session": message.identity.native_session, "event_key": context.event_key,
            "rules_version": self.services.data.catalogs.get("rules").ref.version,
        }, catalog=self.services.data.catalogs.get("rules").ref),))

    def work(self, payload: Any, context: PluginContext) -> Any:
        # This stage has no transaction. Real plugins may render or perform bounded IO here.
        result = dict(payload)
        result["step"] = self.services.data.catalogs.get("rules", payload["rules_version"]).data["step"]
        return result

    def commit(self, result: Any, context: PluginContext) -> Outcome:
        value = self.repositories.bind(context.uow).increment(
            result["account"], result["conversation"], result["actor"],
            self.services.config["maximum"], result["step"])
        text = "已达到计数上限" if value is None else f"后台计数：{value}"
        return Outcome.success(value, replies=(ReplyIntent(
            f"{context.event_key}:reply", result["account"], result["conversation"],
            result["native_session"], text=text, source_event_key=result["event_key"]),))
