# 部署到服务器

本地跑的问题是电脑一关她就下线了。放到服务器上她能一直在，
你半夜想起一笔交易随手说一句，她第二天早上会接。

## 先说结论

**一台最便宜的 VPS，用 docker compose 跑。** 一个月三四十块，全程你自己控制。

理由：她要维持一条 Discord 长连接，不能休眠，所以 Vercel、Cloudflare Workers
这类无服务器的都不行；Railway、Render 的免费档会把闲置容器睡掉，
睡掉就等于她下线了。她本身只是一个 Python 进程加一个 SQLite 文件，
最低配的机器绰绰有余。

如果你不想碰系统运维，Fly.io 也行，往下翻。

## 方案一：VPS

任何一家都行，最低配就够。参考价：

| 服务商 | 配置 | 月费 |
|---|---|---|
| Hetzner CX22 | 2 核 4G | 约 €3.8 |
| DigitalOcean | 1 核 512M | $4 |
| Vultr | 1 核 512M | $2.5 |

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

不用管系统，但要学一点它自己的概念。

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

VPS 上加一条定时任务：

```bash
crontab -e
```

```
0 4 * * * cd ~/New_person && sqlite3 data/newperson.db ".backup '/root/backup/np-$(date +\%F).db'" && find /root/backup -name 'np-*.db' -mtime +14 -delete
```

用 `.backup` 而不是直接 `cp`，因为她随时可能在写。留两周，够了。

再往上一层是把备份同步到别处（rclone 到网盘之类），
看你觉得这段记忆值多少。

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
| 服务器 | ￥25 到 ￥30 |
| Claude API（中度聊天，Sonnet 5） | 约 $8 |

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
