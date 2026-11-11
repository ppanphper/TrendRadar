# 推送降噪与 Telegram 摘要设计

本文件记录本地 fork 在推送侧的设计：Telegram 摘要排版、跨次推送去重、定时摘要与即时提醒、RSS 快照。上游原有行为在未开启这些选项时保持不变。

## 目标

- 每天只在固定时刻收到摘要，一屏内先看到最值得看的几条；高相关的新闻即时提醒。
- 同一条新闻只推送一次，跨推送、跨天都不重复。
- linux.do 等易下架的帖子在抓取时留存本地快照。

验收（用 2026-11-11 真实数据回放）：17:01 全天汇总原为 23 条 Telegram 消息，摘要版式不超过 3 条；同一标题在已推送记录保留期内不再出现。

## 1. Telegram 原生渠道

- 通过 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` 发送，parse_mode=HTML，关闭链接预览。
- 新增 `message_thread_id`（`notification.channels.telegram.message_thread_id` 或 `TELEGRAM_MESSAGE_THREAD_ID`），发到群组话题；多账号时按 `;` 与 chat_id 一一对应，只填一个则全部账号共用。
- 新增 `style`：`digest`（摘要版式，本设计）或 `classic`（上游原版式，默认）。
- 静音：调度时段 `silent: true` 时以 `disable_notification` 发送。
- 发送失败且错误为实体解析失败（HTTP 400 can't parse entities）时，去掉标签改发纯文本，避免整轮中断。

## 2. 摘要版式（style: digest）

长度按 Telegram 规则计算：正文 1-4096 字符，按去掉标签、还原实体后的 UTF-16 长度计，留 300 余量（上限 3800）。上游按 4000 字节切分，中文每条只装约 1300 字，这是消息被拆碎的主因。

消息结构（自上而下）：

1. 标题行：`<b>{图标} {类型} · {MM-DD HH:MM}</b>`，下一行是条数，如 `热榜 12 · RSS 5`。类型：早报/摘要（定时）、提醒（即时）、增量、汇总。
2. 精选：全部条目按 AI 相关度（无分数时按最高排名）取前 5 条，标题为链接，第二行斜体写来源和分组。
3. 分组：按 AI 标签（关键词模式为词组）合并热榜和 RSS 条目，每组显示前 3 条，其余放进默认折叠的 `<blockquote expandable>`。已在精选出现的条目不再重复。
4. 独立展示区：每个源一个分组，规则同上。
5. AI 分析：整段放进折叠引用块，标题 `✨ AI 分析`。

条目行：`• <a href="url">标题</a> <i>来源 #排名</i>`。去掉上游的时间范围和出现次数。

即时提醒（时段设置了 `push_min_score`）只列条目，不分组、不含 AI 分析。

切分：以分组为单位装箱，单个分组超长时按行切开并在新消息中补上分组标题和引用块标签。

## 3. 已推送记录（跨次去重）

- 配置 `notification.dedupe: {enabled, retention_days}`，默认关闭；保留期默认 7 天。
- 存储：`output/meta/push_ledger.db`（SQLite，仅本地存储后端可用；远程存储时跳过并打印提示）。
- 键：热榜按规范化标题（去空白、全角转半角、小写），跨平台同标题视为同一条；RSS 和独立展示区 RSS 按 URL，缺 URL 时按标题。
- 推送前从热榜、RSS、独立展示区中移除已记录条目，同一次推送内也去重；任一渠道发送成功后写入记录。
- 过滤后没有内容则不推送。

## 4. 调度：定时摘要 + 即时提醒

时段新增两个可选字段（timeline.yaml，可写在 default 或 periods）：

| 字段 | 含义 |
|---|---|
| `push_min_score` | 只推送 AI 相关度不低于该值的条目；非 AI 筛选模式下无分数，该时段不推送 |
| `silent` | Telegram 静音推送 |

本地部署的 custom 时间线：

| 时间 | 行为 |
|---|---|
| 08:00、12:00、18:00、22:00 各 30 分钟 | 摘要：daily 模式 + 去重，即上次推送以来未推过的全部条目；静音；08:00 附 AI 分析 |
| 其余 08:00-23:00 | 即时提醒：incremental 模式，`push_min_score: 0.9`，响铃 |
| 23:00-08:00 | 只采集不推送，早上的摘要会带上夜间内容 |

## 5. RSS 快照

- 源配置 `archive: true` 时，抓取后把条目全文（RSS 的 description/content，未截断的 HTML）存为 `output/archive/<feed_id>/<YYYY-MM>/<guid 摘要>.html`，同一条目只保存首次抓到的版本。
- 页面包含标题、作者、发布时间、原文链接和抓取时间；图片保留远程地址（linux.do 图片在 CDN 上）。
- `output/archive/index.html` 按时间倒序列出快照，可通过 `http://localhost:8089/archive/` 浏览。
- Discourse 的 RSS description 含首帖完整 HTML（不含回复），因此 linux.do 无需再请求帖子页面，也就不受 Cloudflare 质询影响。

## 第二期（未实现）

常驻 bot 服务轮询 getUpdates，在推送下方提供存档、不感兴趣按钮：存档把链接交给快照，不感兴趣用于修正 AI 兴趣描述。
