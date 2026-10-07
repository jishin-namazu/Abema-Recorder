FROM python:3.13-slim AS binaries

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tar \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /stage

ARG DOWNLOADER_VERSION=v0.6.0-beta
ARG DOWNLOADER_ASSET=N_m3u8DL-RE_v0.6.0-beta_linux-x64_20260629.tar.gz
ARG SHAKA_VERSION=v3.9.3

RUN curl -fsSL --retry 3 \
      "https://github.com/nilaoda/N_m3u8DL-RE/releases/download/${DOWNLOADER_VERSION}/${DOWNLOADER_ASSET}" \
      -o downloader.tar.gz \
 && tar -xzf downloader.tar.gz \
 && find . -name 'N_m3u8DL-RE' -type f -exec mv {} ./N_m3u8DL-RE \; \
 && chmod +x ./N_m3u8DL-RE \
 && rm -rf downloader.tar.gz

RUN curl -fsSL --retry 3 \
      "https://github.com/shaka-project/shaka-packager/releases/download/${SHAKA_VERSION}/packager-linux-x64" \
      -o shaka-packager \
 && chmod +x ./shaka-packager

RUN ./N_m3u8DL-RE --version || true
RUN ./shaka-packager --version


FROM python:3.13-slim

LABEL org.opencontainers.image.title="abema_recorder"
LABEL org.opencontainers.image.description="Container-first archiver for Widevine-protected DASH streams from ABEMA"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=binaries /stage/N_m3u8DL-RE   /usr/local/bin/N_m3u8DL-RE
COPY --from=binaries /stage/shaka-packager /usr/local/bin/shaka-packager

WORKDIR /opt/abema_recorder
COPY pyproject.toml README.md ./
COPY abema_recorder ./abema_recorder
RUN pip install --no-cache-dir ".[cdm]"

RUN mkdir -p /archive
WORKDIR /archive

ENV PYTHONUNBUFFERED=1 \
    ABM_CDM=/config/device.wvd \
    ABM_SETTINGS=/config/.env

ENTRYPOINT ["python", "-m", "abema_recorder"]
CMD ["probe"]
