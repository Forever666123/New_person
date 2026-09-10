# 部署到服务器

本地跑的问题是电脑一关她就下线了。放到服务器上她能一直在，
你半夜想起一笔交易随手说一句，她第二天早上会接。

## 她到底要多少资源

实测，不是估的：

| | 实测值 |
|---|---|
| 内存 | 78 MB（所有模块导入 + 组件装好 + 两千条消息） |
| 磁盘 | 数据库一年长 2 MB（按一天来回三十条算） |
| CPU | 绝大部分时间在等，只有调模型和发消息时动一下 |
| 网络 | 一条 Discord 长连接，加上每次回复一个 HTTPS 请求 |

所以**任何一档都够**，包括各家最低配的免费机器。
所有依赖（含 pydantic-core、jiter 这些 Rust 编译的）都有 arm64 轮子，
基础镜像 `python:3.12-slim` 也是多架构的，ARM 机器可以直接用。

## 先说结论

你在 UNSW 在读，有 .edu.au 邮箱——**先用 Azure for Students**。不要钱，
**不用绑卡**（这一条别的都做不到），机房能选 Australia East。
一台 B1s（1 核 1G）每月免费 750 小时，一台机器 24 小时跑正好用满，
另外白送 $100 额度当缓冲，基本花不掉。每年重新验证一次学籍就再续一年。

不想把她挂在学生身份上，就买最便宜的 VPS，一个月两三美金，全程你自己控制。

理由：她要维持一条 Discord 长连接，不能休眠，所以 Vercel、Cloudflare Workers
这类无服务器的全都不行；Render、Koyeb 的免费档根本不给后台任务用；
Railway、Fly.io 的免费档 2023、2024 年就没了。她本身只是一个 Python 进程
加一个 SQLite 文件，最低配的机器绰绰有余。

## 平替：从免费到最便宜

按"她能不能一直在"排的，不是按价格排的。

| 路子 | 花多少 | 值不值 |
|---|---|---|
| **Azure for Students** | $0，不绑卡 | 首选。悉尼机房，B1s 免费 12 个月，可续 |
| **手上已有的常开机器**（旧笔记本 / NAS / 台式机） | $0 | 今天就能跑，但断网断电她会穿帮，见下 |
| **Northflank 免费档** | $0 | 唯一不睡、允许无端口服务的 PaaS。但免费档没磁盘，SQLite 得换成他家 Postgres |
| **Fly.io** | 约 $2.5/月 | 托管里最便宜且形状对的：256M 机器 + 1G 卷，不用监听端口 |
| **年付小厂 VPS**（RackNerd 这类） | $11–25/**年** | 纯比钱它最低，一个月合一两美金。别指望售后 |
| **Oracle 永久免费** | $0 | 真·永久免费，但对我们这个进程有个专门的坑，见下 |
| **GCP 永久免费 e2-micro** | 其实约 $3.65/月 | 机器免费，公网 IPv4 从 2024 年起单独收费。只有美国三个区 |
| **Railway** | $5/月 | 最省心的，一口价 |

不用考虑的：**AWS**（2025 年 7 月起新账号是 $100 额度制，6 个月直接关户）、
**Heroku**（磁盘是临时的，每次重启 SQLite 就没了，除非改用它的 Postgres）、
**Render 免费档**（后台任务压根没有免费规格）、**所有 serverless**（执行模型不对）。
另外 GitHub Student Pack 里那个 $200 DigitalOcean 额度**已经没了**——
2026 年 8 月 1 日全部作废，网上教程还在推荐它的都过期了。

价格和条款是 2026 年 9 月查的，各家免费档一年能改三次（Oracle 那次连公告都没发），
真要签之前自己再看一眼官网。Azure 和 Heroku 的具体数字我是从搜索结果里读的，
它们官网在我这边连不上，你自己核一下 azure.microsoft.com/free/students。

### Oracle 免费档的那个坑

Oracle 是唯一真·永久免费还给你一台真机器的（2 核 12G ARM，200G 盘）。
但它会回收"闲置"的免费实例：7 天滚动窗口里 CPU 95 分位 < 20%、网络 < 20%、
内存 < 20%（ARM 机型三条都满足才收），就收走。

**一个挂着 websocket、偶尔写两行 SQLite 的 Python 进程，三条全中。**
她正好是这个策略瞄准的样子。

绕开的办法只有一个：把账号转成 Pay As You Go。回收策略只对纯免费账号生效，
PAYG 账号照样白嫖同样的额度，代价是绑一张真卡——记得设预算告警。

另外两件事：2026 年 6 月 15 日 Oracle 悄悄把免费 ARM 从 4 核 24G 砍到 2 核 12G，
没有公告，文档直接改了，超限的机器 8 月 18 日起被终止；悉尼区的 ARM 容量长期紧张，
开机大概率报 "Out of host capacity"，得反复重试。

它适合"我就是要免费"，不适合"我要她一直在"。

### 放家里跑

不用端口转发，不用公网 IP，不用 DDNS。她只往外连（Discord 的 wss:443、
Anthropic 的 https:443），家里的 NAT、CGNAT、动态 IP 全都无所谓。
让你转发端口的教程是把 bot 跟游戏服务器搞混了。

真正会出事的是这几个，按出现频率排：

1. **Discord 的登录次数限制。** 每 24 小时 1000 次 IDENTIFY（断线重连走 RESUME 不算）。
   家里网络一抖，配上 `restart: unless-stopped`，疯狂重启一小时就能烧完，
   后果是**所有会话被终止 + bot token 被重置**，只发一封邮件通知你。
   所以别让进程一崩就立刻重启：systemd 用 `RestartSec=30`，compose 限一下重启策略。
2. **不是**僵尸连接。网上老帖子说断网之后 socket 死了、进程还活着、库也不重连，
   她就那么安静下去——那是 2018 年的 discord.py。我翻了我们装的 2.7.1：
   心跳线程发现 `_last_recv` 超时会主动关掉分片重连，`poll_event` 自己也带接收超时。
   这个坑已经填上了，不用自己写看门狗。
3. **停电之后机器不自己开。** 主板 BIOS 默认多半是 "Stay Off"，改成 "Power On"。
4. **断电时的 SQLite。** 我们用的是 WAL + `synchronous=NORMAL`，保证数据库不坏，
   但不保证最后几笔事务不丢。放家里要么加个小 UPS，要么把 `synchronous` 改成 FULL——
   一年才写 2 MB，多几次 fsync 无所谓。

最后一条不是技术问题：家用宽带一年下来大概 99.0–99.5%，两到四天不在线。
对普通 bot 这只是停机，对她是**穿帮**——一个应该像人的角色，
在跟她作息完全无关的时间点凭空消失六小时。

新买硬件已经不划算了：2026 年内存涨疯了，Pi 5 4G 涨到 $85，N100 小主机 $240 起，
按 $4/月 的 VPS 算要三到七年才回本，比硬件寿命还长。真要买就买 $40 的二手瘦客户机
（Dell Wyse 5070 之类，6 瓦，一年电费 $10），一年多回本。
**但如果你手上已经有一台常年开着的机器，今天就 docker compose up，零成本。**

自建现在唯一站得住的理由是：`data/newperson.db` 是你们全部聊天的完整记录，
放在自己手上是一个正当的隐私立场。这个理由跟价格无关。

## 方案一：VPS

任何一家都行，最低配就够。参考价：

| 服务商 | 配置 | 月费 |
|---|---|---|
| DigitalOcean | 1 核 512M | $4 |
| Vultr | 1 核 512M | $3.5（纯 IPv6 $2.5） |
| 年付小厂（RackNerd 这类） | 1 核 1G | 折合 $1–2 |

Azure for Students 开出来的 B1s 也是一台普通 Linux，下面的步骤一样能用。

原来这儿写的 Hetzner 现在不能用了：2026 年 4 月和 6 月两轮涨价，
到 9 月所有共享 vCPU 机型（CX23 系列、ARM 的 CAX 系列）在官网上全部标着
"not available"，跟内存涨价是同一件事。

选机房的时候可以挑离她"人在的地方"近的，纯粹是心理作用，对功能没影响。

### 装

```bash
ssh root@你的服务器

# Docker
curl -fsSL https://get.docker.com | sh

# 代码
git clone https://github.com/Forever666123/New_person.git
cd New_person
git checkout claude/discord-virtual-character-qzc8a1
```

### 配

```bash
cp .env.example .env
nano .env
```

填三个必填项，然后确认这三行是正式值，不是调试值：

```
DELAY_SCALE=1.0
DEBUG_FORCE_AWAKE=0
DB_PATH=data/newperson.db
```

**调试开关留在线上是最容易犯的错**：`DELAY_SCALE=0.05` 会让她秒回，
`DEBUG_FORCE_AWAKE=1` 会让她不睡觉。这两个一开，这个项目就白做了。

### 跑

```bash
docker compose up -d --build
docker compose logs -f          # 看日志，Ctrl+C 退出不影响她
```

日志里认这几行：

```
[app] 沈亦宁 上线了。她那边 09-11 08:47，有空（你那边 22:47）
[app] 日志里的时间都是她那边的时间（America/New_York）
[life] 2026-09-11 的日程好了，排了 1 个主动时刻
```

### 更新代码

```bash
cd New_person && git pull && docker compose up -d --build
```

她的记忆在 `data/` 里，不受影响。

## 方案二：Fly.io

不用管系统，但要学一点它自己的概念。它 2024 年 10 月起没有免费档了，
这个配置（256M 机器 + 1G 卷）大概 $2.5 一个月。

```bash
brew install flyctl
fly auth login
fly launch --no-deploy          # 生成 fly.toml，它会认出 Dockerfile

# 记忆要放在持久卷上，不然每次重新部署就失忆
fly volumes create newperson_data --size 1 --region nrt

# 密钥不要写进 fly.toml
fly secrets set DISCORD_BOT_TOKEN=xxx ANTHROPIC_API_KEY=xxx OWNER_DISCORD_USER_ID=xxx
fly deploy
```

`fly.toml` 里要加上这段，把卷挂到她的数据目录：

```toml
[mounts]
  source = "newperson_data"
  destination = "/app/data"

[env]
  DB_PATH = "data/newperson.db"
  DELAY_SCALE = "1.0"
```

还要确认没有 `[http_service]` 那一段。她不监听任何端口，
留着的话 Fly 会因为健康检查失败反复重启她。

## 备份（这条最重要）

`data/newperson.db` 是她的全部记忆：你们说过的话、她记住的关于你的事、
交易台账、每天的日记。**这个文件没了，她就不认识你了。**

代码和人设都在 git 里，服务器炸了十分钟就能重来。这个文件不行，只此一份。

### 为什么不能直接 cp

数据库跑在 WAL 模式下，刚说过的话还躺在 `newperson.db-wal` 里，主文件里没有。
只拷主文件，你会得到一个"她还没听见你说话"的版本——而且它完全正常，
`integrity_check` 也过，你要等真的去恢复那天才发现少了一段。
三个文件一起拷又可能拷到写了一半的中间状态。

所以用 `scripts/backup.sh`，它走的是 SQLite 的在线备份接口：她一边写，
我们一边拷，拿到的仍然是某一个瞬间的一致快照。

### 配一次

```bash
apt install -y rclone gnupg

# 1) 远端。Backblaze B2 免费 10 GB，我们一年才用 2 MB
rclone config          # 新建一个 b2 远端，然后去 B2 网站建一个私有 bucket
rclone lsd b2:         # 能列出来就说明配对了

# 2) 加密口令。这个文件是你们全部的对话，不该以明文躺在别人的硬盘上
openssl rand -base64 32 > /root/.chloe-backup-pass
chmod 600 /root/.chloe-backup-pass

# 3) 抄一份口令到你的密码管理器里 —— 这一步别跳过
cat /root/.chloe-backup-pass

# 4) 填配置
cp scripts/backup.env.example scripts/backup.env
nano scripts/backup.env        # 至少改 RCLONE_REMOTE
```

**第 3 步是整段里最容易忽略的。** 口令跟备份存在同一台服务器上是没有意义的：
服务器没了，两个一起没，那些备份就是一堆你自己也打不开的乱码。

### 跑

```bash
scripts/backup.sh              # 手动跑一次，确认能通
scripts/restore.sh             # 演练：下载、解密、验，不碰线上那份
```

演练会把里面有什么打给你看——多少条消息、从哪天到哪天、她记住了多少事。
**看到数字对得上，才算你有备份。**

定时：

```bash
crontab -e
```

```
0 4 * * * /root/New_person/scripts/backup.sh >> /var/log/chloe-backup.log 2>&1
0 5 * * 0 /root/New_person/scripts/restore.sh >> /var/log/chloe-drill.log 2>&1
```

第二条是每周一次的恢复演练。备份最常见的死法不是没备份，
是**备了一年从来没人试过能不能恢复**。

### 它怎么防自己出事

- **传完会回头确认对面真的有这个文件**，并且比对大小。`rclone copy` 退出 0
  不等于东西到了。
- **只有真正传出去之后才写 `.last_backup_at`**，而 `!np status` 会报
  "上次备份多久之前"。备份停了是没有任何症状的——cron 的报错邮件没人看，
  令牌过期了一切照旧——所以把它放进你每天都会看的那个命令里。
- **拷出来当场跑 `integrity_check`**，坏了就删掉并退非零。一份坏备份比没有
  备份更危险，它让你以为自己有退路。
- **空备份不会顶掉旧的**。如果哪天 `DB_PATH` 指错了或者 `data/` 没挂上，
  拷出来的是个 0 条消息的完好数据库。这种时候它照传，但**不清理旧备份**、
  **不更新时间标记**，于是 `!np status` 会一直提醒你。否则一个月之后，
  三十份空备份就把所有真备份全顶掉了，全程没有一句报错。

### 真的要恢复的时候

```bash
scripts/restore.sh --list                    # 有哪些
scripts/restore.sh --at 20260912             # 先演练那一份
scripts/restore.sh --install --at 20260912   # 确认没问题再装回去
```

`--install` 会停容器、把当前那份改名留着（不删）、装上恢复的那份、再起来。
她会以为中间那段时间自己没看手机。

## 跑起来之后

日常操作全在 Discord 私聊里，不用登服务器：

| 命令 | 用途 |
|---|---|
| `!np status` | 她现在在干嘛、下一条回复排在几点、今天花了多少钱 |
| `!np ledger` | 她记下的、你在交易上说过的话 |
| `!np pause` / `resume` | 你不想被打扰的时候 |
| `!np away 出差 5` | 你出门几天，让她也安静点 |

她不回你的时候先看 `!np status`，多半是她在睡觉或者在上课。

## 花多少钱

| 项目 | 月费 |
|---|---|
| 服务器 | $0（Azure 学生 / 家里的机器）到 $4 |
| Claude API（中度聊天，Sonnet 5） | 约 $8 |

**大头是 API，不是服务器。** 在服务器上省下的那几美金，
比不上你少聊两天省下来的。

服务器的钱是固定的，API 的钱跟你聊多少直接相关，`!np status` 里能实时看到。
`MAX_CALLS_PER_DAY` 是硬上限，超了她就当今天没怎么看手机，不会偷偷烧钱。

## 会踩的几个坑

**她一直显示离线。** 正常，她睡觉时就是离线的。`!np status` 看真实状态。

**改了 persona.yaml 没生效。** 人设是只读挂载的，改完要
`docker compose restart`。

**容器一直重启。** `docker compose logs --tail 50` 看最后的报错。
多半是 `.env` 里少了东西，或者 API key 过期。

**她突然不说话了。** 先 `!np status` 看最近一次接口出错是什么，
再看是不是撞到了 `MAX_CALLS_PER_DAY`。

**换服务器。** 把 `data/` 目录整个拷过去就行，她的记忆全在里面。
