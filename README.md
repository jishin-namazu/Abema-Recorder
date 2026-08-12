# ABEMA Recorder

从 `stagecrowd-recorder` 改造而来的 ABEMA 直播录制和本地 HLS 转发工具。

它不再使用 Stagecrowd 的 Widevine、CDM 或 Shaka Packager。新的数据链路是：

```text
ABEMA 页面
  -> Streamlink ABEMA 解析器
  -> 本地 HLS Proxy（保留广告，内部源保留 discontinuity）
  -> N_m3u8DL-RE（持续下载并解密分片）
  -> 分片级 FFmpeg 时间轴标准化（重写广告重置的 PTS/DTS）
  -> index.m3u8（统一 PID、连续时间轴的本地 HLS）
  -> 持续增长的 MPEG-TS 文件
```

录制模式下的 `index.m3u8` 可以给 OBS、VLC 或 PotPlayer 直接播放。它使用已经解密、
统一音视频 PID、48 kHz AAC 音频并重写成连续 PTS/DTS 的本地分片。播放列表先缓存
10 个分片，再按媒体时长匀速发布，用来吸收广告期间约 20～25 秒的切片停顿；因此首次
播放需要等待约 40～50 秒。`source.m3u8` 是下载器使用的内部原始 HLS；另有
`clean.m3u8` 可用于确实需要原始分片但必须屏蔽 discontinuity 标签的下游。

## 功能

- 解析普通 ABEMA 频道 URL，例如 `https://abema.tv/now-on-air/luckyfes`
- 转发 AES-128 密钥，不需要 `device.wvd`
- 不过滤 `/tsad/`，广告和正片都会进入代理及录制文件
- 录制模式的 `index.m3u8` 输出统一 PID、连续时间轴的本地 HLS
- 正片与广告音频统一为双声道 48 kHz AAC，避免 48/44.1 kHz 切换
- 预缓存 10 个分片并匀速发布，避免广告切片成批到达造成缓冲耗尽
- 内部 `source.m3u8` 保留 discontinuity，`clean.m3u8` 屏蔽 discontinuity
- 记录 `/tslive/ -> /tsad/ -> /tslive/` 切换日志
- N_m3u8DL-RE 只负责持续下载；每个完整分片由独立 FFmpeg 进程标准化后追加到 `.ts`
- OBS 可以直接添加 `http://127.0.0.1:18081/index.m3u8`

## Docker 快速开始

复制配置示例：

```powershell
Copy-Item .env.example .env
```

按需修改 `.env`：

```ini
ABEMA_URL=https://abema.tv/now-on-air/luckyfes
ABEMA_QUALITY=1080p
ABEMA_PROXY_PORT=18081
```

构建：

```powershell
docker compose build
```

先验证 ABEMA 解析、AES 密钥以及音视频解码：

```powershell
docker compose run --rm --service-ports recorder probe
```

开始录制：

```powershell
docker compose up
```

录制结果写入 `archive/abema_日期_时间.out/`。默认保留下载分片，并生成同目录下持续增长的 `.ts` 文件。

## 只给 OBS 播放，不录制

```powershell
docker compose run --rm --service-ports recorder proxy
```

然后在 OBS 添加“媒体源”：

1. 取消“本地文件”
2. 输入：`http://127.0.0.1:18081/index.m3u8`
3. 网络缓冲可从 `2000 ms` 开始；服务启动后先等待本地预缓存完成
4. 不要启用“文件结束后循环”

这个地址是真正的本地 HLS 播放列表，不是强行拼接的 progressive TS。广告和正片都在同一播放源里；每个分片在发布前都已统一 PID 并平移到连续时间轴，因此切换时可以继续播放。

如果下游明确不能接受该标签，可以改用 `http://127.0.0.1:18081/clean.m3u8`，但不建议把它用于需要可靠跨广告播放的播放器。

## 命令

### 查看将要执行的录制命令

```powershell
docker compose run --rm recorder plan
```

### 只录制 60 秒

```powershell
docker compose run --rm --service-ports recorder capture --record-limit 00:01:00
```

### 指定频道、清晰度和输出目录

```powershell
docker compose run --rm --service-ports recorder capture `
  --url https://abema.tv/now-on-air/luckyfes `
  --quality 1080p `
  --out /archive/luckyfes.out
```

### 查看详细日志

全局参数要放在子命令前：

```powershell
docker compose run --rm --service-ports recorder --verbose proxy
```

## 切换日志

代理会输出类似：

```text
initial mode=tslive sequence=30600
transition tslive -> tsad sequence=30611 discontinuity=True
discontinuity within tsad sequence=30614
transition tsad -> tslive sequence=30629 discontinuity=True
```

看到日志中的 `discontinuity=True` 是正常的。它表示内部原始源检测到了广告或编码时间线边界；录制模式的 `index.m3u8` 会发布已经标准化的连续分片。
录制使用 `--skip-merge` 将下载与合并解耦。每个分片会被重映射成统一的音视频 PID，并把广告重置的 PTS/DTS 平移到连续时间轴后再追加，因此广告切换不会让播放器卡在旧时间戳。

## 注意

- ABEMA URL 必须能在当前网络环境正常访问。
- 端口默认只发布到主机的 `127.0.0.1`，不会暴露给局域网。
- `Ctrl+C` 会停止录制；容器使用 init 进程把停止信号传给下载器。
- 如果只想要最终 TS、不保留分片，可给 `capture` 增加 `--discard-segments`。
