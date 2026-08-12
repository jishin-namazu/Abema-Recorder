# ABEMA Recorder

ABEMA Recorder 是一个面向 ABEMA 直播频道的本地 HLS 代理与录制工具。项目通过 Streamlink
解析频道播放地址，由 N_m3u8DL-RE 持续下载并解密媒体分片，再使用 FFmpeg 对每个分片执行
时间戳、流布局和音频参数标准化，最终生成可持续增长的 MPEG-TS 文件以及可供 OBS、VLC、
PotPlayer 等客户端直接读取的本地 HLS 播放列表。

项目会保留正片与广告内容，并针对 ABEMA 在广告切入、广告内部切换和广告结束时可能出现的
`#EXT-X-DISCONTINUITY`、PTS/DTS 回跳、PID 变化、音频采样率变化和分片突发发布进行处理。

## 处理流程

```text
ABEMA 频道页面
    │
    ▼
Streamlink ABEMA 解析器
    │
    ▼
本地 HLS 代理
    │  保留广告、转发播放列表、媒体分片与 AES-128 密钥
    ▼
N_m3u8DL-RE
    │  持续下载并解密原始分片
    ▼
FFmpeg 分片标准化
    │  连续 PTS/DTS、统一 PID、48 kHz 双声道 AAC
    ├──────────────────────► 持续增长的 MPEG-TS 录制文件
    │
    ▼
本地播放缓冲区
    │  预缓冲并按媒体时长匀速发布
    ▼
http://127.0.0.1:18081/index.m3u8
```

## 主要特性

- 解析 ABEMA `now-on-air` 频道 URL，并选择指定清晰度的 HLS 媒体流。
- 转发 ABEMA HLS 播放列表、AES-128 密钥和媒体资源。
- 保留 `/tsad/` 广告分片，不因广告状态切换而终止下载或合并。
- 识别并记录 `/tslive/ → /tsad/ → /tslive/` 状态转换。
- 对每个已下载分片独立执行 MPEG-TS 重封装，避免原始 PTS/DTS 回跳传播至输出文件。
- 跨分片维护 MPEG-TS continuity counter，降低播放器将边界识别为损坏数据包的概率。
- 将正片与广告音频统一为 AAC、48 kHz、双声道、192 kbit/s，避免 48 kHz 与 44.1 kHz
  参数切换触发播放器重新初始化音频解码链路。
- 视频流采用 stream copy，不执行视频重新编码。
- 默认预缓冲 10 个标准化分片，初始发布 3 个分片，后续依据媒体时长匀速发布，用于吸收
  广告期间约 20～25 秒的分片下载停顿或批量到达。
- 同时生成持续增长的 MPEG-TS 文件和滑动窗口 HLS 播放列表。
- 默认仅将服务端口映射至宿主机 `127.0.0.1`。

## 运行要求

推荐使用 Docker Compose：

- Docker Desktop 或兼容 Docker Engine
- Docker Compose v2
- 能够访问 ABEMA 频道及其媒体资源的网络环境

直接在宿主机运行时还需要：

- Python 3.11 或更高版本
- FFmpeg 与 FFprobe
- N_m3u8DL-RE

## 快速开始

### 1. 创建配置文件

```powershell
Copy-Item .env.example .env
```

编辑 `.env`：

```ini
ABEMA_URL=https://abema.tv/now-on-air/luckyfes
ABEMA_QUALITY=1080p
ABEMA_PROXY_PORT=18081
```

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `ABEMA_URL` | ABEMA 频道页面 URL | `https://abema.tv/now-on-air/luckyfes` |
| `ABEMA_QUALITY` | 目标清晰度 | `1080p` |
| `ABEMA_PROXY_PORT` | 宿主机与容器使用的本地 HLS 端口 | `18081` |

### 2. 构建镜像

```powershell
docker compose build
```

### 3. 验证播放链路

```powershell
docker compose run --rm --service-ports recorder probe
```

`probe` 会验证：

- ABEMA 页面与媒体播放列表解析；
- 本地播放列表访问；
- AES-128 密钥转发；
- FFprobe 对音视频流的读取。

### 4. 开始录制

```powershell
docker compose up
```

录制期间可通过以下地址播放经过标准化的本地 HLS：

```text
http://127.0.0.1:18081/index.m3u8
```

使用 `Ctrl+C` 停止录制。容器的 init 进程会将终止信号转发给下载器，使其结束当前任务。

> [!NOTE]
> 标准化播放列表需要先积累 10 个分片。启动后 `/index.m3u8` 在约 40～60 秒内可能暂时不含
> 媒体分片，具体时间取决于上游分片时长与下载速度。这段延迟用于建立可覆盖广告切换停顿的
> 播放储备。

## 播放列表端点

### `capture` 模式

| 端点 | 用途 | `#EXT-X-DISCONTINUITY` | 媒体处理 |
| --- | --- | --- | --- |
| `/index.m3u8` | OBS、VLC、PotPlayer 等播放器 | 不输出 | 解密、连续时间轴、统一 PID、48 kHz AAC、匀速发布 |
| `/source.m3u8` | N_m3u8DL-RE 内部下载源 | 保留 | 转发原始媒体分片 |
| `/clean.m3u8` | 需要原始分片但不接受 discontinuity 标签的下游 | 移除 | 转发原始媒体分片 |

推荐播放器始终使用 `/index.m3u8`。仅删除 `#EXT-X-DISCONTINUITY` 并不能修复原始媒体中的
PTS/DTS 回跳、PID 变化或音频参数变化，因此 `/clean.m3u8` 不适合作为可靠的跨广告播放源。

### `proxy` 模式

单独运行 `proxy` 时不会启动下载器和时间轴标准化器，因此 `/index.m3u8`、`/source.m3u8`
均为上游 HLS 的本地转发版本。该模式适合协议检查和临时代理，不提供录制模式下的连续时间轴、
音频标准化或播放预缓冲。

```powershell
docker compose run --rm --service-ports recorder proxy
```

## OBS 配置

录制服务启动并完成预缓冲后，在 OBS 中添加“媒体源”：

1. 取消选中“本地文件”。
2. 在“输入”中填写 `http://127.0.0.1:18081/index.m3u8`。
3. 网络缓冲可从 `2000 ms` 开始配置。
4. 不启用“文件结束后循环”。

服务端已经维护额外的播放储备。播放器侧缓冲主要用于吸收本机调度与网络读取抖动，无需设置为
与服务端预缓冲相同的时长。

## 命令行用法

### 查看帮助

```powershell
docker compose run --rm recorder --help
docker compose run --rm recorder capture --help
```

### 输出下载计划但不执行

```powershell
docker compose run --rm recorder plan
```

### 限时录制

以下示例录制 60 秒：

```powershell
docker compose run --rm --service-ports recorder capture `
  --record-limit 00:01:00
```

### 指定频道、清晰度和输出目录

```powershell
docker compose run --rm --service-ports recorder capture `
  --url https://abema.tv/now-on-air/luckyfes `
  --quality 1080p `
  --out /archive/luckyfes.out
```

### 合并后删除原始下载分片

```powershell
docker compose run --rm --service-ports recorder capture `
  --discard-segments
```

该参数只删除已成功处理的原始下载分片，不影响最终 MPEG-TS 文件和本地 HLS 当前窗口所需的
标准化分片。

### 输出详细日志

全局参数必须位于子命令之前：

```powershell
docker compose run --rm --service-ports recorder --verbose capture `
  --verbose-downloader
```

## 输出文件

默认输出位于 `archive/`：

```text
archive/
├── abema_YYYYMMDD_HHMMSS.out/
│   ├── abema_YYYYMMDD_HHMMSS.ts    # 持续增长的最终录制文件
│   ├── downloader.log              # N_m3u8DL-RE 日志
│   ├── run.json                    # 本次任务参数与媒体信息
│   └── .timeline-normalized/       # 本地 HLS 滑动窗口使用的标准化分片
└── abema_YYYYMMDD_HHMMSS/          # 下载器保存的原始解密分片
```

默认保留原始下载分片。使用 `--discard-segments` 后，成功写入最终文件的原始分片会被删除。

## 广告切换与时间轴处理

典型日志如下：

```text
initial mode=tslive sequence=30600
transition tslive -> tsad sequence=30611 discontinuity=True
discontinuity within tsad sequence=30614
transition tsad -> tslive sequence=30629 discontinuity=True
```

这些日志表示代理在原始 HLS 中观察到了内容模式或编码时间线边界，不代表录制任务已经停止。
处理过程如下：

1. `/source.m3u8` 保留原始 `#EXT-X-DISCONTINUITY`，使下载器能够正确识别上游边界。
2. 下载器持续保存正片与广告分片，不执行最终合并。
3. FFmpeg 将每个完整分片独立重封装，并通过 `output_ts_offset` 平移至连续时间轴。
4. 相邻分片之间保留一个 MPEG-TS 90 kHz 时钟 tick，即 `1/90000` 秒，确保 DTS 严格递增。
5. 音频统一转换为 48 kHz 双声道 AAC；视频保持原始编码数据。
6. 标准化分片写入最终 MPEG-TS 文件，同时进入本地 HLS 播放缓冲区。

## 故障排查

### `/index.m3u8` 暂时没有媒体分片

首次启动时属于正常现象。等待预缓冲达到 10 个分片，并在日志中确认出现：

```text
playback ready: published=3 reserve=7 (...s)
```

### 广告期间出现 `discontinuity` 日志

这是上游播放列表的边界信息。只要后续仍出现 `timeline merged sequence=...`，下载和标准化流程
就在继续运行。

### 播放地址无法访问

检查 `.env` 中的 `ABEMA_PROXY_PORT`，并确认播放器使用与 Docker 端口映射一致的地址。默认地址为：

```text
http://127.0.0.1:18081/index.m3u8
```

### 需要查看下载器输出

使用 `--verbose-downloader`，或检查本次输出目录中的 `downloader.log`。

## 开发与测试

安装开发依赖后运行：

```powershell
python -m pytest -p no:cacheprovider -q
```

测试覆盖播放列表改写、下载器参数、PTS/DTS 回跳、MPEG-TS continuity counter、分片匀速发布，
以及 48 kHz 正片与 44.1 kHz 广告之间的音频标准化。

## 使用说明

- ABEMA 频道必须能够在当前网络环境中正常访问。
- 默认 Docker 端口只绑定至宿主机 `127.0.0.1`；如需提供给其他设备，应自行调整端口映射并
  配置相应的访问控制。
- 输出 MPEG-TS 文件在录制过程中持续增长。需要复制、转码或归档时，建议先正常停止录制。
- 请根据频道内容的授权范围及适用条款使用本项目。
