# abema-recorder

容器内归档 ABEMA 流。两个引擎按 URL 自动分流：

| 引擎 | 源 | 加密 | 产物 |
|---|---|---|---|
| DASH | `.mpd` 清单（时移/付费直播） | Widevine | 解密分片 + 合并的 mkv（时移）或 ts（直播） |
| HLS | 频道页、`now-on-air` 页、裸 m3u8 | AES-128 | 逐分片规范化后增长的 ts |

## 分流规则

| `--url` 形态 | 引擎 |
|---|---|
| `*.mpd`，或路径含 `dash` | DASH |
| `abema.tv/now-on-air/...`、`abema.tv/channels/...`、其他 abema.tv 页面 | HLS（经 Streamlink 解析） |
| 裸 `*.m3u8` | HLS（直接作为媒体地址，跳过解析） |
| 其他 | 先试 Streamlink，失败回退 DASH |

`--engine dash` / `--engine hls` 可强制指定引擎，跳过自动分流（配置文件里写 `engine=` 等价）。

`keys` / `backfill` / `rebuild` 仅适用 DASH；对 HLS 源或 HLS 运行记录执行会报错退出。

## DASH 引擎功能

| 能力 | 说明 |
|---|---|
| 密钥获取 | 用本地 CDM 重放 license 请求，取出 content key |
| 覆盖校验 | 校验密钥是否覆盖实际会被录制的轨道 |
| 轮换监视 | 录制期间定期复查，直播中途换密钥会告警 |
| 下载解密 | N_m3u8DL-RE + shaka-packager |
| 实时解密 | 时移录制中分片随到随解密，只保留解密副本，结尾直接合并出成品 |
| 边录边播 | 仅直播：混流文件按播放速率写入，录制中即可播放 |
| 本地 HLS | 直播录制默认在 127.0.0.1:8080 生成滚动 m3u8 |
| 分片保留 | 默认跟踪已解密分片并同步到共享目录，中断后可重建 |
| 同链共享 | 同一播放链接跨重启沿用第一次录制的分片目录，每次输出仍独立命名 |
| CDN 补片 | 自动探测连续分片索引，只下载并解密本地缺少的部分 |
| 重建 | 从分片重建可播放文件，逐轨报告缺失 |

## HLS 引擎功能

| 能力 | 说明 |
|---|---|
| 地址解析 | Streamlink 的 ABEMA 插件解析频道页，裸 m3u8 直接使用 |
| 本地代理 | 转发播放列表、分片与 AES-128 密钥 |
| 时间线合并 | 每个分片经 ffmpeg 规范化（连续 PTS/DTS、统一 PID、48kHz 立体声 AAC）后追加进同一个 ts |
| 播放缓冲 | 按媒体时间匀速发布分片，吸收广告段的发布停顿 |

## 部署

需要 Docker 和 Docker Compose。

### 1. 准备配置文件

项目根目录放一个 `.env`。DASH 源：

```ini
mpd=https://vod-playout-abematv.akamaized.net/.../index.mpd
token=https://license.p-c3-e.abema-tv.com/playout/widevine?t=...&pt=...
```

HLS 源：

```ini
url=https://abema.tv/now-on-air/abema-special
quality=1080p
```

DASH 两个 URL 的复制方法（Edge）：

1. 打开播放页，F12 打开开发者工具，切到 Network。
2. 过滤器输入 `mpd`，找到 `index.mpd` 请求，右键 Copy → Copy link address，填 `mpd`。
3. 过滤器输入 `widevine`，找到对 `license.p-c3-e.abema-tv.com/playout/widevine`
   的 POST 请求，复制完整 URL 填 `token`。

`token` 也接受另外两种写法：只粘贴 `t=...&pt=...` 查询串，或只粘贴 `pt=` 后面的
JWT 本体。`pt` JWT 有效期约 26 小时，过期需重新复制。

### 2. 准备 CDM（仅 DASH）

把 Widevine 设备文件放在项目根目录，命名 `device.wvd`。项目不附带此文件。
HLS 运行不使用它。

没有 CDM 时可以跳过这步，改用 `--key` 直接提供密钥。此时需要注释掉 `compose.yaml` 里这一行，
否则 Docker 会在源位置创建目录：

```yaml
# - ./device.wvd:/config/device.wvd:ro
```

### 3. 构建

```powershell
docker compose build
```

自检：

```powershell
docker compose run --rm recorder probe
```

### 4. 开始录制

```powershell
docker compose up
```

按 `Ctrl+C` 停止。时移活动在直播结束后尽快录制：清单与分片在 CDN 上的保留时间有限。

## 命令

| 命令 | 作用 |
|---|---|
| `capture` | 按 URL 分流后录制 |
| `plan` | 同上但只打印将执行的命令，不实际运行 |
| `keys` | 只取密钥并打印覆盖情况（仅 DASH） |
| `backfill` | 探测 CDN 可访问索引并补齐本地缺片（仅 DASH） |
| `rebuild` | 从保留的分片重建可播放文件（仅 DASH） |
| `proxy` | 只运行 HLS 引擎的本地播放列表代理，不录制 |
| `probe` | 自检：工具链、CDM、streamlink；配置了 HLS 源时加做网络检查 |

## 参数

`capture` / `plan` / `keys` 共用：

| 参数 | 说明 |
|---|---|
| `--url URL` | 流地址，按上表分流 |
| `--engine {auto,dash,hls}` | 强制引擎，跳过自动分流，默认 `auto` |
| `--quality Q` | HLS：Streamlink 清晰度，默认 `1080p`。DASH：指定录制清晰度（`2160p`/`1080p`/`720p`/`480p`/`360p`），不写则最高码率 |
| `--record-limit HH:MM:SS` | 直播录制时长上限，传给下载器 |
| `--out DIR` | 输出目录，默认 `run_<时间戳>.out` |
| `--token TOK` | license token：完整 license URL、`t=...&pt=...` 查询串、或裸 pt JWT（仅 DASH） |
| `--license-url URL` | 完整 license 地址，优先于 `--token`（仅 DASH） |
| `--headers-file FILE` | `--license-url` 用的请求头，每行 `Name: value`（仅 DASH） |
| `--key KID:KEY` | 直接提供密钥，可重复或逗号分隔。跳过 license 步骤（仅 DASH） |
| `--cdm PATH` | 设备文件路径，默认 `/config/device.wvd`（仅 DASH） |
| `--decryptor {SHAKA_PACKAGER,MP4DECRYPT}` | 解密引擎，默认 `SHAKA_PACKAGER`（仅 DASH） |
| `--allow-partial-keys` | 允许部分轨道无密钥，那些轨道不会被解密（仅 DASH） |
| `--discard-shards` | 不保留分片，省一半磁盘；`rebuild` 不可用 |
| `--burst-output` | 尽快落盘，不按播放速率（录制中的文件将无法播放；DASH 直播同时关闭默认 m3u8 服务） |
| `--hls [HOST:PORT]` | 本地播放列表服务地址，可只写端口。DASH 直播默认开启；DASH 时移忽略；HLS 引擎始终提供 |
| `--quiet-shards` | 不在控制台打印逐分片进度 |
| `--no-shard-log` | 不写逐分片进度日志；同链接共享同步仍会运行（仅 DASH） |
| `--verbose-downloader` | 显示下载器自身的日志 |
| `--guard-interval S` | 密钥轮换复查间隔，默认 240 秒（仅 DASH） |
| `--settings FILE` | 配置文件路径。未指定时：设了 `$ABM_SETTINGS` 就只用它，否则找 `./.env` 或 `./.abema` |

`backfill` / `rebuild` / `probe` 也接受 `--settings`，只是配置文件总会被加载；
写在其后时由 argparse 接受并忽略，写在前时同样生效（`abema-recorder --settings FILE backfill ...`）。

退出码：`0` 成功，`1` 环境/工具链检查未通过，`2` 运行时错误，`130` 用户中断。

`backfill` 参数：

| 参数 | 说明 |
|---|---|
| `TARGET` | 运行的 `.out` 目录，或对应分片目录 |
| `--rate-limit RATE` | 顺序下载限速，默认 `3M`；可写 `512K`、`3M`，`0` 表示不限速 |
| `--scan-only` | 只报告 CDN 范围和本地缺片数量，不下载 |
| `--include-live-tail` | 录制仍在进行时也补最新尾部 |

## 本地 m3u8 实时播放

| 引擎 | 地址 | 说明 |
|---|---|---|
| DASH 直播 | `http://127.0.0.1:8080/live.m3u8` | 录制默认开启；滚动保留最近 6 个分片，只用于实时观看 |
| HLS | `http://127.0.0.1:8080/index.m3u8` | 规范化后的匀速播放列表；另有 `/source.m3u8`（原始转发）与 `/clean.m3u8`（去掉 discontinuity） |

```powershell
ffplay -fflags nobuffer -flags low_delay "http://127.0.0.1:8080/live.m3u8"
```

容器内服务绑定 `0.0.0.0:8080`，compose 映射到宿主机回环地址。DASH 时移源不产生滚动
播放列表，`--hls` 被忽略并打印一行提示。完整内容始终归档到录制的 `.ts` 文件。

## 自动补齐 CDN 切片（DASH）

把运行的 `.out` 目录作为 `TARGET`。程序会从 `run.json` 找到共享分片目录，读取
`meta_selected.json` 中的 CDN URL 模板，再自动完成以下操作：

1. 用 HEAD 请求定位 CDN 当前仍可访问的最早、最晚索引。
2. 把共享目录中的本地时间戳映射回 CDN 索引。
3. 只下载本地没有的索引，已有分片不会重复下载。
4. 用 `keys.txt` 和对应 init 解密，写回同一播放链接的共享目录。
5. 从分片内部 `tfdt` 校准文件名，保留音频的毫秒级边界。

### 1. 先扫描

```powershell
docker compose run --rm recorder backfill /archive/run_20260823_091907.out --scan-only
```

输出分别列出音视频的 CDN 首尾、检查到的最后索引、本地数量、缺失数量：

```text
video: CDN 1..3630; checked through 3630; local 3519; missing 111; recovered 0
audio: CDN 1..3623; checked through 3623; local 3511; missing 112; recovered 0
```

### 2. 限速补齐

```powershell
docker compose run --rm recorder backfill /archive/run_20260823_091907.out --rate-limit 3M
```

可以反复执行同一命令。已解密落盘的索引会被跳过；网络中断或部分失败后重跑即可继续。

### 同一链接的共享目录（DASH）

同一播放 URL 再次录制时，下载器仍使用新的 `run_<时间>` 会话目录，完成的 init 和解密
分片会以硬链接同步到该 URL 第一次录制的分片目录。各次输出保持独立，而 `run.json`、
`backfill` 和 `rebuild` 都指向共享目录。硬链接位于同一归档卷时不重复占用媒体数据空间。

补片要求共享目录仍保留 init 和 `meta_selected.json`，对应 `.out` 目录仍保留 `run.json`
与 `keys.txt`。

补齐完成后可直接从共享分片重建：

```powershell
docker compose run --rm recorder rebuild /archive/run_20260823_091907.out
```

## 常见用法

```powershell
# 默认录制
docker compose up

# 已有密钥的离线录制，不需要 CDM
docker compose run --rm recorder capture --url "<mpd>" --key kid:key

# 录制频道直播（HLS 引擎），720p，只录一小时；--service-ports 把播放端口映射到宿主机
docker compose run --rm --service-ports recorder capture --url "https://abema.tv/now-on-air/abema-special" --quality 720p --record-limit 01:00:00

# 只取密钥并检查覆盖，不录制
docker compose run --rm recorder keys --url "<mpd>" --token "<license-url>"

# 只运行 HLS 代理，边下边播交给别的播放器（同样要 --service-ports）
docker compose run --rm --service-ports recorder proxy --url "https://abema.tv/now-on-air/abema-special"

# 先扫描，不下载
docker compose run --rm recorder backfill /archive/run_20260823_091907.out --scan-only

# 从中断的录制重建
docker compose run --rm recorder rebuild /archive/run_20260803_091917.out
```

## 配置

优先级：命令行参数 > 环境变量 > 配置文件 > 默认值。

配置文件只认 `KEY=value`，支持短别名：

| 别名 | 规范变量 |
|---|---|
| `mpd` / `url` / `stream` | `ABM_URL` |
| `token` / `pt` / `license_token` | `ABM_TOKEN` |
| `license` / `license_url` | `ABM_LICENSE_URL` |
| `key` / `keys` | `ABM_KEYS` |
| `cdm` / `device_wvd` | `ABM_CDM` |
| `out` / `output` | `ABM_OUT` |
| `headers` | `ABM_HEADERS` |
| `hls` / `hls_address` | `ABM_HLS` |
| `quality` | `ABM_QUALITY` |
| `engine` | `ABM_ENGINE` |
| `ABEMA_URL` / `abema_url` | `ABM_URL` |
| `ABEMA_QUALITY` / `abema_quality` | `ABM_QUALITY` |
| `ABEMA_PROXY_PORT` / `abema_proxy_port` | `ABM_HLS`（端口映射为 `127.0.0.1:端口`） |

其余变量：`ABM_SETTINGS`、`ABM_DOWNLOADER`、`ABM_FFMPEG`、`ABM_SHAKA`、
`ABM_MP4DECRYPT`、`HTTPS_PROXY`、`NO_COLOR`。
