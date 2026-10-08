"""Administrator-only private broadcasts; automatic lifecycle broadcasts are paused."""
from pathlib import Path

from wechat_receiver.plugins import GroupBroadcast, GroupBroadcastImage
from wechat_receiver.target import is_group_target

NAME = 'broadcast'

ASSETS_DIR = Path(__file__).resolve().parent / 'assets'
ONLINE_IMAGE_PATH = ASSETS_DIR / 'bot_online.png'
OFFLINE_IMAGE_PATH = ASSETS_DIR / 'bot_offline.png'

_HELP = """管理员广播
#广播 内容 —— 向所有已配置的群发送正文
#广播上线 —— 立即向所有群广播机器人上线卡片
#广播下线 —— 立即向所有群广播机器人下线卡片
#广播群列表 —— 查看广播目标
#广播帮助 —— 查看此说明

仅限管理员私聊机器人使用；正文支持换行。"""


def parse_command(message):
    if (message.message_kind != 'text' or not message.sender_id
            or message.conversation_id != message.sender_id
            or is_group_target(message.conversation_id)):
        return None
    text = (message.content or '').strip()
    if text in ('#广播', '#广播帮助'):
        return ('help', '')
    if text == '#广播群列表':
        return ('groups', '')
    if text in ('#广播上线', '#上线通知'):
        return ('online', '')
    if text in ('#广播下线', '#下线通知'):
        return ('offline', '')
    prefix = '#广播'
    if text.startswith(prefix) and len(text) > len(prefix) and text[len(prefix)].isspace():
        return ('send', text[len(prefix):].strip())
    return None


def handle_command(command, context):
    if not context.is_admin:
        return '仅管理员可以使用广播指令。'
    kind, body = command
    if kind == 'help':
        return _HELP
    if kind == 'groups':
        groups = sorted(target for target in context.allowed_targets if is_group_target(target))
        if not groups:
            return '没有可广播的群，请先配置群聊发送白名单。'
        return f'广播目标：共 {len(groups)} 个群\n' + '\n'.join(groups)
    if kind == 'online':
        if not ONLINE_IMAGE_PATH.is_file():
            return '未找到上线通知图片文件。'
        return GroupBroadcastImage(ONLINE_IMAGE_PATH)
    if kind == 'offline':
        if not OFFLINE_IMAGE_PATH.is_file():
            return '未找到下线通知图片文件。'
        return GroupBroadcastImage(OFFLINE_IMAGE_PATH)
    try:
        return GroupBroadcast(body)
    except (TypeError, ValueError):
        return '广播内容无效：请填写非空正文，最多 16384 个 UTF-8 字节，且不能包含 NUL 字符。'


def _check_startup(context):
    if not context.connection_id:
        return None
    run_key = f"{context.started_at:.4f}"
    context.store.execute("""
        CREATE TABLE IF NOT EXISTS broadcast_runs (
            account_id TEXT NOT NULL,
            run_key TEXT NOT NULL,
            event_type TEXT NOT NULL,
            PRIMARY KEY(account_id, run_key, event_type)
        )
    """)
    row = context.store.execute(
        "SELECT 1 FROM broadcast_runs WHERE account_id=? AND run_key=? AND event_type='online'",
        (context.account_id, run_key)
    ).fetchone()
    if row is not None:
        return None
    context.store.execute(
        "INSERT INTO broadcast_runs (account_id, run_key, event_type) VALUES (?, ?, 'online')",
        (context.account_id, run_key)
    )
    if ONLINE_IMAGE_PATH.is_file():
        req_id = int(context.started_at * 1000)
        return GroupBroadcastImage(ONLINE_IMAGE_PATH, request_key=f"online-{req_id}")
    return None


# 暂停自动上下线广播：连同入口定义一起注释，使插件加载器不注册这些钩子。
# 管理员私聊命令仍由上面的 parse_command / handle_command 正常处理。
# 注意：插件在进程内缓存；修改后需启动新的 Python 进程，旧进程退出仍可能执行旧回调。
#
# def on_before_messages(context):
#     return _check_startup(context)
#
#
# def on_poll(context):
#     return _check_startup(context)
#
#
# def on_shutdown(context):
#     if not context.connection_id:
#         return None
#     run_key = f"{context.started_at:.4f}"
#     context.store.execute("""
#         CREATE TABLE IF NOT EXISTS broadcast_runs (
#             account_id TEXT NOT NULL,
#             run_key TEXT NOT NULL,
#             event_type TEXT NOT NULL,
#             PRIMARY KEY(account_id, run_key, event_type)
#         )
#     """)
#     row = context.store.execute(
#         "SELECT 1 FROM broadcast_runs WHERE account_id=? AND run_key=? AND event_type='offline'",
#         (context.account_id, run_key)
#     ).fetchone()
#     if row is not None:
#         return None
#     context.store.execute(
#         "INSERT INTO broadcast_runs (account_id, run_key, event_type) VALUES (?, ?, 'offline')",
#         (context.account_id, run_key)
#     )
#     if OFFLINE_IMAGE_PATH.is_file():
#         req_id = int(context.now * 1000)
#         return GroupBroadcastImage(OFFLINE_IMAGE_PATH, request_key=f"offline-{req_id}")
#     return None
