# 大爱仙途 · 深色金纹个人面板 v3

2026-09-28 重设计 `#修仙`，方向为黑金山门、境界印章、双列法宝陈列。仅个人面板使用这套背景；仙榜与斗法预览仍使用 v2。

## 资产与渲染

- `bg_profile.png`：内置 imagegen 生成的完整无文字背景，已保存到项目。保留生成原图，不在位图中固化道号、境界、法宝或数值。
- `receiver/src/wechat_receiver/games/renderer.py`：实时绘制 1080×1440 PNG；名称用行楷，数值与说明用微软雅黑；模块、品质色、法宝分类线条图标均由代码绘制。
- 突破门槛和秘境费用使用 `games/artifact_effects.py` 中与结算相同的函数，冷却使用 `games/cooldowns.py`。面板不会另行猜测法宝折扣。
- 库存默认六格，乾坤鼎扩容为八格；超过八件保留总数、超限及查看全部的提示。长道号按宽度缩放后省略。乾坤鼎单独注明“扩容 · 不加攻击”。
- 四项状态为每日修炼、秘境探索、自主修炼、灵矿采矿；魔契/道具数在标题下方，受咒与当前斗法在底部独立显示。
- 沿用 `[game] visual_cards_enabled = true` 和媒体模式；渲染失败沿用文字回复。替换 Python 代码和此图片即可，无需构建 DLL。

## 离线预览

使用合成玩家数据，不读取运行数据库，也不连接微信：

```powershell
& 'E:\wechatbot\.venv\Scripts\python.exe' 'E:\wechatbot\tools\preview_xiuxian_cards.py' --profiles-only --output-dir 'E:\wechatbot\outputs\xiuxian-profile-v3'
```

生成普通六件、八件扩容/长道号/大数值/受咒，以及新玩家空储物袋三种 PNG；对应 `_preview.jpg` 为长边 1280、质量 85 的阅览副本。已实际查看普通布局、八件扩容和新玩家布局，核对名称、品质、数值、长道号和底部提示未重叠。手机微信实机显示未在本次任务中发送验证。

## 背景生成提示词

工具：内置 `image_gen`，非 CLI。最终使用的完整提示词如下：

```text
Use case: stylized-concept.
Asset type: production background artwork for a Chinese xianxia game character profile, portrait 3:4 aspect ratio, 1536 x 2048.
Primary request: sophisticated DARK BLACK AND ANTIQUE GOLD immortal cultivation profile, a fresh premium interface background with no baked text or widgets.
Scene: obsidian black lacquered silk, softly illuminated ancient gilded mountain ridges in the upper right 25%, tiny distant celestial palace, very restrained gold dust and subtle bronze cloud engravings only at the outer perimeter. Refined painterly Chinese fantasy ink art, dignified collector's folio, flat front view.
Composition: upper left is calm black negative space for player name, a subtle thin circular gold halo behind upper right for the future realm seal, positioned at 78% width 15% height. Center and bottom 75% is uniform near-black warm charcoal with extremely subtle silk grain to support crisp runtime text and inventory. Narrow gold double rules and understated geometric corner engravings on outer 2%. No content frames inside; no horizontal bars dividing canvas. Gold atmospheric mountains confined to top and edges, never behind data area.
Palette: near-black #111212, warm charcoal, muted antique champagne gold #CBA669, ivory, extremely limited jade. Quiet luxurious glow, no glossy bevel, no harsh yellow.
Constraints: no text, no calligraphy, no letters, no digits, no logos, no watermark, no portraits, no people, no equipment icons, no fake game data, no UI panels, no empty boxes. This must be clean reusable BACKGROUND ART, not a finished UI screenshot.
```
