"""Reply to the exact help command with an image."""

from pathlib import Path
from wechat_receiver.plugins import ReplyImage

NAME = "help"

HELP_IMAGE_PATH = Path(__file__).resolve().parent / "assets" / "help.png"

# 原有文本定义保留（注释未删除）：
# HELP_TEXT = """╭─── 📖 指令导航菜单 ───╮
# 
#  ⚔️ 大爱仙途
#   ├ #修仙 <仙名>  » 创建角色／修改仙名
#   └ #修仙帮助     » 查看具体玩法指引
# 
#  🔮 问道
#   ├ #问道 <问题>       » 20 灵石
#   
#   └ #问道 天机 <问题>  » 40 灵石
# 
#  🔢 实用工具计算器
#   ├ 格式：@bot 计算 <表达式>
#   ├ 示例：@bot 计算 2^10 + 512
#   └ ※ @ 放在算式前或后均可识别
# 
# ╰──────────────╯"""


def on_message(message):
    content = message.content
    if isinstance(content, str) and content.strip() == "#帮助":
        # 原有的回复文本逻辑先注释不要删除：
        # return HELP_TEXT
        if HELP_IMAGE_PATH.is_file():
            return ReplyImage(HELP_IMAGE_PATH)
        # 备用降级（注释保留）：
        # return HELP_TEXT
    return None
