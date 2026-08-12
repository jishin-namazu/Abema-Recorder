# Fetch the pinned live downloader in a separate stage, then copy only the
# verified binary into the runtime image.
FROM python:3.13-slim AS binaries

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tar \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /stage

ARG DOWNLOADER_VERSION=v0.6.0-beta
ARG DOWNLOADER_ASSET=N_m3u8DL-RE_v0.6.0-beta_linux-x64_20260629.tar.gz

RUN curl -fsSL --retry 3 \
      "https://github.com/nilaoda/N_m3u8DL-RE/releases/download/${DOWNLOADER_VERSION}/${DOWNLOADER_ASSET}" \
      -o downloader.tar.gz \
 && tar -xzf downloader.tar.gz \
 && find . -name 'N_m3u8DL-RE' -type f -exec mv {} ./N_m3u8DL-RE \; \
 && chmod +x ./N_m3u8DL-RE \
 && rm downloader.tar.gz

# This release prints its version but may return a non-zero status.
RUN ./N_m3u8DL-RE --version || test -x ./N_m3u8DL-RE


FROM python:3.13-slim

LABEL org.opencontainers.image.title="abema-recorder"
LABEL org.opencontainers.image.description="ABEMA HLS proxy and discontinuity-aware live recorder"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=binaries /stage/N_m3u8DL-RE /usr/local/bin/N_m3u8DL-RE

WORKDIR /opt/abema-recorder
COPY pyproject.toml README.md ./
COPY abema_recorder ./abema_recorder
RUN pip install --no-cache-dir .

RUN mkdir -p /archive
WORKDIR /archive

ENV PYTHONUNBUFFERED=1 \
    ABEMA_URL=https://abema.tv/now-on-air/luckyfes \
    ABEMA_QUALITY=1080p \
    ABEMA_PROXY_HOST=0.0.0.0 \
    ABEMA_PROXY_PORT=18081

EXPOSE 18081

ENTRYPOINT ["python", "-m", "abema_recorder"]
CMD ["capture"]
