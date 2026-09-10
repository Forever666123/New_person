# NewPerson 设计文档（v3）

> 目标：做一个接入 Discord 的"虚拟人物"。它不是客服机器人，而是一个"有自己生活"的人：
> 不秒回、睡觉时不回、上班时偶尔瞄一眼手机、有空的时候会主动找你聊天、会发照片。
> 人物背景（persona）后续由使用者填写，代码里只留插槽。
>
> v2 吸收了四路评审（真人感 / 可靠性 / LLM 集成 / 产品）的结论：
> **"看手机"模型代替单峰延迟**、**任务租约与进度持久化**、**Owner 控制命令**。
>
> v3 按实际人物（沈亦宁）落地，三处大改：**作息改成阶段 + 当日变体 + 看手机三层叠加**、
> **新增 style_guard 把关说话风格**、**主动消息类型由人设定义而不是写死**。

## 0. 范围（v1 明确只做什么）

- **一个主要会话**：人物和 Owner（你）的私聊。`conversation_id = "owner"`。
  可选 `PROACTIVE_CHANNEL_ID`：把某个频道也当作同一个会话（只回 Owner 的消息，主动消息发到这里）。
  `ALLOWED_USER_IDS` 保留字段但标记为实验性，不在 README 里宣传。
- 照片来自本地照片库；图片生成只留接口（`ImageGenerator`）与一个可选的外部命令实现。
- 中文优先。

## 1. 总体架构

```
Discord Gateway ──► discord_bot.py（收消息 / 发消息 / 在线状态 / Owner 命令）
                         │
                         ▼
                    App.on_user_message（存库 → attention 决定"什么时候看到、什么时候回" → scheduler）
                         │
                         ▼
                    scheduler.py（持久化任务队列 + 租约 + 每会话单飞：reply / proactive / follow_up / day_plan / memory_update）
                         │
        ┌────────────────┼──────────────────┐
        ▼                ▼                  ▼
   brain.py          life.py            media.py
（调用 Claude：      （每日日程、主动消息    （照片库；按 id/标签挑图；
 回复、主动消息、     候选、follow-up、       30 天冷却）
 日程、记忆更新）     收尾提醒）
        │
        ▼
   delivery.py（气泡拆分、打字模拟、错字后编辑、引用回复、发图、表情反应、被打断检测、进度持久化）
        │
        ▼
   memory.py（SQLite/aiosqlite：消息、会话、事实、日记、任务、照片使用、用量、KV）
```

横切模块：

- `clock.py`：时钟抽象（真实 / 假），`now()` 永远返回 persona 时区的 aware datetime。
- `rhythm.py`：作息 → `sleeping / busy / free / winding_down` + 下一次切换时间。
- `attention.py`（原 timing.py 的升级）：**核心"像真人"逻辑**——"看手机"（glance）过程 + 回复时机 + 防抖 + 疲劳。
- `persona.py` / `config.py` / `models.py`。

## 2. 关键行为规范

### 2.1 作息：阶段 + 当日变体 + 看手机，三层叠加

**不用时刻表，也不用"今天不理人"这种开关。** 长延迟应该是几件事撞在一起的自然结果，
不是某个标志位被打开。所以分三层：

**第一层，阶段（phase）**，跨三到十几天。考试周、赶 project、刚放假，
真人的忙是成片的，不是每天独立掷骰子。阶段序列从一个固定起点确定性推演，
影响整段时间的活跃度和心情提示。

**第二层，当日变体（variant）**，在阶段之上做单日扰动：普通 / 熬夜 / 早起 / 累 / 闲 / 心情差。
变体影响的是**当天**：抽到熬夜就是今晚睡得晚、今天起得晚。

**第三层，看手机（glance）**，见 2.2。

睡觉时刻每天从分布采样，且**起床跟着昨晚走**：熬夜到四点不会七点就起，
但也不会一路睡到下午，因为有课有闹钟。两者按 `sleep_follow_weight` 加权混合，
再把睡眠时长夹进 `[min_hours, max_hours]`。每节课按自己的 `probability` 决定今天去没去。

抽签的随机源是 `(persona.seed, 日期)`，所以同一天反复查询结果一致，进程重启也一致，
但不同的日子互不相关。

于是"她一整天没怎么回"通常是：这周本来就在赶 due，昨晚熬到四点，
他刚好在她睡着的时候发的消息，醒来又赶上有课。三件事叠出来的，不是开关。

**活跃度曲线**取代了 busy 开关。`activity_at(dt)` 返回此刻"会看手机"的程度，0 到 1：
睡着是 0，上课是 `class_activity`，其余按 `activity.points` 阶梯取值，
最后乘当日变体和阶段的倍率。它只决定多久瞄一眼手机，不决定回不回。

状态判定：在睡眠区间 → `sleeping`；在课里 → `busy`；距入睡不足 `winding_down_minutes` → `winding_down`；否则 `free`。

时区只有 `persona.timezone` 一个来源，容器时区无关紧要，所有计算都用 aware datetime。

### 2.2 注意力与回复时机（attention）

真人不是"收到消息后等一个随机时间再回"，而是"隔一阵看一眼手机，看到了就回（偶尔看到了忘了回）"。所以：

**(a) 看手机（glance）过程**。间隔由活跃度决定，而不是由状态查表：

```
间隔中位数 = base_glance_minutes / activity_at(t)
实际间隔  = 中位数 × exp(N(0, glance_sigma))
```

活跃度 0.85 的晚上大约十分钟看一次，活跃度 0.15 的课上大约一小时看一次，
睡着（活跃度 0）就完全不看，顺延到起床。醒来后第一眼：六成的概率在起床后二十五分钟内，
其余落在一个半小时内。`next_glance_after(dt, rng)` 负责这些。

**(b) 回复时机** `plan_reply(now, heat, features, last_user_at, rng) -> TimingDecision`：

- `heat`：`hot` = 双方最后一次交流 < 3 min；`warm` < 45 min；否则 `cold`。
- hot（手机就在手上）：`notice_at = now`，`reply_at = now + LN(20s, σ0.5)`。
- warm：`notice_at = min(next_glance, now + LN(4min, σ0.8))`（刚聊过，手机还在附近）；`reply_at = notice_at + LN(40s, σ0.7)`。
- cold：`notice_at = next_glance`；`reply_at = notice_at + LN(60s, σ0.7)`；**另以 p=0.15 "看了忘了回"**：`reply_at` 推到再下一次 glance + 短滞后（reason 里注明 `forgot_once`）。
- sleeping：按 glance 规则自然落到起床后。**例外**：heat==hot 且入睡不到 20 min → 允许一次快速回复（`quick_before_sleep=True`），上下文告诉模型"你已经准备睡了"。
- **看到了不一定当场处理**：每次 notice 之后按 `DailyRhythm.engage_probability` 掷一次骰子，
  没中就**先放着**，等下一次看手机再说，最多放 `max_defers` 次。
  注意这不是"把消息丢掉"，而是延迟自然被拉长到几小时。真正的"不回"由模型决定（`parts` 为空），
  比如那条消息本来就不需要回。
- **话题模式**：命中 `persona.modes` 的触发词会改变节奏和口吻。交易话题 `delay_multiplier=0.6`，
  她更上心；同时把她记过的台账带进上下文，让她能指出前后矛盾。
- 问题/紧急（`?`/`？`/"吗"/"在吗"/"急"/"快"/"救命"/`!!!`）：所有滞后 × `urgent_multiplier`，最低 8 s。
- 长消息（> 120 字）：`reply_at` 加阅读时间 `len/6` 秒。
- **疲劳**：连续 hot 交流超过 `fatigue_after_minutes`（默认 30）后，每多 15 min 滞后中位数 ×2，并在上下文里提示模型"你们已经聊了一会儿，可以自然收尾去做事"。
- **边界提示**：若 15 min 内将切换到 sleeping/busy 且 heat 为 hot/warm，上下文提示"你 N 分钟后要去睡/上班了，可以自然收尾，需要的话用 follow_up 约晚点聊"。
- 上限：非睡眠原因的总延迟 ≤ `max_delay_hours`（默认 10 h）。
- 最后：相对 now 的所有延迟乘以 `delay_scale`（调试用）。
- `reason` 字段写清每一步，并按固定格式打日志（见 2.10）。

**(c) 防抖与打断**：

- 同会话已有 pending reply：不新建；`reply_at = min(max(reply_at, now + burst_gap), original_reply_at + max_burst_defer)`，`burst_gap ~ LN(25s, σ0.4)`，`max_burst_defer` = 90 s（hot）/ 180 s（其他）。这保证连发不会把回复饿死。
- Discord `on_typing`（对方正在输入）：若 8 s 内对方在打字，把 reply_at 推 8–12 s，同样受上限约束。
- 发送中被打断（对方又发了）：发完当前气泡后停止，已发内容进历史，立即按 hot 规则重新安排一次 reply，并把"你刚说了一半"写进上下文。

**(d) 全局注意力**：人只有一部手机。任何主动动作（reply / proactive / follow_up）执行前都调用 `attention.notice_all(now)`：
把所有未读消息的 notice_at 设为 now。**proactive/follow_up 触发时若存在未读消息或 pending reply** → 不发主动消息，而是把 pending reply 提前到 `now + LN(30s)`，并把主动消息的 note 塞进回复上下文（"你本来想跟 TA 说 xxx"）。

### 2.3 说话风格把关与投递（style_guard + delivery）

**先过关，再投递。** 如果把"不许用 emoji、不许打句号、不许说加油"一条条塞进提示词，
模型会写得非常拘谨，句子变僵，反而更不像人。所以分工是：提示词只描述她是个什么样的人，
`style_guard` 在发出去之前把不符合她习惯的地方拦下来。

拦下来分两种处理：

- **能机械修的**直接修，不惊动模型：句尾句号去掉，句中句号变空格（等于换口气继续说），
  emoji 和感叹号删掉，气泡超量就合并而不是丢内容。
- **不能机械修的**返回违规清单，让模型带着清单重写一次：说了 `never_say` 里的话、
  整句英文、长句超预算。重写还不过就退回机械修剪，不会卡住。

两个刻意的宽松处：问号保留（她问问题是表达在意的方式）；长句按**预算**判定而不是一刀切，
因为"极少数超过二十个字的句子"本身是人设的一部分，出现时说明是重话。

`ReplyPlan.parts` 过关之后进入投递。对每条：

1. `pause_before_seconds > 0` 先静默等待。
2. 打字模拟：时长 = `1.0 + len(text) / cps + N(0, 0.5)`，`cps` 来自 persona（默认 **2.0**，手机打字），限制在 `[1.5, 40]` 秒；用 `async with channel.typing():`（discord.py 会每 ~5 s 自动续），**不要**用 `trigger_typing()`。
   组稿噪声：若时长 > 8 s，以 p=0.4 中途停 2–6 s 再继续（退出并重新进入 typing 上下文）。
3. 发送。超过 2000 字的气泡按句号/换行拆分。
4. **错字后编辑**：若该条带 `typo_text`，先发 `typo_text`，4–20 s 后 `message.edit(content=text)`。
5. **引用回复**：若这批未读消息里最早一条距今 > 2 h，或未读消息 > 3 条且第一条气泡明显是在回某一条（brain 给出 `reply_to_index`），用 Discord reply reference。
6. 照片：`{photo}` 占位的那条带图发送；没有占位但有图 → 最后单独发；有占位没图 → 去掉占位符，去掉后为空则丢弃。
7. 表情反应：`reaction` 对对方最后一条消息 `add_reaction`。可只有反应没有文字。

**进度持久化**：每发出一条，把 `sent_parts` 写进 job 的 `progress_json`。重启后续发（见 2.7）。

**错误分类**：`discord.Forbidden`（50007 无法私聊 / 50001）→ 会话标记 `deliverable=0`，任务 `done(undeliverable)`，life 不再安排 proactive，日志醒目提示"Owner 需要和机器人共享一个服务器并允许私信"。`HTTPException` 5xx / 网络错误 → 交给调度器重试。

### 2.4 主动消息（life）

- **每日日程**：`day_plan` 任务，**去重键** `day_plan:<本地日期>`（`jobs.dedupe_key UNIQUE`，`INSERT OR IGNORE`）。启动时若今天没有日程且现在已醒 → 立刻入队；否则在下一次起床时间 + U(0,10)min 入队。执行：`brain.generate_day_plan()` → 存 `diary` → 生成候选。
- **候选类型**由 `persona.proactive.kinds` 定义，不写死在代码里。沈亦宁的四种是：
  凌晨发一张窗外的照片不配字、说一句自己今天的事、提一句他几天前说过的事、
  问他之前说要做的交易做了没有。每种带权重、时段限制（`hours`）、照片要求（`requires_photo`）、
  同类最小间隔（`min_days_since_last`）。
  **没有"问候"这一类**：她不会说"最近怎么样"，主动的方式是直接说事。
- **抽样**：概率 = `base_probability × unanswered_decay ^ unanswered_initiations`。
  `unanswered_initiations` 是她主动开的话头对方没回的次数，对方任何消息都清零。
  每天最多 `max_unanswered_per_day` 次没被回应的主动开场，总数 ≤ `max_per_day`。
  过滤掉睡眠时段、已过去的时刻、以及 `requires_photo` 但挑不到图的候选。
- **触发时**：sleeping → 丢弃；存在未读/pending reply → 转为提前回复（2.2d）；heat hot → 丢弃；away 模式下只允许 reach_out；否则 `brain.generate_proactive()`，模型可返回 `send=false`。
- **follow-up**：模型在回复里声明 `follow_up{delay_minutes, note}` → `follow_up` 任务；触发时走 proactive 流程，`trigger_kind="follow_up"`。

### 2.5 在线状态（presence）

状态不是时钟的函数，而是"注意力事件"的副产品：

- sleeping → `invisible`。
- 清醒基线：`idle`（手机在口袋里）。busy 时按 persona 用 `idle` 或 `dnd`。
- **手机会话**：每次 glance / reply / proactive 前后 `online` 持续 U(1,8) min；投递期间一直 `online`。
- 自定义状态文字（CustomActivity）**每天最多改一次**，且只有 p=0.3 会改：从 DayPlan.mood 或某个事件里挑一句。不要跟着事件自动轮换。
- 只在状态真的变化时调用 `change_presence`（避免速率限制）。

### 2.6 记忆（memory）

单个 `aiosqlite` 连接，`PRAGMA journal_mode=WAL; synchronous=NORMAL; busy_timeout=5000; foreign_keys=ON`。
所有读-改-写（任务认领、`pending_reply_job_id`、`read_at`、日程创建）用 `BEGIN IMMEDIATE` 事务。
datetime 一律存 ISO8601 含时区偏移的字符串。

表：

- `messages(id, conversation_id, discord_message_id UNIQUE, author_kind{user,bot}, author_id, author_name, content, attachments_json, created_at, read_at, edited_at, deleted)`
- `conversations(id, kind, last_user_message_at, last_bot_message_at, pending_reply_job_id, summary, summary_upto_message_id, unanswered_initiations, last_initiation_date, deliverable, hot_session_started_at)`
- `facts(id, subject{owner,self}, fact, source_message_id, created_at, superseded)`
- `diary(date PRIMARY KEY, status{generating,ready}, day_plan_json, notes_json, status_text)`
- `jobs(id, kind, dedupe_key UNIQUE NULL, run_at, conversation_id, payload_json, status, attempts, lease_until, progress_json, covers_upto_message_id, created_at, original_run_at, reason)`
- `photo_usage(photo_id, sent_at, conversation_id, framed_as_fresh)`
- `usage(day, calls, input_tokens, cache_read_tokens, cache_creation_tokens, output_tokens, estimated_usd)`
- `kv(key, value)`（`paused`、`away_until`、`away_note`、`away_reply_scale`、`chattiness`、`last_status_change_date`、`last_api_error`）

上下文构造：最近 N 条原文 + 更早部分滚动摘要（未摘要消息超过阈值时入队 `memory_update`）+ facts + 今日 diary（日程 + notes）。

### 2.7 调度器（scheduler）

- **认领**：`UPDATE jobs SET status='running', lease_until=now+300s WHERE id=? AND status='pending'`，rowcount==1 才执行。
- **每会话单飞**：`asyncio.Lock` per conversation；同会话已有 running 任务时不认领第二个。
- **租约清扫**：每轮把 `running` 且 `lease_until < now` 的任务改回 `pending`（进程崩溃恢复）。
- **失败重试**：最多 3 次，间隔 1 / 3 / 9 min（× delay_scale）；API 失败对用户表现为"这会儿没看手机"，不发任何错误文本。
- **过期策略**（启动时）：reply 任务过期 → 立刻执行（notice_all）；proactive/follow_up 过期 > 2 h → `cancelled(stale)`；day_plan 过期 → 立刻执行。
- **续发**：reply 任务的 `progress_json` 里有 `plan` 与 `sent_parts`，重启后从第 `sent_parts` 条继续，不重新调模型。
- **认领快照**：reply 任务认领时记录 `covers_upto_message_id = MAX(id of unread)`；投递完成后若出现更大 id 的未读 → 立即按 hot 规则安排新的 reply。
- `paused`（kv）为真时：reply 照常入队但不执行（状态里显示"已暂停"）；proactive 不生成。

### 2.8 大脑（brain / prompts）

- `anthropic.AsyncAnthropic`；模型默认 `claude-opus-5`；`output_config={"effort": settings.effort}`；不传 `thinking`。
- 结构化输出：`client.messages.parse(output_format=<pydantic>)`。
- **提示词缓存**：system 列表 = [稳定块（人物设定 + 说话规范 + 输出规范），`cache_control: ephemeral`]。易变内容全部放 user 消息。稳定块里**不能**有时间/日期/随机数。
- 对方发的图片（image/*，≤ 5 MB）下载后作为 image block 传入。
- 错误链：`RateLimitError` → `APIStatusError`（≥500 可重试）→ `APIConnectionError`；全部返回 `None`；`stop_reason == "refusal"` → `None`。记录 `last_api_error` 到 kv。
- **用量**：每次调用把 usage 写进 `usage` 表并打日志；`MAX_CALLS_PER_DAY`（默认 200）超过后所有调用视为失败（"没看手机"），任务推到明天起床后。
- 说话规范（稳定 system）：像真人发 IM：短句、口语、不用 markdown / 列表、不长篇；可以只回表情反应或不回（parts 为空）；**照片只能从"可用照片"列表里按 id 选**，文字要与照片 caption 一致，用 `{photo}` 标记配图那条；不要许诺发不出来的照片；`never_admit_ai` 为真时永远不说自己是 AI/模型/程序；不要复述已经说过的事；不要每次都问问题。
- 回复上下文必须包含：现在几点、你在干什么（当前/最近事件）、**距对方发消息过去了多久以及你这段时间在做什么**（解释为什么现在才回，但不要每次都解释）、今日日记（含已经主动发过的内容，避免重复）、对话摘要、事实、最近对话、这次要回的未读消息、可用照片列表、边界/疲劳提示、被打断/本来想说的 note。

### 2.9 照片（media）

- `persona/photos/index.yaml`：`{id, file, tags, caption, taken_hint, time_of_day?, location?, freshness?{evergreen,dated}}`。
- 候选列表 = 全库 − 30 天内发过的 − （若指定 `time_of_day`）与当前时段不符的。候选列表（id / caption / tags / taken_hint）作为文本进入 brain 上下文，brain 直接用 `photo_id` 选。
- `PhotoRequest{photo_id?, tags, description}`：按 id → 按标签 → （可选）生成器。都没有 → 文字里去掉 `{photo}`。
- `python -m newperson photos scan`：扫描目录，为没登记的图片生成 index 条目草稿。

### 2.10 Owner 控制命令与日志

在私聊里以 `!np` 开头的消息是控制命令，**不存库、不进模型**：

| 命令 | 作用 |
|---|---|
| `!np status` | 作息状态与结束时间、热度、pending 任务（时间 + reason）、今日调用次数/估算费用、最近 API 错误、paused/away |
| `!np now` | 把 pending reply 提前到现在 |
| `!np pause` / `!np resume` | 暂停/恢复（暂停时不回不主动，消息照常记录） |
| `!np away <说明> [--until YYYY-MM-DD] [--scale 3]` / `!np back` | 出差/生病模式：作息视为 busy（标题=说明），延迟 × scale，主动消息只保留 reach_out，说明注入上下文 |
| `!np chatty <0.2~2>` | 主动消息概率倍率 |
| `!np plan` | 打印今天的日程 |
| `!np help` | 列出命令 |

结构化日志（固定前缀，便于 grep）：

```
[inbox] conv=owner msg=123 len=14 q=yes urgent=no image=no
[timing] conv=owner state=busy(until 18:00) heat=cold notice=17:42 reply=17:44 reason="glance@17:42; lag 63s"
[job] fired reply#42 unread=3 covers_upto=130
[brain] reply parts=2 reaction=😂 photo=lunch-001 follow_up=no in=3.1k cached=2.7k out=180 ms=4200
[delivery] sent 2/2 photo=yes interrupted=no
[proactive] dropped: unread pending -> merged into reply#43
[presence] online (phone session 4m)
```

## 3. 数据结构（models.py）

见 `newperson/models.py`。相较 v1 新增：`ReplyPart.typo_text`、`ReplyPlan.reply_to_index`、`PhotoRequest.photo_id`、`Job.dedupe_key/lease_until/progress/covers_upto_message_id/original_run_at/reason`、`Photo.time_of_day/location/freshness`、`ResolvedPhoto.is_fresh`、`UsageRecord`。

## 4. 配置

- 运行配置：`.env`（见 `.env.example`）→ `config.Settings`。新增 `MAX_CALLS_PER_DAY`、`ALLOW_PLACEHOLDERS`。
- 人物配置：`persona/persona.yaml`（见 `persona/persona.example.yaml`）。新增 `timing.typing_chars_per_second=2.0`、`timing.fatigue_after_minutes=30`、`timing.forgot_probability=0.15`、`timing.typo_probability=0.07`。
- `check`：`background` / `speaking_style` 仍含【待填】→ 失败；其他占位 → 警告。`run` 在有【待填】时拒绝启动，除非 `--allow-placeholders`。

## 5. 命令行

```
python -m newperson run [--allow-placeholders]   # 启动机器人
python -m newperson check [--online]             # 检查配置；--online 会登录 Discord 验证 token、privileged intent、能否私聊 Owner
python -m newperson simulate [--days N --seed S --messages-per-day M] [--from-db]   # 用假时钟模拟回复时机；--from-db 回放库里真实消息
python -m newperson plan                          # 生成今日日程并打印（联网）
python -m newperson photos scan                   # 为照片目录生成 index 草稿
```

## 6. 首次接入 Discord（README 必须包含）

1. Developer Portal 建应用 → Bot → **打开 MESSAGE CONTENT INTENT**（privileged；不开的话 `message.content` 永远是空串）。
2. OAuth2 → URL Generator：scope `bot`；权限 `Send Messages`、`Read Message History`、`Attach Files`、`Add Reactions`、`View Channels`。
3. 用生成的链接把机器人邀请进**你自己的一个服务器**——机器人只能私聊与它共享服务器的用户。
4. 你的隐私设置要允许"来自服务器成员的私信"。
5. 开启 Discord 开发者模式，复制你的用户 ID 填到 `OWNER_DISCORD_USER_ID`。
6. `python -m newperson check --online` 验证。

## 7. 测试策略

- `rhythm`：跨午夜、周五夜按工作日、周日夜按周末、winding_down 边界。
- `attention`：固定种子；glance 序列落在清醒时段；hot/warm/cold 的分布区间；forgot 分支；sleeping 推迟；quick_before_sleep；防抖上限不饿死；疲劳倍率；边界提示；delay_scale。
- `memory`/`scheduler`：临时 SQLite；租约认领只成功一次；清扫过期租约；重启恢复 running；dedupe_key；过期策略；续发从 sent_parts 开始；每会话单飞。
- `brain`：假 client；system 稳定块字节级不变；cache_control 存在；refusal → None；错误链；用量记录与日限。
- `delivery`：假 channel；顺序、打字时长、打断、错字编辑、引用、2000 字拆分、{photo} 处理、Forbidden 分类。
- `life`/`media`：候选抽样、未回应衰减、每日上限、sign_off、照片 30 天冷却与 time_of_day 过滤。
- `discord_bot`：权限过滤（bot/self/非 owner）、`!np` 命令不入库、edit/delete 处理、on_typing 推迟。

## 8. 部署

Docker（`Dockerfile` + `docker-compose.yml`），`./persona` 只读挂载，`./data` 读写挂载。或 `pip install -e . && python -m newperson run`。

## 9. 代码约定

- Python 3.11+，asyncio，类型标注；标识符英文，注释/日志/README 中文。
- 不裸调 `datetime.now()`，一律 `Clock`。所有随机走传入的 `random.Random`。
- 每个模块顶部 docstring 说明职责；公共函数写 docstring。
