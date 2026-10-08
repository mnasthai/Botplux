"""修仙游戏的命令与生命周期入口，复用现有收发及事务运行器。"""
from wechat_receiver.games.commands import parse_command
from wechat_receiver.games.service import handle_command
from wechat_receiver.games import duels
from wechat_receiver.games.dungeon import service as dungeon

NAME = 'xiuxian'


def _run_hooks(name, context):
    replies = []
    for module in (duels, dungeon):
        hook = getattr(module, name)
        result = hook(context) if name == 'on_start' else hook(context, limit=3 - len(replies))
        if result is not None:
            replies.extend(result if isinstance(result, (list, tuple)) else [result])
    return replies


def on_start(context):
    return _run_hooks('on_start', context)


def on_before_messages(context):
    return _run_hooks('on_before_messages', context)


def on_poll(context):
    return _run_hooks('on_poll', context)
