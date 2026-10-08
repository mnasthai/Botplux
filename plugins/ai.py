"""统一 AI 问答入口；模型 I/O 由独立后台 worker 执行。"""
from wechat_receiver.ai.commands import handle_command, on_start, parse_command

NAME = "ai"
