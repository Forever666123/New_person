# 给 Claude Code 的项目说明

一个接入 Discord 的虚拟人物。目标只有一个：**让她像人，不像程序。**
所有技术决定都服务于这一点，读代码时先想"这会不会露馅"。

## 跑起来

```bash
source .venv/bin/activate
python -m pytest -q                  # 389 个测试，不联网，二十秒跑完
ruff check newperson tests
python -m newperson simulate --days 3   # 看作息和回复时机，不联网
python -m newperson check                # 检查配置和人设
```

## 硬约定

- **不裸调 `datetime.now()`。** 一律通过 `Clock`，测试用 `FakeClock` 把时间拨到任意时刻。
- **不用全局 `random`。** 随机源通过参数传入 `random.Random`，否则测试不可复现。
- **注释、docstring、日志、文档一律中文；标识符和测试函数名用英文。**
  注释解释"为什么这么做"，不解释"做了什么"。
- **测试函数名描述行为**，例如 `test_she_never_replies_instantly`，docstring 写中文说明它守着什么。
- 类型标注完整，文件开头 `from __future__ import annotations`。

## 改行为之前先读这个

人物的行为**不写死在代码里**，全在 `persona/persona.yaml`。
代码里只有机制，参数在人设文件里。想改她的作息、说话方式、主动频率，
改 yaml，不要改 Python。

有一批测试专门守着"像不像人"：她不会秒回、起床时间要分散、
看手机的间隔不能成周期、睡觉时绝不回复、主动消息不能太频繁。
调参数调过头这些会先叫。**它们失败时先想想是不是参数调坏了，
而不是去改断言。**

## 四层作息（最容易改错的地方）

```
学期日历(calendar.py) → 阶段(phase) → 当日变体(variant) → 看手机(glance)
```

每一层都是**倾向和概率**，不是时刻表。固定作息会让她每天同一分钟上下线，
那是最大的破绽。抽签的随机源是 `(persona.seed, 日期)`，
所以同一天反复查结果一致、重启也一致，但天与天之间互不相关。

改 `rhythm.py` 时注意：`for_day(d)` 只回溯一天（`_raw_night(d-1)`），
不能改成递归，否则算一次要往前推到纪元。

## 关键不变量

改代码时不要打破这些，每条都有测试守着：

1. **模型调用失败绝不能被对方看见。** 任何异常都返回 `None`，
   调度器当作"这会儿没看手机"重试。绝不发错误文本。
2. **消息不能凭空消失。** `mark_read` 必须在模型成功返回**之后**。
   放在前面的话，失败重试时未读是空的，整批消息就永远回不出去。
3. **system prompt 字节级固定。** 里面出现时间、日期、随机内容，
   prompt cache 就永远不命中，成本翻好几倍。易变内容全部放 user 消息。
4. **每会话单飞。** 同一段对话同时只跑一个任务，
   否则回复和主动消息会在同一个频道里交错发出。
5. **`!np` 开头的消息不入库、不进模型。** 她不知道你在操控她。
6. **风格约束用预算不用开关。** 偶尔一个 emoji 是正常的，满屏才不正常。
   频率交给提示词描述，`style_guard` 只管上限。

## 文件职责

| 文件 | 管什么 |
|---|---|
| `calendar.py` | 学期日历、假期、出行（含跨时区） |
| `rhythm.py` | 每天抽签、活跃度曲线、看手机的时机 |
| `attention.py` | 什么时候看到、什么时候回、防抖、疲劳 |
| `style_guard.py` | 发出前的风格把关（预算制） |
| `prompts.py` | 稳定层 / 易变层，缓存的关键 |
| `brain.py` | 所有模型调用，失败处理，用量与日限额 |
| `memory.py` | SQLite，事实衰减，任务租约 |
| `scheduler.py` | 任务队列，崩溃恢复，重试 |
| `life.py` | 每日日程、主动消息候选 |
| `delivery.py` | 气泡、打字、发图、被打断 |
| `owner.py` | `!np` 命令 |
| `discord_bot.py` | Discord 适配与组合根（含停机后补抓漏掉的消息）|
| `backup.py` | 一致快照、完整性校验、上次备份的时间 |
| `doctor.py` | 体检：**只读元数据，绝不碰聊天内容**，盯的是她变得有规律 |

## 加新东西的位置

- 新的主动消息方式 → `persona.yaml` 的 `proactive.kinds`，代码不用动
- 新的话题模式（某个话题让她换个样子）→ `persona.yaml` 的 `modes`
- 新的当日状态 → `persona.yaml` 的 `rhythm.variants`
- 新的 `!np` 命令 → `owner.py` 的 `handlers` 字典
- 新的任务类型 → `models.JobKind` + `discord_bot.App.start` 里注册 handler

## 提交前

```bash
python -m pytest -q && ruff check newperson tests
```

改了行为参数的话，顺手跑一次 `simulate` 看看效果对不对。
