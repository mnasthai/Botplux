# 大爱仙途 · 墨青山水卡片 v2

2026-09-28：`#修仙` 个人面板已切换到 [v3 深色金纹](../v3/README.md)。本目录继续提供仙榜、斗法预览及旧个人面板素材；以下为 v2 的原始设计记录。

这套资产把背景插画、无数据模块、运行时文字分开保存。三个背景使用内置 imagegen 生成；模块使用 Pillow 绘制可重复构建的细线切角图形。所有背景和组件均不包含游戏数据，中文标题也由 renderer 绘制。

## 目录与使用

- `templates/bg_profile.png`：月下山门。
- `templates/bg_ranking.png`：云阶仙鹤。
- `templates/bg_duel.png`：金色剑光与紫色雷纹。
- `components/`：16 个 RGBA 模块，没有文字、数字和状态。
- `receiver/src/wechat_receiver/games/renderer.py`：绘制模块中的实际数据，组合为 1080×1440 PNG。

画布四边为文字保留至少 64 px 安全区；正文以玉白、柔金和浅玉色为主。装饰景物集中在顶部与边缘。背景中的框线不承担数据布局，数据模块按独立坐标摆放，因此更换山水插画不会改变游戏数据结构。

| 组件 | 尺寸 | 用途 |
| --- | --- | --- |
| panel_jade | 952×190 | 修为、灵石、进度 |
| panel_inventory | 952×288 | 储物袋区 |
| panel_status | 952×184 | 今日状态 |
| slot_artifact / spirit / ancient / treasure / empty | 140×174 | 法宝及空槽 |
| rank_1 / 2 / 3 / common | 952×84 | 金、银、铜及普通榜单行 |
| duel_winner / loser | 460×356 | 胜败对手 |
| panel_loot | 952×240 | 战利品与实际修为扣除 |
| panel_support | 952×328 | 围观支持结算 |

## 维护

在项目虚拟环境安装 `receiver[visual]`，并使用媒体版本发送图片。模块已保存为可直接读取的 PNG；修改模块配色或尺寸时，从任意目录运行：

```powershell
& 'E:\wechatbot\.venv\Scripts\python.exe' 'E:\wechatbot\tools\build_modular_assets.py'
```

背景重新绘制需调用图像模型；构建模块脚本不会对背景涂抹、擦字，也不依赖外部临时目录。旧资产保留在上一级目录，当前渲染器使用此 v2 目录。

在 `receiver/xiuxian.local.toml` 的 `[game]` 下设置 `visual_cards_enabled = true` 启用个人面板与仙榜图片，设为 false 恢复文字。本机已开启；机器人重新加载配置后生效。图片生成失败会记录异常并回退文字，不改变游戏结算。由于图片渲染较慢，斗法结算暂时恢复文字；已验证的图片战报接入分支在 `games/duels.py` 中注释保留，渲染器、素材及离线预览继续保留。斗法阶段的 @ 提醒和取消通知继续使用原有文本。

使用合成数据生成预览，不连接微信或真实数据库：

```powershell
& 'E:\wechatbot\.venv\Scripts\python.exe' 'E:\wechatbot\tools\preview_xiuxian_cards.py'
```

输出在 `runtime/media/card-previews`，包含标准卡片及边界样例。预览中的人名、修为、法宝与结算均为模拟数据。

本轮视觉检查覆盖三张标准成品、十行榜单、长道号和大数值、库存超限与当前斗法提示、超时判负及多行围观摘要。已去除干扰文字的横纹，修正标题与山景的位置关系；标准 PNG 为 1080×1440。预览用于检查本地排版，尚未向微信群发送这套新卡片做手机端实机验收。

## 背景生成记录

方式：内置 imagegen。仙榜与斗法图以上述个人面板背景作风格参考，维持统一的色调与边框；没有把生成文字用于最终数据层。以下保留实际使用的完整提示词，方便后续定向改稿。

### 个人面板

```text
Create a production-ready BACKGROUND illustration for a Chinese xianxia cultivation game PERSONAL PROFILE card. Portrait 3:4 aspect ratio, preferably 1536 x 2048 pixels. This is the first of a cohesive three-card set. Art direction: elegant ink-wash fantasy landscape on very dark blue-green silk paper, desaturated jade mist, restrained antique pale gold line details, sophisticated Chinese game editorial art, luminous but quiet, beautifully painterly. Strong layout constraint: all scenic interest in the upper 23 percent and narrow outer margins; central and lower 75 percent must remain an uninterrupted smooth near-black deep teal surface with extremely faint silk texture, for modular information panels to be overlaid later. In the upper right only, misty layered mountains and a small isolated Daoist pavilion beneath a pale jade moon; upper left half stays calm and dark to receive the title. Bottom edge can have very faint curling jade mist. Very fine understated antique-gold corner filigree along the outer 3 percent, not bulky frames. ONE unified seamless artwork, no flat rectangular paint-overs, no visible content placeholders or boxes. Absolutely NO text, NO lettering, NO numbers, NO logos, NO seals with characters, NO UI widgets, NO portraits, NO people. Avoid heavily embossed gold, dragons, busy sparkling particles, modern dashboard styling, bright background behind text. The artwork should feel like a collectible page from a refined Daoist immortal chronicle.
```

### 仙榜

```text
Create a NEW production-ready background illustration for a Chinese xianxia game LEADERBOARD card, matching the attached PERSONAL PROFILE background as a cohesive premium art set. Reference image is ONLY for palette, restrained gold border, silk texture, quiet large data area and overall layout; make a distinct new scenic artwork. Portrait 3:4 aspect ratio, preferably 1536 x 2048. Dark ink-teal silk, elegant painterly jade mountain mists, very fine antique pale-gold details. All scenic interest restricted to upper 23 percent and narrow outer edges. Upper RIGHT: towering distant immortal mountains, very small moonlit celestial terraces, two elegant ivory cranes gliding upward through jade mist, perhaps a faint curved celestial orbit, an uplifting ascent theme. Upper LEFT must remain calm dark negative space for title and subtitle. Center/lower 75 percent must be uninterrupted near-black teal with extremely subtle silk texture, ready for ten readable ranking rows to be overlaid. Fine antique gold corner decorations only at outer 3 percent, delicate side lines. Bottom corners understated jade cloud scrolls. No heavy central frame, no internal rectangles or boxes. NO text, NO letters, NO calligraphy, NO numbers, NO logos, NO seals, NO UI, NO people, NO fake ranking data. Do not include large bright scenery beneath the future data rows. Refined Chinese immortal chronicle, not glossy mobile-game advertisement.
```

### 斗法战报

```text
Create a NEW production-ready background illustration for a Chinese xianxia game DUEL RESULT report, matching the attached personal-profile background's refined painterly style, border construction and layout. This is the third image in a cohesive set. Portrait 3:4, preferably 1536 x 2048. Main palette remains very dark ink-teal silk, aged pale gold, jade, with restrained smoky amethyst accents specific to the duel. All scenic elements confined to upper 23 percent and narrow edges: in upper RIGHT, distant mountain peaks in storm mist, a fine golden sword-shaped ray crossing a slender muted violet lightning arc above the ridges. No characters, no figures, no bodies. Suggest a duel of cultivation energies, NOT an explosion. Upper LEFT stays dark and quiet to receive title. Central and lower 75 percent stays an uninterrupted near-black blue-green silk surface, nearly empty, ready for two player panels and readable results to be composited later. Very fine antique gold corner filigree and side rules in outer 3 percent, matching reference. Lower corners contain restrained mist, subtle violet/jade wisps and a few minute warm glints. ONE seamless illustration; absolutely NO labels, text, numbers, calligraphy, logos, seals, data, boxes, panels, UI widgets, or thick frames. No bright lightning inside future body text area. Elegant immortal chronicle, dignified and quietly dramatic.
```
