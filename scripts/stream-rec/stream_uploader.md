# stream_uploader.py 使用说明

通用直播录像上传/同步脚本,合并自原 `kelie_upload.py` + `kelie_rclone.py`。
多主播共用,在各自 systemd service 的 `ExecStart` 里传入不同参数即可。

---

## 目录

- [一、四种运行模式](#一四种运行模式)
- [二、参数总览](#二参数总览)
  - [1. 主播身份](#1-主播身份--必填所有模式)
  - [2. 运行模式](#2-运行模式)
  - [3. B 站投稿](#3-b-站投稿--upload--upload-only--rclone-模式生效)
  - [4. rclone](#4-rclone--rclone--rclone-only-模式生效)
  - [5. 磁盘清理](#5-磁盘清理--仅-mode-upload-生效)
  - [6. 循环行为](#6-循环行为)
- [三、模块级常量](#三模块级常量)
- [四、目录与日志文件](#四目录与日志文件)
- [五、内置自动行为](#五内置自动行为)
- [六、systemd service 模板](#六systemd-service-模板)
- [七、常见问题排查](#七常见问题排查)

---

## 一、四种运行模式

| `--mode` | B 站投稿 | 投稿后处理 | 必填项 |
|---|---|---|---|
| `upload` (默认) | ✅ | 移到 `backup/{yyyy-mm-dd}/`,触发空间清理 | `--cookie` `--tag` |
| `upload-only` | ✅ | 直接删除本地文件(含 `.ass` / `.xml`) | `--cookie` `--tag` |
| `rclone` | ✅ | `rclone move` 到云端,本地随之消失 | `--cookie` `--tag` `--rclone-zone` |
| `rclone-only` | ❌ | 跳过 B 站,只 `rclone move` 到云端 | `--rclone-zone` |

---

## 二、参数总览

共 18 个 CLI 参数。⚠️ 表满足该 mode 时必填,其它都有默认值。

### 1. 主播身份 — 必填,所有模式

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `--up` ⚠️ | str | — | 主播英文标识,只在日志/启动信息里露出,无业务作用 |
| `--up-name` ⚠️ | str | — | 主播中文名,会拼到投稿标题 `【{up-name}】yyyy-mm-dd 录播` |
| `--base-dir` ⚠️ | path | — | 主播工作根目录,自动派生 `upload/`、`backup/`、`main.log`、`upload.log`、`upload_danmu.log`、`rclone.log` |

### 2. 运行模式

| 参数 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `--mode` | `upload` / `upload-only` / `rclone` / `rclone-only` | `upload` | 决定整体行为(见上节) |

### 3. B 站投稿 — `upload` / `upload-only` / `rclone` 模式生效

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `--cookie` ⚠️ | path | `""` | biliup-rs cookie 文件绝对路径 |
| `--tag` ⚠️ | str | `""` | 稿件标签,**逗号分隔**,例 `录播,直播回放` |
| `--desc` | str | `""` | 稿件简介 |
| `--source` | str | `""` | 转载来源(URL),仅 `--copyright 2` 时拼到命令行 |
| `--tid` | int | `171` | B 站分区 tid (171 = 单机游戏) |
| `--copyright` | `1` / `2` | `1` | 1 自制 / 2 转载 |
| `--line` | str | `txa` | biliup-rs 上传线路: `bda2`/`tx`/`txa`/`bldsa` 等 |
| `--limit` | int | `8` | biliup-rs `--limit` 并发分片数,**仅 `.flv` 使用**,`.mp4` 弹幕版不传 |
| `--biliup-bin` | path | `biliup-rs` | biliup-rs 可执行文件路径,建议绝对路径(如 `/opt/stream-rec/biliup-rs`) |
| `--title-suffix` | str | `""` | 标题尾巴,会以 ` + suffix` 形式追加,常用于游戏名(如 `永劫无间`) |

**生成的标题格式**

- `.flv` → `【{up-name}】{date} 录播 {title-suffix}`
- `.mp4` → `【{up-name}丨弹幕版】{date} 录播 {title-suffix}`

### 4. rclone — `rclone` / `rclone-only` 模式生效

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `--rclone-zone` ⚠️ | str | `""` | rclone 远端,**两种写法都支持**:`kelie_backup` 或 `openlist:/stream-rec/重楼_bak`。脚本自动识别有无 `:` 决定拼接方式 |
| `--rclone-chunk-size` | str | `100M` | 透传给 rclone 的 `--onedrive-chunk-size`(其他后端不影响) |
| `--rclone-extra` | str | `""` | 拼到 rclone 命令末尾的原始字符串,可塞 `--bwlimit 50M --transfers 4` 之类 |

**远端路径拼接规则** (`rclone_remote_path()`)

| `--rclone-zone` 写法 | 实际目录 |
|---|---|
| `kelie_backup` | `kelie_backup:/2026-04-25` |
| `openlist:/stream-rec/重楼_bak` | `openlist:/stream-rec/重楼_bak/2026-04-25` |
| `openlist:/stream-rec/重楼_bak/` | `openlist:/stream-rec/重楼_bak/2026-04-25`(末尾 `/` 会被去掉) |

### 5. 磁盘清理 — 仅 `--mode upload` 生效

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `--cleanup-threshold` | float | `20.0` | `backup/` 所在分区可用空间 < 此 % 时,**每轮循环删除 1 个最老子目录**(按 `ctime`)。`0` 关闭 |

### 6. 循环行为

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `--interval` | int | `30` | 主循环每轮 sleep 秒数。空闲就等;有文件就跑完任务再 sleep |
| `--reset-time` | str | `08:00` | 每天该时刻删除 `upload.log` / `upload_danmu.log` / `rclone.log`,**让下一次上传当作"新一场直播"开新稿件**。空串关闭 |

---

## 三、模块级常量

改源码才能改,不是 CLI 参数:

| 常量 | 值 | 说明 |
|---|---|---|
| `MAX_RETRIES` | `3` | B 站上传失败的最大重试次数,超过则删本地文件并跳过 |
| `RETRY_WAIT` | `10` | 两次重试之间 sleep 的秒数 |

---

## 四、目录与日志文件

`--base-dir` 自动派生出以下结构:

```
{base-dir}/
├── upload/                  # 待上传文件投放点 (.flv/.mp4 + .xml/.ass)
├── backup/                  # 仅 --mode upload 使用
│   └── 2026-04-25/          # 按直播日期归档
├── main.log                 # 业务主日志
├── upload.log               # flv 投稿 biliup-rs 输出 + bvid 锚点
├── upload_danmu.log         # mp4 弹幕版的同上
└── rclone.log               # rclone 输出 + 本场直播首次同步锚点
```

| 文件 | 作用 | 何时清空 |
|---|---|---|
| `main.log` | 业务主日志(启动/上传/重试/删除/rclone) | 永不自动清空,需要时自行 `truncate` |
| `upload.log` | flv 投稿的 biliup-rs 输出,**用于解析 bvid 触发追加分 P** | 每天 `--reset-time` / 重试 3 次失败 |
| `upload_danmu.log` | mp4 弹幕版的同上 | 同上 |
| `rclone.log` | rclone 命令输出 + "本场首次同步"锚点 | 每天 `--reset-time` |

---

## 五、内置自动行为

无对应参数,但行为固定:

- **重试逻辑**:每个文件最多上传 3 次,间隔 10 s;3 次后 `remove_files()`,日志含文件大小,`upload.log` 同时清掉,让下个文件重新发起新稿件
- **bvid 缺失自动降级**:`upload.log` 存在但解析不到 bvid → 当作全新投稿(先删除残缺日志再上传)
- **sidecar 同步**:`.flv` 联动 `.xml`,`.mp4` 联动 `.ass`;处理动作(删除/move-backup/rclone)对它们一视同仁
- **首次 mkdir**:rclone 首次同步本场时先 `rclone mkdir` 创建远端目录,后续同场不再 mkdir(靠 `rclone.log` 锚点判定)
- **新场识别**:删除 `upload.log` / `rclone.log` 即视为新一场直播,会重新建稿/重新建远端目录

---

## 六、systemd service 模板

> ⚠️ **`\` 续行规则**:`\` 后**严禁任何字符**(包括空格/制表符),否则被当字面量传给 python。
> 写完后用 `cat -A *.service | grep '\\'` 检查,期望看到 `\$`,不能是 `\ $`。
> 一键清理尾空格:`sed -i 's/[[:space:]]\+$//' *.service`

### 模式 1 — `upload`(本地按日期备份 + 空间不足自动清理)

```ini
[Unit]
Description=Stream-Rec Upload+Backup - 克烈
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/stream-rec/stream_uploader.py \
    --up kelie \
    --up-name 克烈 \
    --base-dir /opt/stream-rec/克烈 \
    --mode upload \
    --cookie /opt/stream-rec/cookies-qn.json \
    --tag "永劫无间,克烈,录播,直播回放" \
    --desc "克烈直播间" \
    --title-suffix 永劫无间 \
    --tid 171 \
    --copyright 1 \
    --line txa \
    --limit 8 \
    --biliup-bin /opt/stream-rec/biliup-rs \
    --cleanup-threshold 20 \
    --interval 30 \
    --reset-time 08:00
Restart=always
RestartSec=10
User=root
Group=root

[Install]
WantedBy=multi-user.target
```

### 模式 2 — `upload-only`(传完即删)

```ini
[Unit]
Description=Stream-Rec Upload-Only - 不会捏蓝
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/stream-rec/stream_uploader.py \
    --up nielan \
    --up-name 不会捏蓝 \
    --base-dir /opt/stream-rec/不会捏蓝 \
    --mode upload-only \
    --cookie /opt/stream-rec/cookies-qn.json \
    --tag "录播,直播回放" \
    --desc "不会捏蓝直播间" \
    --tid 171 \
    --copyright 1 \
    --line txa \
    --limit 8 \
    --biliup-bin /opt/stream-rec/biliup-rs \
    --interval 30 \
    --reset-time 08:00
Restart=always
RestartSec=10
User=root
Group=root

[Install]
WantedBy=multi-user.target
```

### 模式 3 — `rclone`(传完后云端备份)

```ini
[Unit]
Description=Stream-Rec Upload+Rclone - 重楼
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/stream-rec/stream_uploader.py \
    --up chonglou \
    --up-name 重楼 \
    --base-dir /opt/stream-rec/重楼 \
    --mode rclone \
    --cookie /opt/stream-rec/cookies-OutPrivate.json \
    --tag "录播,直播回放" \
    --desc "重楼直播间" \
    --tid 171 \
    --copyright 1 \
    --line txa \
    --limit 8 \
    --biliup-bin /opt/stream-rec/biliup-rs \
    --rclone-zone openlist:/stream-rec/重楼_bak \
    --rclone-chunk-size 100M \
    --interval 30 \
    --reset-time 08:00
Restart=always
RestartSec=10
User=root
Group=root

[Install]
WantedBy=multi-user.target
```

### 模式 4 — `rclone-only`(纯云端备份,不投稿)

```ini
[Unit]
Description=Stream-Rec Rclone-Only - 疯尤金
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/stream-rec/stream_uploader.py \
    --up fengyoujin \
    --up-name 疯尤金 \
    --base-dir /opt/stream-rec/疯尤金 \
    --mode rclone-only \
    --rclone-zone openlist:/stream-rec/fyj_bak \
    --rclone-chunk-size 100M \
    --interval 30
Restart=always
RestartSec=10
User=root
Group=root

[Install]
WantedBy=multi-user.target
```

### service 部署流程

```bash
# 1. 写完 service 文件后清掉行末尾的空白
sed -i 's/[[:space:]]\+$//' /etc/systemd/system/<svc>.service

# 2. 检查行尾是否干净 (期望看到 \$)
cat -A /etc/systemd/system/<svc>.service | grep '\\'

# 3. 重载 + 启动
systemctl daemon-reload
systemctl enable <svc>.service
systemctl restart <svc>.service

# 4. 观察
systemctl status <svc>.service
journalctl -u <svc>.service -f
tail -f /opt/stream-rec/<base-dir>/main.log
```

---

## 七、常见问题排查

### Q1. service 启动后报 `unrecognized arguments: \`

`\` 后有尾空格,systemd 没识别成续行符。用 `sed -i 's/[[:space:]]\+$//' file.service` 清掉,或干脆把 `ExecStart` 写成单行。

### Q2. 上传失败但日志没有 bvid,后续启动一直在追加失败

脚本会自动检测:`upload.log` 存在但读不到 bvid → 视为残缺,直接删除并改走全新投稿。无需手动干预。

### Q3. rclone 远端路径多了一个冒号

旧版本对 `--rclone-zone openlist:/stream-rec/重楼_bak` 会拼成 `openlist:/stream-rec/重楼_bak:/2026-04-25`。当前版本已修复,识别有无 `:` 自动选拼接方式(详见 [4. rclone](#4-rclone--rclone--rclone-only-模式生效) 节)。

### Q4. backup 磁盘满了,但 cleanup 没生效

- 确认 `--mode upload`,清理只在该模式触发
- 确认 `--cleanup-threshold` 不为 `0`
- 一次循环只删 1 个最老子目录,空间不足要等下一轮(默认 30 s)
- `--cleanup-threshold` 计算的是 `backup/` 所在分区,如果 backup 单独挂盘,以那块盘的总量为基准

### Q5. 想跳过某场直播的 append,直接发新稿

删除对应的 `upload.log` / `upload_danmu.log` 即可:

```bash
rm /opt/stream-rec/<主播>/upload.log
```

下个循环就会当作新一场直播,自动发起全新投稿。`--reset-time` 默认 08:00 也是这个机制。

### Q6. 想观察具体执行了什么 rclone / biliup-rs 命令

`main.log` 里每条命令都会以 `执行 ...` 形式打印;biliup-rs / rclone 自身的 stdout/stderr 在对应的 `upload.log` / `rclone.log`。
