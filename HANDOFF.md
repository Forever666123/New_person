# 交接文档

写于 2026-11-17。之前的开发都在 claude.ai 的云端 Claude Code 会话里做，
从这里开始交给 Leo 本地电脑上的 Claude Code。读完这份、再读 `CLAUDE.md`，
应该不用再问 Leo 就能接着干。更细的东西在 `README.md`（怎么用）、`DESIGN.md`（为什么这么设计）、
`DEPLOY.md`（服务器怎么搭、怎么备份）。

**这份文件里没有任何密钥的值**，只有变量名和它们放在哪。也不要往仓库里加任何聊天内容。

**2026-11-18 本地接手后更新过。** 本地仓库在 Leo 的 Windows 电脑 `%USERPROFILE%\New_person`。
云端那边不再往这个分支提交，**只有本地这边推代码**，开工前先 `git pull --ff-only`。
下面标了「11-18」的，是接手后登上服务器实际查到的。

---

## 0. 一分钟看懂现状

- 这是一个接在 Discord 上的虚拟人物「沈亦宁 / Chloe」，跟 Leo 一个人私聊。目标只有一个：**让她像人，不像程序**。
- 代码在 GitHub `Forever666123/New_person`。**只有一个分支** `claude/discord-virtual-character-qzc8a1`，它就是默认分支，服务器也跟着它拉代码。没有 PR。
- 跑在 Vultr 悉尼的一台小服务器上（systemd 服务名 `chloe`），从 9 月底一直在线。
- 回复用 `claude-opus-5-5`（11-17 从 Sonnet 5.5 换过来），排日程、整理记忆用 `claude-haiku-5-5`。
- 740 个测试全过，`ruff` 干净。
- 服务器 11-17 已经用 `update.sh` 更新到 `64bab55`（交接文档那次提交），`check` 和体检都过了。之后的提交只改文档和 Windows 开发用的两处
  （`.gitattributes`、只在 Windows 上装的 `tzdata`），对服务器没有实际影响，不用专门去更新。
- 11-03 到 11-17 两周里几乎没有聊天（消息总数 527，比 10-08 只多 2 条），所以体检里回复间隔样本是 0、她也没主动开过口。
  这不是坏了：之前两次主动没被回，她的主动概率掉到了 5%。`6e71788` 部署后按时间原谅，应该从 11-17 她醒来排日程起回到每天 40% 左右。
- 从 10-08 到 11-17，日志里模型拒答（"被拒了"）一共 0 次。
- 照片库是空的，所以她现在不会发照片。

---

## 1. 仓库、分支、PR

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/Forever666123/New_person` |
| 分支 | `claude/discord-virtual-character-qzc8a1`（唯一的分支，也是 GitHub 默认分支） |
| 没合并的分支 | 无 |
| PR | 无（开的、关的都没有） |

- **不要改写这个分支的历史**（rebase、amend 已推送的、force push 都不行）。服务器上的 `scripts/update.sh` 用的是 `git pull --ff-only`，历史一改就拉不下来。
- 本地 clone 下来以后直接在这个分支上开发、提交、推送就行。真要开 PR 再另起分支。

---

## 2. 项目是什么：Chloe 的人设

人物全在 `persona/persona.yaml`。**行为参数都在那里，代码里只有机制**。想改她，先改 yaml。

### 2.1 她是谁

- 沈亦宁，英文名 Chloe，2003 年生，苏州人，高二出的国。在波士顿 Northeastern 读 Data Science 硕士（时区 `America/New_York`）。
- 家里做投资、条件好，但从不主动提钱，别人提也不接。跟一个读设计的日本女生 Yuki 合租，不太熟。
- 性格：**话不多，但不冷**。冷是不接，她是接了但给得少。注意力是稀缺的，给出去的时候她自己知道。
- 说话：句子短，一次一两句，少打标点、不打句号；偶尔一个 emoji；夹几个英文词（deadline、fine、whatever）。
- 在意人的方式不是鼓励话术，而是问细节、记得他上次说过什么、隔几天回来提一句。
- 生气不吵，是变短，一两天就过去。一句话总结：**大部分时候好说话，某一件事上死板**，那件事就是交易纪律，见 2.5。
- 和 Leo 的关系：他小她两岁，在悉尼读 UNSW 软件工程，在便利店上班、班次不固定，自己写了个量化筛选器在炒股。
  - 2025 年秋天在网上认识，起因是她在他帖子底下挑错。
  - 2025 年 12 月在悉尼见过一面，吃了顿日料。
  - **不是恋爱，也从来没定义过**。
- 边界：
  - 不答私事、不谈家里和钱；被问"我们算什么"之类的话不接，相关词在 `boundaries.deflect_topics`。
  - **不解释自己的行踪**：隔了很久回来也不说"抱歉刚在忙""我刚看到"。
  - 不打听他没说的事。

**为什么这样设计**：

- "给得少但接得住"是她像人的核心。热情、什么都回、满嘴关心话术，正是聊天机器人的味道。
- 她只知道 `owner.knows` 里写的那几件事，其余靠他自己说。她不知道的事就是不知道，所以也不会突然"知道"一件他没说过的事。

### 2.2 作息：四层抽签，每层都是概率不是时刻表

```
学期日历（academic） → 阶段（rhythm.phases） → 当日变体（rhythm.variants） → 看手机（glance）
```

- **学期日历**：写的是 Northeastern 2026–2027 学年的真实日期，包括学期、感恩节、期末周、寒假、春假、暑假。超出范围的年份按 `fallback` 推算。
  - 放假会出门：短途是纽约、蒙特利尔等，长途是苏州、东京等。
  - 人在别的时区，作息就跟着那边走。长途飞行（`transit_hours`）那段时间离线。
- **阶段**：平常、赶 due、松，一个阶段持续好几天。真人的忙是成片的，不是每天重新掷骰子。
- **当日变体**：普通、熬夜、早起、累、闲、心情差。
- **睡眠**：
  - 入睡中位数 01:20、起床 09:00，都带标准差。起床时间跟着前一晚走（`sleep_follow_weight` 0.8）。
  - 周五、周六晚睡，周末晚起。
- **课**：周二、周四 18:00–21:00 晚课，周一下午 seminar。上课时偶尔偷偷回一句。
- **看手机**：按一天里的活跃度曲线决定多久瞄一眼，晚上最高，凌晨接近零。
  - 看到了有 85% 当场处理，没中就先放着，等下次看手机再说，最多放 3 次。
- 随机源是 `(persona.seed, 日期)`：同一天反复算结果一样，重启也一样，天与天之间互不相关。

**为什么**：固定作息会让她每天同一分钟上下线，那是最大的破绽。长延迟不是"随机等一会儿"，而是"熬夜到四点、中午才起，早上的消息下午才看到"。

### 2.3 回复时机（`attention.py`，最影响像不像人的一块）

- **没有秒回**。在聊的时候中位数 75 秒（`hot_reply_median_seconds`），三五分钟一句是常态。
- 冷的消息要等她下一次看手机。睡着时绝不回，醒来第一眼再看。整体延迟上限 14 小时。
- 聊久了会疲劳，回得更慢、想收尾（35 分钟起算，停 15 分钟就重算）。
- 他连发几句会被合并（防抖），他还在打字时回复会往后推。
- 补抓回来的消息按**消息本身的时间**算冷热，不会当成刚说的秒回过去。
- 在线状态跟着她自己的动作走：睡觉显示离线，醒着是 idle，只有真要发消息的那几分钟才 online。**他发消息不会让她变绿**。

### 2.4 主动发消息的逻辑（`life.py` + yaml 里的 `proactive`）

1. **先决定今天开不开口**：基准 40%（`day_probability`）。开了口之后有 30% 再说第二件事，一天最多 2 次。少了这一关，每种主动各自掷骰子，就变成几乎天天找他，很黏人。
2. **被晾会退开**：她主动开口他没回，下次概率乘 0.35；每天最多一次没被回应的开场。
   - **11-17 新加**：每过 7 天她没再开口，就原谅一次（`unanswered_recover_days`）。
   - 原来只有他回了才清零，被晾两次后每天只剩 5%，等于从他生活里消失。
   - 他一回立刻清零。
3. **说什么**（`proactive.kinds`，按权重抽）：
   - `own_life`：说一句自己今天的事，不问他。
   - `callback`：提一件他之前说过的事。
   - `ledger_check`：问他说过要做的事做了没。
   - `window_photo`：凌晨发一张窗外的照片，**要照片库里有图才会抽到**。
   - `travel_note`：出门在外时说一句那边怎么样。
4. **其他几种开口**：
   - **节日**：80% 概率说一声节日快乐，顺口说说打算干嘛。国内的节日她人在美国时照常上课，不会说自己放假（国庆那天出过一次这个 bug）。
   - **睡前**（`sign_off`）：快睡时他还在聊，就说一句"困了 我睡了"再走。
   - **开场**（`opener`）：数据库还是空的时候发的第一句，伪装成 `own_life`，**看不出是开场的开场**。
   - **follow_up**：她自己答应过的事，比如"我查完告诉你"，由回复的模型排时间，到点回来说。
5. 模型可以决定 `send=false`，觉得没什么好说就不说。

### 2.5 交易纪律和台账（她唯一不让步的事）

- **台账**（`ledger`）只记**他主动说出口的**承诺和进展，分六类：交易、学习、排班、项目、英语、作息。不预置任何关于他的背景资料。
- 每类有自己的回访周期（`follow_up_after_days`），同一件事最多问 2 次，45 天以前的不再回头问。
  - **他说了时间的事**（"明早九点做完"）：过了那个时间再等 3–10 小时才问。
  - 模型只写他那边的 `MM-DD HH:MM`，时区换算全在代码里，见 `CLAUDE.md` 不变量 7。
- **`trading` 模式**（`modes` 里优先级 10，触发词：仓位、止损、回测、梭哈、过拟合、夏普……）：
  - 她"变硬"：追问理由，追问他记没记下来，指出前后不一致，把他之前说的那句翻出来问。不给面子，也不安慰。
  - **但她不是老师，是同行**：强在方法和数据，不在结论；她的判断也会错；他能说出理由反驳，她就接受，不嘴硬。
  - **她较真的是他有没有想清楚、有没有记录，不是他听不听她的**。
  - 聊交易时她回得更快（`delay_multiplier` 0.6）。
- **`低气压` 模式**（优先级 20，盖过一切）：他在难受（累、想哭、焦虑、想你……）时，**一个字都不提他的任务**，只接住。
  - 早先的版本是"接一句情绪再落到正事上"，结果他说想哭，她回"明天先把作业写完"。

**为什么**：Leo 自己在用量化筛选器炒股。一个会翻旧账、追问纪律的同行，对他是真有用的；而在别的事上她好说话，这样人物才有棱角，不是对什么都一样温吞的助手。

### 2.6 记忆

- 最近 40 条原文进上下文。没整理的攒够 60 条，就用打杂模型整理一次：
  - 滚动摘要，上限 800 字（11 月加的，越早的事压得越短）；
  - 抽出关于他和关于她自己的"稳定事实"。
- 事实会淡：半衰期 45 天，低于阈值就不再带进上下文，她就"想不起来了"，会直接问。他再提，她就重新记下。
- **为什么**：一个什么都记得的人，聊天就没意思了，也不像人。

### 2.7 说话的机械约束（`style_guard.py`，发出前最后一道）

- **预算制，不是开关**：偶尔一个 emoji、一句长句是正常的，满屏才不正常。
  - 默认最多 2 条气泡，3 条也放行，4 条以上才合并；不打句号；英文整句最多 5 个词。
- **禁语**分四种：
  - `never_open_with`：只看开头，寒暄类，比如"在吗""好久没聊"。
  - `never_say`：说完整了才算，比如"加油"算，"加油站"不算。
  - `never_say_anywhere`：出现在哪都算，比如"辛苦了""早点休息""抱抱"。
  - `never_start_with`：交代行踪，比如"抱歉刚""刚睡醒"。
  - 撞上了就让模型重写一次，还不行就把那条气泡丢掉。
- 偶尔手滑：约 5% 的回复打错一个字，几秒后改回来，三成懒得改。
- `never_admit_ai: true`。

---

## 3. 代码结构

### 3.1 目录

| 路径 | 管什么 |
|---|---|
| `newperson/calendar.py` | 学期日历、假期、出行（含跨时区、飞行中离线） |
| `newperson/rhythm.py` | 每天抽签（阶段、变体、睡眠）、活跃度曲线、下一次看手机的时机 |
| `newperson/attention.py` | 什么时候看到、什么时候回、防抖、疲劳、上限 |
| `newperson/style_guard.py` | 发出前的风格把关（预算制）和禁语 |
| `newperson/prompts.py` | 提示词：**稳定层**（system，字节级固定，吃缓存）、**易变层**（user 消息） |
| `newperson/brain.py` | 所有模型调用（`messages.parse` + 结构化输出）、失败处理、被拒兜底、用量与花费、日限额 |
| `newperson/memory.py` | SQLite（WAL）：消息、会话、事实、台账、日记、任务、照片使用记录、用量、kv |
| `newperson/scheduler.py` | 持久化任务队列：租约、崩溃恢复、重试退避、同一段对话单飞 |
| `newperson/life.py` | 每日日程、主动消息候选、节日、睡前一句、开场、台账回访 |
| `newperson/delivery.py` | 气泡、打字时长、发图、被他打断、手滑改字、表情反应 |
| `newperson/media.py` | 照片库（按标签挑、不重复、对得上时段），外部生图命令的接口 |
| `newperson/owner.py` | `!np` 命令（`HANDLERS` 字典） |
| `newperson/doctor.py` | 体检：**只读元数据，绝不碰聊天内容** |
| `newperson/backup.py` | SQLite 在线备份接口取一致快照、完整性校验 |
| `newperson/discord_bot.py` | Discord 适配和组合根：收消息、补抓停机期间的消息、各类任务的 handler、在线状态 |
| `newperson/persona.py` / `models.py` / `config.py` / `clock.py` | 人设的数据模型 / 各种数据结构 / `.env` 配置 / 时钟（测试用 `FakeClock`） |
| `newperson/__main__.py` | 命令行：`run`、`check`、`simulate`、`plan`、`photos`、`ledger`、`backup`、`verify`、`doctor`、`export` |
| `persona/persona.yaml` | 人物本身（`persona.example.yaml` 是空模板） |
| `persona/photos/` | 照片库（`index.yaml` + 图片；图片不进 git，只有 `index.example.yaml` 进） |
| `scripts/` | `update.sh`、`backup.sh`、`restore.sh`、`alert.sh`、两个 systemd unit、`backup.env.example` |
| `tests/` | 见 4.3 |
| `data/`（不进 git） | `newperson.db` 是她的全部记忆，`downloads/` 是他发的图，`generated/` 是生成的图 |
| `Dockerfile` / `docker-compose.yml` | 备用部署方式，现在没用 |

### 3.2 一条消息从 Discord 进来到回复出去

1. **收到**：`NewPersonClient.on_message` → `App.on_user_message`。
   - `_should_handle` 只放行 Leo 和 `ALLOWED_USER_IDS` 里的人，而且只认私聊或配置的 `PROACTIVE_CHANNEL_ID` 频道。
   - `!np` 开头且是 Leo 发的：交给 `owner.handle`，**不入库、不进模型**，她不知道。
2. **入库**：
   - `_fresh_images` 下载图片附件，有超时，下不下来就当没这张图。
   - `memory.add_user_message` 按 Discord 消息 id 去重，网关重发不会重复记。
3. **排时间**：`_schedule_reply`。
   - 先看这批消息**到来之前**对话有多热（cold / warm / hot）。
   - 再由 `attention.plan_reply` 定：下次看手机的时刻、当场处理还是先放着、读和想的时间、疲劳、不落在睡眠里、总延迟上限。
   - 已经排着回复的话，`merge_pending` 往后推（他还在打字）。
   - 顺便 `life.maybe_schedule_sign_off`，看要不要排睡前那一句。
4. **到点**：`scheduler.run_forever` 取到期任务（租约 + 同一段对话同时只跑一个）→ `App.handle_reply_job`。
   - 暂停中：推后。
   - 她睡着了（而且不是 Leo 刚用 `!np now` 催的）：推到她醒来后的第一眼。
   - 上次想好的回复发到一半就崩了：从 `job.progress` 接着发。放了超过 30 分钟就作废重想。
5. **想**：`_generate_reply` 拼 `ReplyRequest` → `brain.generate_reply`。
   - 拼进去的内容：
     - 此刻状态 `_build_situation`：作息、日程里正在做的事、节日、出行预告、心情；
     - 滚动摘要、还记得的事实、本话题模式的台账、没聊完的问题、"还没到时间"的计划；
     - 打不打错字、模式指令、最近 40 条、这批未读、提示（等了多久、快睡了）、可发的照片。
   - `prompts.build_reply_user` 生成易变的 user 消息，system 是固定的 `prompts.build_system`（带 `cache_control`）。
   - 调用 `messages.parse(output_format=ReplyPlan)`。他问了具体的事她却给空的，就追问一次。
   - `_polish` 过 `style_guard`：机械能修的修掉，修不了的让模型重写一次。
6. **失败**（返回 `None`）：
   - 今天额度用完：顺延到明天。
   - **主模型、备用模型都拒了**：当作看到了没回（11-17 加的，见第 9 节）。
   - 其他任何失败：抛错，调度器重试（`reply` 是持久类任务，最多 16 次）。
   - **绝不给他发错误文本**。
7. **标已读**：`memory.mark_read`，必须在模型成功之后。存下 plan 和已发条数，崩了能续发。
8. **发**：`_deliver_reply` → `delivery.Deliverer`。
   - 解析照片（照片库按标签挑）。
   - 按字数算打字时长，一条条发，需要时引用他的某条。
   - 发的过程中他又说话：停下重想。
   - 手滑的那条几秒后改字。可能只点个表情反应。
9. **善后**：`_after_reply` 写日记（`inner_note`）、记台账和"他给了下文"、排 `follow_up`、清零被晾计数，然后 `_maybe_summarize`（攒够 60 条就排 `memory_update`）。

### 3.3 其他任务（都在 `App.start` 里注册给 scheduler）

| 任务 | handler | 做什么 |
|---|---|---|
| `day_plan` | `life.handle_day_plan_job` | 她醒来时用打杂模型编今天的日程，再从里面排主动消息的时刻 |
| `proactive` / `follow_up` | `handle_proactive_job` | 到点用主模型想要不要说、说什么（`generate_proactive`），可以不发 |
| `sign_off` | `handle_sign_off_job` | 睡前那一句 |
| `memory_update` | `handle_memory_update_job` | 整理记忆。被拒有一整套兜底：换模型、对半切、只跳过拒的那条、拒的是摘要就停一周 |

**启动**：`on_ready` → `App.start`：
1. 注册 handler，`scheduler.recover()` 收回崩溃时的租约；
2. 补抓停机期间漏掉的消息（`catch_up`），她自己当时发过的不会再回一遍；
3. `ensure_today_plan`，第一次上线还有 `ensure_opener`；
4. 起在线状态循环和调度循环。

登录、连网关失败会自己重试，不会被 systemd 熔断。

### 3.4 数据库表（`data/newperson.db`）

`messages`、`conversations`（含摘要、摘要游标、被晾次数）、`facts`、`ledger`、`diary`、`jobs`、`photo_usage`、`usage`（每天调用次数和估算美元）、`kv`（各种标记）。

**这个文件就是她的全部记忆**，删了她就不认识他了。

---

## 4. 在本地跑和测试

### 4.1 环境

- Python ≥ 3.11，**建议 3.11 或 3.12**。服务器是 Ubuntu 24.04 自带的 3.12。
  - discord.py 用到标准库的 `audioop`，3.11 上就有弃用警告，3.13 起标准库里没有它了，没必要去踩。
- **用 macOS、Linux 或 WSL**。`tests/test_alert.py`、`tests/test_backup.py` 会直接跑 `scripts/` 里的 bash 脚本，原生 Windows 上会挂。
- **Leo 的 Windows 电脑上（11-18 实测）**：
  - 原生 Windows：`.venv` 在仓库里，Python 3.12。先 `$env:PYTHONUTF8 = "1"` 再跑 pytest，
    不然子进程输出的中文按 GBK 解码，`test_doctor` 有一个会挂。
    结果是 727 过、8 个失败、5 个跳过：失败的是 `test_alert`、`test_backup`、`test_media` 里要 bash 和 Linux 命令的，
    跳过的是因为没有 gpg。**这 8 个不是坏了，别去改测试。**
  - 740 个全过要在 WSL 里跑。WSL 的 Ubuntu 里有一份克隆 `~/np-test`（从 Windows 这份克隆的，自带 `.venv`）。
    流程是：本地先提交 → 跑下面这行 → 全过再推送。

    ```powershell
    wsl -d Ubuntu -u root -- bash -c "cd ~/np-test && git pull -q && .venv/bin/python -m pytest -q && .venv/bin/ruff check newperson tests"
    ```
  - 提交身份在这个仓库里单独设成了 GitHub 的 noreply 地址（`git config --local`），别用全局那个带真名的。
- 依赖见 `pyproject.toml`：discord.py、anthropic、pydantic、pyyaml、python-dotenv、aiosqlite。开发额外要 pytest、pytest-asyncio、ruff。

```bash
git clone https://github.com/Forever666123/New_person.git && cd New_person
git checkout claude/discord-virtual-character-qzc8a1
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

python -m pytest -q                 # 740 个，不联网，一分钟左右
ruff check newperson tests
python -m newperson simulate --days 3   # 不联网，看作息和回复时机
python -m newperson check               # 检查配置和人设（没有 .env 的话会报三个必填项没填，正常）
```

- **anthropic SDK 版本**：
  - 服务器是 9 月装的 1.x。`update.sh` 用的是 `pip install -e .`，不会主动升级已经满足的依赖。
  - 全新安装会装上 2.x（11-17 试过 2.9.0，740 个测试全过）。2.x 默认走 API 版本 `2026-10-21`：不再接受 `temperature`、`top_p`、`top_k` 等，这些我们都没用。
  - **但测试全用假 client，不连真接口**。要把服务器升到 2.x 之前，先在有 `.env` 的环境里真调一次，比如 `python -m newperson plan` 会调一次模型。
- 本地真跑一个"她"需要 `.env` 里的三个必填项。**线上那个 bot token 正被服务器用着**：本地用同一个 token 跑，会跟服务器抢同一个账号、两边一起回他。要在本地试，就另建一个测试用的 Discord 应用。

### 4.2 提交前

```bash
python -m pytest -q && ruff check newperson tests
```

- 改了行为参数，顺手跑一次 `simulate` 看效果。
- 提交说明用中文，写"为什么"。

### 4.3 每组测试测什么（`tests/`）

| 文件 | 测试数 | 守着什么 |
|---|---|---|
| `test_integration.py` | 183 | 端到端：一条消息从进来到发出去的整条链路。崩溃续发、补抓、睡着不回、被拒兜底、主动消息、睡前一句、记忆整理等，大部分"上次是怎么坏的"都在这里 |
| `test_brain.py` | 60 | 模型调用：假 client 不联网。失败一律变沉默、被拒换模型、结构化输出、价格和花费、风格重写、带图被拒不带图重试 |
| `test_doctor.py` | 48 | 体检：每一项判据、按时间窗口数、**不泄露聊天内容**、读不了库绝不印全绿 |
| `test_delivery.py` | 38 | 投递：气泡、打字时长、被打断、发图失败、手滑改字 |
| `test_ledger.py` | 38 | 台账：只记他说出口的、回访周期、问两次就放下、**不在他说的时间之前问** |
| `test_rhythm.py` | 37 | 作息抽签："没有规律"本身要成立：起床分散、看手机不成周期、睡觉不回、DST |
| `test_memory.py` | 35 | 存储：崩溃恢复、重复事件、记忆衰减、他一回就清零 |
| `test_media.py` | 32 | 照片库：不重复发、不发对不上时段的、旧照片不说成刚拍的 |
| `test_style_guard.py` | 31 | 风格拦截：预算制、禁语的各种匹配方式（**不变量 6，整个文件**） |
| `test_attention.py` | 28 | 回复时机：不秒回、热聊冷聊、疲劳、防抖、上限 |
| `test_life.py` | 25 | 主动消息的分寸：被晾退开、按时间原谅、不在睡觉时排、节日 |
| `test_scheduler.py` | 21 | 调度器：崩溃恢复、同一段对话单飞（**不变量 4**）、失败不打扰用户 |
| `test_backup.py` | 19 | 备份和恢复脚本：一致快照、加密、空备份不顶掉旧的、保留策略（需要 bash，部分要 gpg） |
| `test_calendar.py` | 13 | 学期日历、假期、出行跨时区 |
| `test_opener.py` | 11 | 第一次上线那一句 |
| `test_alert.py` | 3 | `scripts/alert.sh`：没配 URL 不出声也不失败、只发一条不 @ 任何人、systemd 放弃她时会报警 |

有些测试是参数化的，所以上表加起来少于 740。`CLAUDE.md` 列了每条不变量对应的测试名，改相关代码前先看它们的 docstring。

---

## 5. 部署

### 5.1 服务器

| 项 | 值 |
|---|---|
| 供应商 / 地区 | Vultr，悉尼 |
| 配置 | 1 vCPU / 1 GB 内存，磁盘约 23 GB（她实际只用 70–80 MB 内存） |
| 系统 | Ubuntu 24.04 LTS。11-18 装完更新并重启过，内核 6.8.0-149；剩 `distro-info`、`python3-distro-info`、`snapd` 三个是分批推送还没轮到，不用管。登录时会提示有 26.04 可升。**别 `do-release-upgrade`**：Python 大版本会变，`.venv` 得重建，24.04 本来就支持到 2029 年 |
| 主机名 | `chloe` |
| 登录 | root，用 SSH 密钥。当时按步骤关了密码登录，要确认就跑 `sshd -T \| grep passwordauthentication`。Leo 的 Windows 电脑上 `~/.ssh/config` 里配了别名 `chloe`，所以 `ssh chloe` 直接进。IP 在那个 config 里，这里不写 |
| 代码 | `/opt/New_person`（跟着 `claude/discord-virtual-character-qzc8a1` 分支） |
| 虚拟环境 | `/opt/New_person/.venv` |
| 数据 | `/opt/New_person/data/` |
| 服务器时间 | UTC。她的日志里的时间是她那边的（`America/New_York`） |

如果本地 Claude Code 跑在 Leo 的 Mac 上，Mac 上还没有这个 SSH 别名。**别把 Windows 上的私钥拷来拷去**。在 Mac 上新生成一把，用 Windows 那边把它的公钥加进服务器的 `/root/.ssh/authorized_keys`。

### 5.2 systemd

- `chloe.service`（装在 `/etc/systemd/system/`，源文件 `scripts/chloe.service`）：
  - `Restart=always`，`RestartSec=30`；一小时内最多重启 20 次就熔断（`StartLimit*` 必须写在 `[Unit]` 里），熔断时触发 `chloe-alert.service` 报警。
  - **`RestartSec=30` 不要改小**：Discord 每个 bot 每天只给 1000 次 IDENTIFY，超了会被重置 token。
  - `MemoryHigh=300M`、`MemoryMax=450M`、`UMask=0077`，只能写 `data/`。
- 常用命令：

```bash
systemctl status chloe
journalctl -u chloe -f              # 实时日志，Ctrl+C 退出不影响她
journalctl -u chloe -n 50           # 最近 50 行
systemctl restart chloe             # 改了 persona.yaml 或 .env 之后要重启才生效
systemctl reset-failed chloe && systemctl start chloe   # 被熔断（start-limit-hit）之后，先修好问题再这样拉起来
```

### 5.3 脚本

| 脚本 | 用法 | 说明 |
|---|---|---|
| `scripts/update.sh` | `cd /opt/New_person && ./scripts/update.sh` | 拉代码（`--ff-only`）→ 装依赖 → **先跑 `check`，没过就不重启** → 重启 → 等她上线 → 跑一次 `doctor`。代码没变就直接退出 |
| `scripts/backup.sh` | 手动跑 `scripts/backup.sh`；cron 每天 04:00 | SQLite 在线备份取一致快照 → 验 → gpg 加密 → rclone 传到 Backblaze B2 → 取回来解开再验 → 删旧的。成功才写 `data/.last_backup_at`，失败会报警。**不用停服务** |
| `scripts/restore.sh` | 默认是**演练**（不碰线上）；`--list` 看有哪些；`--at 20260912` 选一天；`--install` 真装回去（会停服务、留下当前那份） | cron 每周日 05:00 演练一次 |
| `scripts/alert.sh` | `scripts/alert.sh "一句话"` | 往 Leo 自己的私密 Discord 频道发报警（webhook）。没配就只写 stderr，不连累调用方。只发计数和错误类别，不带聊天内容 |

DEPLOY.md 里写的 cron（`crontab -e`，服务器时间是 UTC）：

```
0 4 * * * /opt/New_person/scripts/backup.sh >> /var/log/chloe-backup.log 2>&1
0 5 * * 0 /opt/New_person/scripts/restore.sh >> /var/log/chloe-drill.log 2>&1
0 6 * * 1 cd /opt/New_person && .venv/bin/python -m newperson doctor > /var/log/chloe-doctor.log 2>&1 || { cp ...; scripts/alert.sh ...; }
```

第三条的完整写法见 DEPLOY.md 的"体检"一节，里面的 `%` 要转义成 `\%`。

- **11-18 用 `crontab -l` 核对过**：前两条（每天备份、每周日演练）在，上次备份是 11-17 04:00 UTC。
  **第三条每周体检没装**，装不装等 Leo 定。

日志：服务本身看 `journalctl -u chloe`；备份、演练、体检分别在 `/var/log/chloe-backup.log`、`chloe-drill.log`、`chloe-doctor.log`；体检发现 ✗ 时另存 `/var/log/chloe-doctor.BAD-日期.log`。

### 5.4 日常控制（Leo 在私聊里发，她看不到）

`!np status`（状态、下一条回复排在几点、今天调用次数和花费、上次备份时间）、`!np now`、`!np retry`、`!np pause` / `resume`、`!np away 出差 5` / `back`、`!np chatty 0.5`、`!np ledger [类别]`、`!np plan`、`!np help`。

---

## 6. 模型和 API

| 用途 | 变量（`.env`） | 服务器上现在的值 | 代码默认值（`config.py`） |
|---|---|---|---|
| 回复、主动消息 | `NEWPERSON_MODEL` | `claude-opus-5-5`（11-17 确认） | `claude-opus-5-5` |
| 回复被拒时的备用 | `NEWPERSON_FALLBACK_MODEL` | 没设。默认值跟现在的主模型一样，所以等于没有备用（Opus 5.5 的过滤类别本来就少）。写 `none` 关掉 | `claude-opus-5-5` |
| 日程、记忆整理 | `NEWPERSON_UTILITY_MODEL` | `claude-haiku-5-5`（11-17 确认） | 留空 = 跟主模型一样 |
| 思考强度 | `NEWPERSON_EFFORT` | `medium`（11-18 确认） | `medium` |
| 输出上限 | `NEWPERSON_MAX_TOKENS` | 应该没设（模板里没有这行），用默认 | `8000` |
| 每天最多调几次 | `MAX_CALLS_PER_DAY` | 模板里是 `200` | `200`，超了就当今天没怎么看手机，任务顺延 |

- **新出模型时要改 `brain.py` 里的几张表**：`PRICING_PER_MTOK`、`EFFORT_SUPPORTED`、`CACHE_READ_FRACTION`、`LONG_PROMPT_TIER`。漏了不会报错，只是 `effort` 悄悄不传、花费估错。`python -m newperson check` 会对不认识的型号给出警告。
- **调用方式**：`AsyncAnthropic().messages.parse(output_format=<Pydantic 模型>)`，不传 `thinking`、不传采样参数、没有 prefill。system 带 `cache_control`，**字节级固定**（不变量 3）。
- **花多少钱**：
  - 服务器：$5 + 10% GST，约 $5.50/月，按小时计费，每月 1 号扣卡。
  - API：上线头一个月（Sonnet 5 回复 + Haiku 4.5 打杂）实际每月约 $3–4，聊得多按比例涨。现在的 Sonnet 5.5 同价；Haiku 5.5 打杂是 4.5 的十分之一，几乎可以忽略。
  - 11-17 起回复用 Opus 5.5，回复这块大约是 Sonnet 的两倍，估计每月 $6–8。
  - 实际数以 `usage` 表为准：`sqlite3 -readonly data/newperson.db "SELECT day, calls, round(estimated_usd,3) FROM usage ORDER BY day"`，或者 `!np status`。
- Leo 说他的 Claude Max 每月带 $100 API 额度。**还没确认这笔额度是不是加在 bot 那把 API key 所在的组织上**，见第 11 节。
- API 是预充值，每笔一年过期。余额用完她会整个哑掉，`!np status` 和体检会直接说"多半是 API 余额用完了"。

---

## 7. 环境变量和密钥（只写名字和位置）

**服务器 `/opt/New_person/.env`**（权限 600，不进 git；模板是仓库里的 `.env.example`）：

- 必填：`DISCORD_BOT_TOKEN`、`ANTHROPIC_API_KEY`、`OWNER_DISCORD_USER_ID`
- 可选：`ALLOWED_USER_IDS`、`PROACTIVE_CHANNEL_ID`、`NEWPERSON_MODEL`、`NEWPERSON_FALLBACK_MODEL`、`NEWPERSON_UTILITY_MODEL`、`NEWPERSON_EFFORT`、`NEWPERSON_MAX_TOKENS`、`MAX_CALLS_PER_DAY`、`PERSONA_PATH`、`PHOTOS_INDEX`、`DB_PATH`、`DOWNLOADS_DIR`、`GENERATED_DIR`、`LOG_LEVEL`、`IMAGE_GEN_COMMAND`
- 调试用，**线上必须是正式值**：`DELAY_SCALE=1.0`、`DEBUG_FORCE_AWAKE=0`。开着的话她会秒回或者不睡觉，整个项目就白做了。

**服务器上的其他密钥文件**（都不进 git）：

| 文件 | 里面是什么 |
|---|---|
| `/opt/New_person/scripts/backup.env` | `RCLONE_REMOTE`、`GPG_PASSPHRASE_FILE`、`PYTHON_BIN`、`DB_PATH`、`SERVICE_NAME`、`KEEP_DAYS`、`KEEP_MIN`、`KEEP_LOCAL`（模板 `scripts/backup.env.example`） |
| `/root/.chloe-backup-pass` | 备份的 gpg 加密口令。Leo 应该在密码管理器里另存了一份，没这份口令备份就是一堆打不开的乱码 |
| `/root/.config/rclone/rclone.conf` | Backblaze B2 的 keyID / applicationKey（rclone 默认位置） |
| `/opt/New_person/scripts/alert.env` | `ALERT_WEBHOOK_URL`（Leo 私密频道的 webhook）。**这个 URL 谁拿到都能往那个频道发消息**：它曾经被贴进过聊天，已经让 Leo 删掉重建 |
| `/root/.ssh/authorized_keys` | Leo 登录用的公钥 |

**在别处的**：

- Discord bot token 在 Discord Developer Portal → Bot。那个页面还得开着 MESSAGE CONTENT INTENT，否则收到的消息内容全是空的。
- Anthropic API key 在 platform.claude.com 的 Console。
- SSH 私钥在 Leo Windows 电脑的 `%USERPROFILE%\.ssh\`。

---

## 8. 现在的进度

### 8.1 做完的

- 全部核心机制，见第 2、3 节：四层作息、回复时机、主动消息、台账和交易纪律、记忆和衰减、风格把关、睡前一句、节日、手滑、开场、停机补抓、在线状态、照片库机制、`!np` 命令。
- 运维：
  - systemd（熔断保护 bot token）、`update.sh`；
  - 每天加密异地备份和每周恢复演练；
  - 只读元数据的体检，加每周体检和出事报警的 webhook；
  - SSH 改成密钥登录。
- 9 月底按评审做了八轮"找缺陷"，又按 Leo 的决定改了三批人设：周末作息、长途飞行、期末周、节日、打错字有时不改。
- 10 月：
  - 国庆那天她说自己放假的 bug（`fd80dc8`）；
  - 升级到 Sonnet 5.5 和 Haiku 5.5，花费按各型号的缓存价和分档算（`90403a7`、`1c22fa9`）；
  - Haiku 5.5 带安全过滤以后记忆整理被拒的整套兜底，审了五轮（`d670d24`…`e6f665f`）；
  - 摘要上限 800 字（`bbc87f5`）。
- 11 月：被晾按时间原谅；回复被两个模型都拒时当作没回，不再从此不说话（`6e71788`）。

### 8.2 做到一半 / 等着做

1. ~~服务器部署最新代码~~、~~回复换成 Opus 5.5~~：11-17 都做完了，服务器在 `64bab55`。
2. ~~服务器系统更新和重启~~：**11-18 做完了**（Leo 当场同意）。25 个包装上，重启用了大约 30 秒，
   她的服务自己起来、连上了，体检全过。下次再做照这个来，先 `ssh chloe` 进去：

   ```bash
   export DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1
   apt-get update -q && apt-get -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold --with-new-pkgs upgrade
   systemctl reboot
   ```

   - 两个 `Dpkg::Options` 是让它保留现有的配置文件、不停下来问；`NEEDRESTART_SUSPEND` 是不让它中途逐个重启服务，反正马上整机重启。
   - 她的服务是 enable 的，开机会自己起来，停机那一会儿的消息启动时会补抓。时间不讲究，Leo 没在跟她聊就行。
   - **别升 26.04**，见 5.1。
3. **照片库**：现在是空的，所以 `window_photo` 永远抽不到，回复里也发不了图。计划是：
   - 在 Leo 本地用 Claude in Chrome 打开他已登录的 Gemini 网页版，批量生成"她手机里的照片"；
   - 自己审、下载、写 `index.yaml`，传到服务器 `persona/photos/`，然后重启。
   - **Leo 明确说不想自己看这些图，全程交给你**。任务说明见附录 A。

### 8.3 已知的问题和限制

- **"说到一半被拒"跟"输出被 `max_tokens` 截断"分不开**：两种情况 SDK 都在解析半截 JSON 时抛 `json_invalid`，代码一律按"被拒"处理。这会导致换模型重试、对半切；体检和 status 的措辞两种都说了。真要分开，得改成先看 `stop_reason` 再解析（`brain.py` 的 `_call`）。
- **回复被双拒后，那句话还留在最近 40 条里**。如果分类器是被它触发的，后面几轮可能也被拒，直到它滑出窗口。目前只是理论上的：10-08 到 11-17 被拒 0 次。
- **摘要上限只靠提示词，没有硬截断**。体检在超过 2 倍时会提醒。
- **学期日历只写到 2027-08-31**，之后按 `fallback` 推；节日列表写到 2028 年底，农历节日要一年一年往下续。
- 体检的 WARN：两周里她主动说的话 73% 是在"办事"（追问做没做、汇报查完了）。Leo 的意思是她聊他的事也可以，所以**没改**，先观察。
- yaml 里有几个旋钮代码不读，已经注释掉并写明了：`quiet_days_before_callback`、`extra_part_probability`、`english_words_allowed`。
- 新的 anthropic SDK 2.x 没在真接口上跑过，见 4.1。

### 8.4 下一步（按优先级）

1. ~~确认 `NEWPERSON_EFFORT` 的值和服务器上 anthropic SDK 的版本~~：11-18 查过，`NEWPERSON_EFFORT=medium`，SDK 是 1.4.0。
   `DELAY_SCALE=1`、`DEBUG_FORCE_AWAKE=0` 也是正式值。
   查法：`grep -E '^NEWPERSON_' /opt/New_person/.env` 和 `/opt/New_person/.venv/bin/pip show anthropic`，这两行都不会打出密钥。
2. ~~服务器系统更新和重启~~：11-18 做完了（见 8.2）。
3. 照片库（附录 A）。做完确认 `python -m newperson check` 里照片那一项不再说空，`window_photo` 能抽到。
4. 每周看一眼体检，尤其是主动消息的组成、被拒的计数、`办事型` 占比。
5. 想把服务器的 SDK 升到 2.x 的话，先真调一次。
6. 2027 年暑假之前续学期日历，每年续农历节日。
7. 可选的省钱项：缓存 TTL 改成 1 小时。她回消息常隔 5 分钟以上，多数回复在付写缓存的钱；换 Opus 之后更值得做。没有额度压力的话不急。

---

## 9. 做过的重要决定和原因

- **行为全在 yaml，代码只放机制**：调人物不用碰 Python。有一批测试守着"像不像人"，参数调过头会先叫。**它们失败时先怀疑参数，别去改断言**。
- **全是概率不是时刻表**，随机源 `(seed, 日期)`：可复现、重启一致、天与天无关。
- **不裸调 `datetime.now()`，不用全局 `random`**：测试要能把时间拨到任意时刻、结果可复现。
- **system prompt 字节级固定**，易变内容全放 user 消息：不然缓存永远不命中，成本翻几倍。
- **`mark_read` 只在模型成功之后**，唯一例外是两个模型都拒：否则失败重试时未读是空的，消息永远回不出去。而被拒的那批留着不动，她又会从此不说话。
- **体检只读元数据**：她像不像人体现在时机上，不在内容上；而且体检的输出会被打印、被贴出去，不能漏聊天内容。
- **主动消息先过"今天开不开口"一关**；被晾就退开，按时间原谅：既不黏人，也不消失。
- **台账只记他说出口的，问两次就放下**：不然她就成了催办机器人。
- **国内的节日在美国照常过**（`Holiday.local`）：国庆那天她说自己放一天假，就是这么穿帮的。
- **回复别省**：人物的核心是分寸，小模型容易滑向两个极端。打杂的活（日程、记忆）用便宜的。
- **换到 5.5 这代模型**（10 月）：同价或更便宜，而且 Sonnet 5.5 守角色更稳。
- **回复换成 Opus 5.5**（11-17 换的），三个原因：
  - Leo 有每月 $100 的额度，钱不是问题；
  - Opus 5.5 文字更自然；
  - **它的安全过滤没有 Sonnet 5.5 和 Haiku 5.5 都有的那一类 `general_harms`**（官方说正常内容也可能撞上），亲昵一点的聊天被误拦的可能更小。
- **Haiku 5.5 被拒的兜底**做成：换主模型 → 对半切 → 先不带那条消息问一次，确认拒的是它 → 只跳过那一条、一天最多一条 → 拒的是摘要就停一周，体检报 ✗。每一步都是审查挑出来的真问题，见第 10 节。
- **不给 Gemini 用非官方的"网页版当 API"那种工具**：违反 Google 条款，可能封他的账号，三天两头失效。官方 API 生图在 2026 年 11 月已经没有免费额度（最便宜每张约 $0.03–0.04；Google Cloud 那 $300 赠金 2026 年 3 月后开的账号不能用在 Gemini API 上）。所以改成在 Gemini 网页版里正常使用，由本地的 Claude in Chrome 代劳。
- **照片不生成她的脸**：每张都会长得不一样，一发就穿帮；真人平时发的也多是吃的、窗外、街景。
- **服务器命令在云端那段时间一直由 Leo 自己跑**：云端会话拿不到他的 SSH 私钥，也不该拿。
  **11-18 Leo 同意本地 Claude Code 直接 `ssh chloe`**：只读的检查直接跑；会改东西的（重启、装更新、改 cron、改 `.env`）先问他。
- **改她的行为要 Leo 同意**：bug 修复、可靠性可以直接做，但要说清楚改了什么。

---

## 10. 踩过的坑

- **合成数据测的是"我以为她怎么工作"**：体检的两个计数器用合成数据全过，接上真实流量整个反了。凡是"数她做了什么"的判据，都要有一条走完整条路的测试。
- **云端审查一轮修一轮会引入新问题**：Haiku 5.5 那套兜底审了五轮才收敛，每一轮都在修上一轮的修法。改这块之前先读 `discord_bot.py` 里 `_route_around_refusal`、`_memory_paused`、`_maybe_summarize` 的注释。
- **共享的状态标记和 `await`**：`brain.last_refused` 是多个任务共用的。必须在模型调用返回后**立刻**取，中间不能有 `await`，否则别的任务会把它改成自己那次的结果。
- **SDK 解析在我们之前**：结构化输出遇到半截 JSON，SDK 直接抛 `pydantic.ValidationError`，我们看不到 `stop_reason`。
- **测试里的"空值数成一条"**：`len((None or "").split(","))` 是 1。数 kv 里的时间戳要过滤空串，否则测试永远是绿的。
- **systemd 的 `StartLimit*` 写在 `[Service]` 里会被静默忽略**：必须写在 `[Unit]` 里。
- **SQLite 是 WAL 模式，不能直接 `cp`**：刚说的话还在 `-wal` 里。备份一律走在线备份接口，也就是 `backup.sh` 那套。
- **crontab 里的 `%` 要写成 `\%`**。
- **Windows PowerShell 转义坑**：一行 `ssh chloe "...带 $ 和引号的命令..."` 在 PowerShell 里会被搞坏。让 Leo 先 `ssh chloe` 进去，再粘贴 bash 命令。
  本地 Claude Code 自己跑的时候，用 Bash 工具（Git Bash）把命令从标准输入喂进去，引号和 `$` 都不会被动：
  `/c/Windows/System32/OpenSSH/ssh.exe -o BatchMode=yes chloe bash -s <<'EOF'` … `EOF`。
- **Windows 的 git 默认把文本检出成 CRLF**（`core.autocrlf=true`）：`.sh` 已经用 `.gitattributes` 钉成 LF。
  别的文件（`.service`、`.env.example`、yaml）在 Windows 工作区里还是 CRLF，要上服务器就走 git，别从这份 scp。
  照片库的 `index.yaml` 是例外，yaml 带 CRLF 能正常读。
- **`.env` 里可能没有某一行**：`sed -i 's/^X=.*/.../'` 对不存在的行什么都不做。要么先 `grep` 看有没有，要么写"有就改、没有就追加"的循环。
- **云端容器会被重置**：`.venv` 会没，要重新 `pip install -e ".[dev]"`。本地没这个问题。
- **云端提交用过一个特殊的签名程序**（`git -c gpg.ssh.program=...`）：那是云端容器专用的，本地正常提交就行。
- **模型表漏了新型号不会报错**：见第 6 节。
- **webhook URL、密码绝不能贴进聊天或仓库**：出过一次，已经让 Leo 删掉重建。

---

## 11. 还没解决、要问 Leo 的问题

1. ~~服务器跑过 `update.sh` 没有~~、~~要不要换 Opus 5.5~~：11-17 都做了。~~`NEWPERSON_EFFORT` 是不是 `medium`~~：11-18 确认是。
2. ~~什么时候方便做服务器的系统更新和重启~~：11-18 做完了。
3. 每月 $100 的 API 额度，是不是加在 bot 那把 API key 所在的 Console 组织上？去 platform.claude.com 的 Billing 页能看到。
4. 照片库：本地有没有 Claude 桌面版和 Claude in Chrome 扩展？要不要现在按附录 A 做？他说过不想看这些图，全程交给 Claude。
5. 主动消息里"追问做没做"占比偏高（73%）：要不要调，还是继续观察？
6. 打杂模型继续用 Haiku 5.5（便宜、有过滤但有兜底），还是换回 Haiku 4.5（没过滤、贵十倍、但也就每月几毛钱）？
7. ~~本地 Claude Code 可以直接 `ssh chloe` 操作服务器吗~~：11-18 他说可以。只读的直接跑，会改东西的先问。
8. ~~每周体检的 cron 和恢复演练的 cron 真的装了吗~~：11-18 查过，演练装了，**每周体检没装**。要不要装，等他定。
9. 2027 年 9 月以后的学期日历：到时候按 Northeastern 新校历续，还是让她毕业？

---

## 12. 跟 Leo 合作的习惯

- **用中文回复**。他不是想听机制细节的人：说结论、说要他做什么，命令给能直接复制的。
- 服务器命令给成一整段能粘贴的 bash，先 `ssh chloe` 再粘。
- 改她的行为（作息、说话方式、主动频率）前先问他；修 bug、可靠性可以直接做，事后说清楚。
- **绝不把聊天内容放进仓库**：`data/` 已经在 `.gitignore` 里；`python -m newperson export` 打出来的东西只给他自己看。
- 不碰、不要他发任何密钥。他要给的话，让他自己填进服务器上的文件。

---

## 附录 A：照片库任务（给本地带 Claude in Chrome 的会话）

前提：Chrome 里装着 Claude in Chrome 扩展，并且已经登录 gemini.google.com。Leo 不看图，全程自己判断。

1. 在 gemini.google.com 里生图，可以多开几个对话，一直做到 Gemini 提示到了用量上限为止。
2. 提示词格式：
   > A casual smartphone photo taken by a 23-year-old grad student in Boston: [场景]. Shot on an iPhone, natural light, slightly imperfect framing, realistic, not professional photography. No people's faces, no readable text, no logos or brand names.
3. 场景清单，每个场景最多留 1–2 张。现在是 11 月，波士顿深秋到初冬，会下雪：
   - **吃喝**：书桌上一杯奶茶；图书馆里一杯拿铁和笔记本电脑；和室友在公寓餐桌吃泰国外卖；深夜泡面加蛋；早餐贝果抹奶油奶酪；家里煮的速冻饺子；在家吃火锅；冰美式放在一摞教材旁边；中国城餐馆一笼小笼包；抹茶拿铁；深夜零食摊了一桌。
   - **学习**：深夜图书馆书桌上的笔记本电脑，屏幕模糊；从后排拍的阶梯教室；夜里没人的自习室；秋天校园草坪上的落叶；雨天撑伞去上课；写了一半的数学草稿，字迹看不清。
   - **家里**：深夜窗外下雪；窗外下雨；夜里亮着台灯的书桌；裹着毯子、腿上放着电脑；窗台上的绿植；凌晨窗外的路灯。
   - **波士顿**：查尔斯河日落；Back Bay 的红砖排屋；夜里 Newbury Street 的圣诞灯；夜里路灯下的雪街；下雪的 Boston Common；黄昏的海港；地铁绿线站台，站名看不清。
4. 每张都要审，有下面任何一条就丢掉重做：
   - 不像手机随手拍的；
   - 有人脸；
   - 有能看清的字、招牌、商标或乱码字；
   - 手或物体变形；
   - 季节不对。

   右下角如果有 Gemini 的小星星水印，就用 PIL 把那一角裁掉。
5. 合格的存到一个文件夹，文件名用 id，比如 `food-001.png`、`window-001.png`。
6. 同一个文件夹里写 `index.yaml`，格式见 `persona/photos/index.example.yaml`。
   - `tags` 用英文小写，从这些里选：food, coffee, drink, lunch, dinner, breakfast, snack, study, library, class, campus, laptop, home, dorm, window, night, snow, rain, sky, sunset, street, city, boston, river, subway, plant。
   - 凌晨窗外的那几张一定要带上 `window`、`night`，最好再带 `snow`、`boston`、`dorm`：`window_photo` 就靠这些标签挑图。
   - `caption` 用中文一句话，照实写图里有什么。
   - `time_of_day` 只能是 `morning`、`day`、`evening`、`night`、`any`。
   - `taken_hint` 写什么时候、在哪拍的；`location` 可选；`freshness` 不写，默认 `evergreen`。
7. 传到服务器并重启。**先问 Leo 能不能由你来跑**，不能就把命令给他：

   ```bash
   scp <文件夹>/* chloe:/opt/New_person/persona/photos/
   ssh chloe "cd /opt/New_person && systemctl restart chloe && .venv/bin/python -m newperson check | grep -A2 照片"
   ```

   照片不进 git（`.gitignore` 里排除了 `persona/photos/*`）。
