# NewPerson

一个接入 Discord 的虚拟人物。

她不是客服机器人。她有自己的作息、学期、假期，会因为在赶 due 而话少，
会熬夜到四点然后第二天中午才回你早上的消息，放假会飞去别的城市，
偶尔主动跟你说一句今天下雪了。

**她不秒回。** 就算正在聊，中位数也是一分多钟；隔了几小时才回是常态；
凌晨发的消息要等她醒。这不是加了随机延迟，是模拟"隔一阵看一眼手机"。

当前的人物是**沈亦宁**，23 岁，在波士顿读 Data Science。
换一个人物只要改 `persona/persona.yaml`，不用动代码。

## 快速开始

### 1. Discord 那边（新手最容易卡在这里）

1. 去 [Developer Portal](https://discord.com/developers/applications) 建一个应用，
   在 Bot 页面复制 Token。
2. **在同一个页面打开 MESSAGE CONTENT INTENT。** 这是特权 intent，
   不打开的话机器人能连上，但收到的每条消息内容都是空字符串，什么都不会发生。
3. OAuth2 → URL Generator，scope 选 `bot`，权限勾
   Send Messages、Read Message History、Attach Files、Add Reactions、View Channels。
4. 用生成的链接把机器人邀请进**你自己的一个服务器**。
   Discord 不允许机器人私聊没有共同服务器的人。
5. 在那个服务器的隐私设置里允许服务器成员给你发私信。
6. 打开 Discord 的开发者模式，右键自己的头像复制用户 ID。

### 2. 装起来

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.example .env      # 填 Token、API key、你的用户 ID
python -m newperson check --online
```

`check --online` 会真的连一次 Discord，验证 Token、特权 intent、以及能不能私聊你。
上面六步里漏了哪一步，它会直接告诉你。

### 3. 跑

```bash
python -m newperson run
```

或者用 Docker：

```bash
docker compose up -d
```

## 命令

| 命令 | 做什么 |
|---|---|
| `run` | 启动 |
| `check [--online]` | 检查配置和人设，`--online` 会真的连一次 Discord |
| `simulate [--days N]` | 不联网，看她在各个时间点会什么时候回消息。调参数时用 |
| `plan [--save]` | 让她给今天编一份日程并打印（会调模型） |
| `photos` | 扫描照片目录，为没登记的图生成索引草稿 |

`simulate` 的输出长这样，能直接看出时差和作息的效果：

```
2026-09-10 周四　秋季学期／松／普通
  08:47 起　23:22 睡　晚课　当场回的概率 85%
    05:42（他那边 19:42） 他发消息　她睡着　→ 09:10 回（等了 3.5 小时）
    21:48（他那边 11:48） 他发消息　她有空　→ 22:10 回（等了 22 分钟）
```

## 在 Discord 里控制她

私聊里以 `!np` 开头的消息是给程序看的，**不入库、不进模型**。她不知道你在操控她。

| 命令 | 做什么 |
|---|---|
| `!np status` | 她现在什么状态、有什么排着队、今天花了多少钱 |
| `!np now` | 让排着的那条回复立刻发 |
| `!np pause` / `resume` | 暂停。暂停时她不回也不主动，但消息照常记着 |
| `!np away 出差 5` / `back` | 请假。这期间她话少，基本不主动 |
| `!np chatty 0.5` | 主动消息的频率倍率 |
| `!np plan` | 看今天她给自己编的日程 |

她隔二十分钟才回是设计好的，但你盯着屏幕的时候分不出"正常"和"坏了"。
`!np status` 就是回答这个问题的，它会告诉你下一条回复排在几点、为什么。

## 改人物

全在 `persona/persona.yaml`，改完重启就生效。`persona/persona.example.yaml` 是空模板。

那份文件里的行为部分**全是倾向和概率，不是时刻表**。这是刻意的：
固定的作息会让她每天同一分钟上线下线，那是最容易看出是程序的地方。

| 想改什么 | 改哪里 |
|---|---|
| 她是谁、怎么说话 | `background` / `voice` / `relationship` |
| 什么话题不答 | `boundaries.deflect_topics` |
| 什么话绝对不会说 | `boundaries.never_say`（发出前会被拦下来重写） |
| 作息 | `rhythm.sleep`（分布，不是区间）、`rhythm.activity`、`rhythm.classes` |
| 忙起来是什么样 | `rhythm.phases`（跨天）、`rhythm.variants`（单天） |
| 学期和假期 | `academic.periods`，放假出门的地点在 `academic.travel` |
| 她主动说话的方式 | `proactive.kinds` |
| 某些话题让她换个样子 | `modes` |

## 她为什么像人

| 机制 | 解决什么 |
|---|---|
| 四层作息 | 学期决定大节奏，阶段决定这一周，当日变体决定今天，看手机频率决定这一刻。叠起来看不出规律 |
| 起床跟着昨晚走 | 熬夜到四点不会七点就起，假期会多睡 |
| 看到了先放着 | 不是"不回"，是延迟自然拉长到几小时，最多放三次 |
| 全局注意力 | 她只有一部手机。不会一边发主动消息，一边让半小时前的私信躺着没读 |
| 主动衰减 | 她开的话头你没回，下次概率乘 0.35。每天最多一次没被回应的开场 |
| 疲劳与收尾 | 聊久了回得慢，快睡了会说一句再走，而不是突然消失 |
| 在线状态跟着行为 | 不是到点自动切换。睡觉离线，醒着 idle，只有真在看手机才 online |
| 记忆会淡 | 太久没提的事她会忘，记不清就直接问。什么都记得的人聊天没意思 |
| 风格拦截 | 句号、emoji 刷屏机械修掉；说了不该说的话让模型重写一次 |
| 台账 | 他在交易上说过的话存着，前后矛盾时她直接翻出来问 |

## 花多少钱

每条回复、每天的日程、每次记忆整理各是一次模型调用。
稳定的人设提示词做了缓存，命中时按十分之一计费。

`MAX_CALLS_PER_DAY`（默认 200）是硬上限，超了她就当今天没怎么看手机，
任务顺延，程序不会崩。`!np status` 里能看到今天调了多少次、大概多少钱。

## 项目结构

```
newperson/
  calendar.py     学期日历与出行
  rhythm.py       作息：每天抽签，活跃度，看手机的时机
  attention.py    什么时候看到、什么时候回
  style_guard.py  发出前的风格把关
  prompts.py      提示词（稳定层 / 易变层）
  brain.py        所有模型调用
  memory.py       SQLite：消息、事实、台账、日记、任务
  scheduler.py    持久化任务队列
  life.py         每日日程、主动消息的候选
  delivery.py     气泡、打字、发图、被打断
  media.py        照片库
  owner.py        !np 命令
  discord_bot.py  Discord 适配与组合根
persona/
  persona.yaml    人物（改这个）
  photos/         照片库
```

数据落在 `data/`，删掉就等于失忆。

## 开发

```bash
pip install -e ".[dev]"
python -m pytest -q          # 232 个测试，不联网
ruff check newperson tests
```

测试里有一批断言守着"像不像人"这件事：她不会秒回、起床时间要分散、
看手机的间隔不能成周期、睡觉时绝不回复、连发不会把回复饿死。
调参数调过头，这些会先叫。
