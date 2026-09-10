# NewPerson 设计文档

> 目标：做一个接入 Discord 的"虚拟人物"。它不是客服机器人，而是一个"有自己生活"的人：
> 不秒回、睡觉时不回、上班时偶尔瞄一眼手机、有空的时候会主动找你聊天、会发照片。
> 人物背景（persona）后续由使用者填写，代码里只留插槽。

## 1. 总体架构

```
Discord Gateway ──► discord_bot.py（收消息 / 发消息 / 在线状态）
                         │
                         ▼
                    inbox（把消息存进 memory，请 timing 决定"什么时候看到、什么时候回"）
                         │
                         ▼
                    scheduler.py（持久化的定时任务队列：reply / proactive / follow_up / day_plan / presence）
                         │
        ┌────────────────┼──────────────────┐
        ▼                ▼                  ▼
   brain.py          life.py            media.py
（调用 Claude，     （每天生成"今日日程"，   （照片库 / 可选的图片生成）
 生成回复、主动      从日程里挑"值得分享
 消息、日程、记忆）   的时刻"变成主动消息）
        │
        ▼
   delivery.py（把一条回复拆成几个气泡，模拟打字时长，逐条发送；发照片；加表情反应）
        │
        ▼
   memory.py（SQLite：消息记录、对话摘要、用户事实、人物日记、任务队列、KV 状态）
```

横切模块：

- `clock.py`：时钟抽象（真实 / 测试用假时钟），统一带时区的 `now()`。
- `rhythm.py`：人物作息 → 任一时刻处于 `sleeping / busy / free / winding_down` 哪个状态，以及下一次状态切换时间。
- `timing.py`：**核心的"像真人"逻辑**：给定作息状态、对话热度、消息特征，决定"什么时候看到消息"和"什么时候回"。纯函数 + 注入的随机源，可测试。
- `persona.py`：加载 `persona/persona.yaml`。
- `config.py`：从环境变量 / `.env` 读取运行配置。
- `models.py`：所有跨模块的数据结构（pydantic）。

## 2. 关键行为规范

### 2.1 作息状态（rhythm）

persona.yaml 中按"工作日 / 周末"分别定义 `sleep`（入睡、起床时间）和 `busy`（不方便看手机的时间段，如上班、上课）。

状态判定（按优先级）：

1. 处于 sleep 区间 → `sleeping`
2. 处于任一 busy 区间 → `busy`
3. 距离入睡不到 `winding_down_minutes`（默认 60）→ `winding_down`
4. 否则 → `free`

`rhythm.state_at(dt) -> RhythmSnapshot(state, block_end, next_wake, ...)`。跨午夜的区间（如 23:30–07:30）必须正确处理。

### 2.2 回复时机（timing）

输入：

- `now`、作息快照
- 对话热度 `heat`：按"双方最后一次交流距今"计算：`hot` < 3 分钟，`warm` < 45 分钟，否则 `cold`
- 消息特征：是否像问题（含 `?`/`？`/"吗"/"在吗"）、是否显得紧急（"急"、"快"、"救命"、"!!!"）、字数
- 随机源 `random.Random`

输出：`TimingDecision(notice_at, reply_at, reason)`。

规则（延迟用对数正态分布采样，单位秒；`median` 中位数，`sigma` 离散度）：

| 状态 | hot | warm | cold |
|---|---|---|---|
| free | median 20s, σ0.5 | median 4min, σ0.8 | median 20min, σ0.9 |
| winding_down | 30s | 6min | 25min |
| busy | 60s | 15min | 40min，且上限为 busy 区间结束 + U(2,15)min |
| sleeping | 见下 | 见下 | 见下 |

- `sleeping`：`notice_at = 起床时间 + U(5, 45)min`。例外：如果 `heat == hot` 且刚入睡不到 20 分钟，允许一次快速回复（U(30s, 3min)），并在上下文里告诉模型"你已经准备睡了"。
- 问题/紧急 → 延迟 × 0.6（最低 8s）。
- 长消息（> 120 字）→ 额外加阅读时间 `len/6` 秒。
- 如果算出的 `reply_at` 落在睡眠区间 → 推到起床 + U(5,45)min。
- 防抖（对方正在连发）：`reply_at = max(reply_at, last_user_message_at + burst_gap)`，`burst_gap` hot 时 25s，否则 45s。
- 上限：任何情况下延迟不超过 `max_delay_hours`（默认 10 小时，睡眠除外）。
- 全局 `delay_scale`（配置）把所有延迟按比例缩放，用于调试。

### 2.3 消息合并与打断

- 同一会话已有待执行的 reply 任务时，**不再新建**，只把 `reply_at` 至少推到 `now + burst_gap`，并让任务执行时读取"所有未读消息"一起回。
- 发送过程中（正在逐条发气泡）如果对方又发了新消息：发完当前这条后**停止**剩余气泡，把已发的记入历史，立即按 hot 规则安排一次新的回复。这就是真人"啊你回了，我刚想说……"的效果。

### 2.4 投递（delivery）

`ReplyPlan.parts` 是若干条短气泡。对每条：

1. 若 `pause_before_seconds > 0` 先静默等待（人在想）。
2. 显示"正在输入…"，时长 = `1.0 + len(text) / cps + N(0, 0.5)`，cps 默认 3.5 字/秒（中文）；限制在 `[1.5, 40]` 秒内。
3. 发送。
4. 如果计划里有 `photo_request`，在文字之后（或按 `photo_position`）发照片；照片来自 `media.resolve()`。
5. 如果计划里有 `reaction`，给对方最后一条消息加表情反应（可与文字并存，也可只有反应没有文字）。

投递时使用 `delay_scale` 缩放等待与打字时长。

### 2.5 主动消息（life）

- **每日日程**：每天第一次醒来（或程序启动时当天还没有）→ `brain.generate_day_plan()` 生成今天的 `DayPlan`（事件列表、心情、心里挂念的事）。存入 `diary`。
- **候选时刻**：日程里 `shareable=True` 的事件 → 在 `[start, end]` 内随机一个时刻作为候选；另外在 free 时段随机 1 个"随便聊聊"的候选；如果和对方已经 `reach_out_after_silent_days` 天没聊 → 一个"想起你了"候选。
- **抽样**：每个候选以 `base_probability × 关系修正` 概率保留，总数不超过 `max_per_day`，全部作为 `proactive` 任务写入 scheduler。
- **触发时**：若当前 `sleeping` → 丢弃；若对话正 `hot`（对方正在聊）→ 丢弃（人不会在聊天中"突然主动开话题"，那是回复的事）；否则调用 `brain.generate_proactive()`，模型可以返回 `None` 表示"其实没啥想说的"。
- **follow-up**：模型在回复里可以声明 `follow_up: {delay_minutes, note}`（如"我晚点查一下再告诉你"），系统按时创建一个 `follow_up` 任务，触发时同样走 `generate_proactive`，把 `note` 作为触发原因。

### 2.6 在线状态（presence）

每 60 秒计算一次作息状态，变化时调用 `change_presence`：

| 状态 | Discord status | activity |
|---|---|---|
| sleeping | `invisible`（看起来离线） | 无 |
| busy | `idle` 或 `dnd`（persona 可配） | CustomActivity(当前事件标题，如"上班中") |
| free | `online` | CustomActivity(日程里当前/最近事件，如"在听歌") |
| winding_down | `idle` | CustomActivity("准备睡了") |

### 2.7 记忆（memory）

SQLite 表：

- `messages(id, conversation_id, discord_message_id, author_kind{user,bot}, author_id, content, attachments_json, created_at, read_at)`
- `conversations(id, kind{dm,channel}, last_user_message_at, last_bot_message_at, pending_reply_job_id, summary, summary_upto_message_id)`
- `facts(id, subject{user,self}, fact, source_message_id, created_at, superseded)` — 从对话里抽取的稳定事实（对方叫什么、喜欢什么、约好的事）
- `diary(date, day_plan_json, notes_json)` — 人物"今天做了什么"，保证主动消息与回复口径一致
- `jobs(id, kind, run_at, conversation_id, payload_json, status{pending,running,done,failed}, created_at, attempts)`
- `photo_usage(photo_id, sent_at, conversation_id)`
- `kv(key, value)`

上下文构造：最近 N 条消息原文 + 更早部分的滚动摘要（超过阈值时由 `brain.summarize()` 更新）+ facts + 今日 diary。

### 2.8 大脑（brain / prompts）

- 使用 Anthropic Python SDK（`AsyncAnthropic`），模型默认 `claude-opus-5`，`output_config={"effort": settings.effort}`，不传 `thinking`（Opus 5 默认自适应思考）。
- 结构化输出：`client.messages.parse(output_format=ReplyPlan)` 等，返回 pydantic 对象。
- **提示词缓存**：system 由两部分组成，稳定部分（人物设定、说话规则、输出规范）放前面并加 `cache_control: {"type": "ephemeral"}`；易变部分（当前时间、作息状态、今日日程、记忆摘要、未读消息）放在 user 消息里。
- 对方发的图片：下载（≤ 5MB，image/*）后作为 image block 传入，让人物"看得到"。
- 错误处理链：`RateLimitError` → `APIStatusError`（>=500 重试，否则记录）→ `APIConnectionError`。任何失败都**不要**让人物发出"系统错误"之类的话；失败就当作"这会儿没看手机"，任务延后重试（最多 3 次，间隔递增）。
- `stop_reason == "refusal"` → 视为没回复。
- 说话规范（写进稳定 system）：像真人发 IM 一样短句、口语、可以有错别字与语气词但不要过火、不用 markdown、不用列表、不要一次发很长的一段、可以只回一个表情反应、可以不回（`parts` 为空）。**永远不要**说自己是 AI / 模型 / 程序，除非 persona 里明确允许。

### 2.9 照片（media）

- `persona/photos/index.yaml`：`[{id, file, tags: [...], caption, taken_hint}]`。
- `PhotoLibrary.pick(tags, exclude_ids) -> Photo|None`：按标签重合度排序，优先没发过的；同分随机。
- `ImageGenerator` 协议：`async generate(prompt) -> Path|None`。内置 `CommandImageGenerator`（调用用户配置的外部命令）和 `NullImageGenerator`。
- `MediaService.resolve(PhotoRequest) -> ResolvedPhoto|None`：先查库，库里没有再尝试生成（若配置了）；都没有则返回 `None`，此时投递方**只发文字，不提照片**（brain 生成的文字里如果说"给你看看"会很尴尬，所以 brain 提示词要求：把"要发照片"写在 `photo_request`，文字里用 `{photo}` 占位——没图时把含占位符的那条气泡改写/丢弃）。

## 3. 数据结构（models.py）

见 `newperson/models.py`，此处只列关键类型：

- `RhythmState = Literal["sleeping","busy","free","winding_down"]`
- `RhythmSnapshot(state, since, until, next_wake, current_block_title)`
- `Heat = Literal["hot","warm","cold"]`
- `MessageFeatures(is_question, is_urgent, length)`
- `TimingDecision(notice_at, reply_at, reason)`
- `IncomingMessage(conversation_id, discord_message_id, author_id, content, attachments, created_at)`
- `ReplyPart(text, pause_before_seconds)`
- `PhotoRequest(tags, description)`
- `FollowUp(delay_minutes, note)`
- `ReplyPlan(parts, reaction, photo_request, follow_up, inner_note)`
- `ProactivePlan(parts, photo_request, inner_note)`
- `PlanEvent(start, end, title, detail, shareable, share_hint, photo_tags)`
- `DayPlan(date, mood, events, thoughts)`
- `Job(id, kind, run_at, conversation_id, payload, status, attempts)`

## 4. 配置

- 运行配置：`.env` / 环境变量（见 `.env.example`），由 `config.Settings` 加载。
- 人物配置：`persona/persona.yaml`（见 `persona/persona.example.yaml`），由 `persona.Persona` 加载。
- 照片索引：`persona/photos/index.yaml`。

## 5. 命令行

```
python -m newperson run        # 启动机器人
python -m newperson check      # 检查配置、人设、照片索引、Discord/Anthropic 凭据是否齐全（不联网）
python -m newperson simulate   # 用假时钟跑 N 天，打印"如果对方在这些时间发消息，人物会在什么时候回"
python -m newperson plan       # 调一次模型生成今天的日程并打印（联网）
```

## 6. 测试策略

- `timing`/`rhythm`：固定种子 + 假时钟，断言分布落在区间内、跨午夜正确、防抖正确、睡眠推迟正确。
- `scheduler`/`memory`：临时 SQLite，断言持久化、重启恢复、到期执行、失败重试。
- `brain`：注入假的 Anthropic client（返回预置的 pydantic 对象），断言提示词组装、缓存标记、错误处理。
- `delivery`/`discord_bot`：假的 channel/message 对象，断言气泡顺序、打字时长、打断逻辑、权限过滤。
- `life`/`media`：固定种子，断言候选抽样、每日上限、照片挑选与去重。

## 7. 部署

Docker（`Dockerfile` + `docker-compose.yml`），数据目录挂载到 `./data`。也可以直接 `pip install -e . && python -m newperson run`。

## 8. 代码约定

- Python 3.11+，asyncio，类型标注。
- 标识符英文；注释、docstring、日志、README 用中文。
- 不使用 `datetime.now()` 裸调用，一律走 `Clock`。
- 所有随机都通过传入的 `random.Random`，方便测试。
