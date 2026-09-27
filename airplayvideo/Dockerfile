FROM debian:bookworm-slim AS youtube-dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl xz-utils python3 python3-pip libstdc++6 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/sources
COPY requirements-youtube.txt ./
RUN python3 -m pip download --no-deps --only-binary=:all: --require-hashes \
      -r requirements-youtube.txt -d /opt/python-wheels \
 && curl -fL https://files.pythonhosted.org/packages/1e/e0/832fa4ca334b766a06933a196066edc3dba37cdb6f14cd98d59bcc69a4b4/yt_dlp-2026.8.19.tar.gz -o yt_dlp-2026.8.19.tar.gz \
 && echo '9e213e48cea35c66b378e4447903f118f6392a5fa380a2b6d7070ec86f4e0af1  yt_dlp-2026.8.19.tar.gz' | sha256sum -c - \
 && curl -fL https://files.pythonhosted.org/packages/d3/e6/cceb9530e8f4e5940f6f7822d90e9d94f1b85343329a16baaf47bbbb3de1/yt_dlp_ejs-0.8.0.tar.gz -o yt_dlp_ejs-0.8.0.tar.gz \
 && echo 'd5fa1639f63b5c4af8d932495f60689d5370f1a095782c944f7f62a303eb104e  yt_dlp_ejs-0.8.0.tar.gz' | sha256sum -c -
# The app image is amd64 (including Chrome). Keep Node's complete source and
# official binary archive, but install only the JS runtime, not npm or npx.
RUN curl -fL https://nodejs.org/dist/v22.23.3/node-v22.23.3-linux-x64.tar.xz -o node-linux-x64.tar.xz \
 && echo 'df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de  node-linux-x64.tar.xz' | sha256sum -c - \
 && curl -fL https://nodejs.org/dist/v22.23.3/node-v22.23.3.tar.xz -o node-source.tar.xz \
 && echo 'bd97093e1a1e9243338950c174a693a64d4e0926a9c6ce259962bc58d5e96909  node-source.tar.xz' | sha256sum -c - \
 && mkdir -p /opt/node \
 && tar -xJf node-linux-x64.tar.xz --strip-components=1 -C /opt/node \
      node-v22.23.3-linux-x64/bin/node node-v22.23.3-linux-x64/LICENSE \
 && /opt/node/bin/node --version > node-version.txt \
 && sha256sum *.tar.* > youtube-source-sha256.txt

FROM debian:bookworm-slim AS dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake ninja-build pkg-config nasm curl ca-certificates \
    libssl-dev libsodium-dev libplist-dev nlohmann-json3-dev libcpp-httplib-dev \
    libpulse-dev libxcb1-dev libxcb-shm0-dev libxcb-xfixes0-dev libva-dev \
    libdrm-dev zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/sources
RUN curl -fL https://github.com/cisco/openh264/archive/refs/tags/v2.6.0.tar.gz -o openh264.tar.gz \
 && echo '558544ad358283a7ab2930d69a9ceddf913f4a51ee9bf1bfb9e377322af81a69  openh264.tar.gz' | sha256sum -c - \
 && tar -xf openh264.tar.gz && cd openh264-2.6.0 \
 && make -j4 && make install-shared PREFIX=/usr/local && ldconfig
RUN apt-get update && apt-get install -y --no-install-recommends libgnutls28-dev libdav1d-dev \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fL https://ffmpeg.org/releases/ffmpeg-8.0.1.tar.xz -o ffmpeg.tar.xz \
 && echo '05ee0b03119b45c0bdb4df654b96802e909e0a752f72e4fe3794f487229e5a41  ffmpeg.tar.xz' | sha256sum -c - \
 && tar -xf ffmpeg.tar.xz && cd ffmpeg-8.0.1 \
 && ./configure --prefix=/usr/local --enable-shared --disable-static \
    --disable-gpl --disable-nonfree --disable-autodetect --disable-everything \
    --disable-doc --disable-debug --enable-ffmpeg --enable-ffprobe \
    --enable-libopenh264 --enable-libpulse --enable-libxcb --enable-libxcb-shm --enable-vaapi \
    --enable-gnutls --enable-libdav1d \
    --enable-protocol=file,pipe,http,https,tcp,tls,crypto --enable-indev=xcbgrab,pulse,lavfi \
    --enable-demuxer=mpegts,mov,matroska,h264,hevc,hls,wav \
    --enable-decoder=wrapped_avframe,rawvideo,h264,hevc,vp9,libdav1d,mpeg2video,aac,opus,ac3,mp2,mp3,alac,pcm_s16le,pcm_s16be,pcm_f32le \
    --enable-encoder=libopenh264,h264_vaapi,hevc_vaapi,aac,pcm_s16le,pcm_s16be,png \
    --enable-muxer=mpegts,mp4,hls,matroska,h264,wav,image2,rawvideo,null \
    --enable-parser=h264,hevc,vp9,av1,opus,mpegvideo,aac,ac3,mpegaudio \
    --enable-bsf=h264_mp4toannexb,aac_adtstoasc \
    --enable-filter=testsrc2,color,sine,aresample,format,scale,hwupload,null,anull,bwdif,buffer,buffersink,abuffer,abuffersink,setpts,asetpts \
 && make -j4 && make install && ldconfig \
 && ffmpeg -buildconf > /opt/sources/ffmpeg-buildconf.txt 2>&1
WORKDIR /opt/app
RUN apt-get update && apt-get install -y --no-install-recommends libpango1.0-dev fonts-noto-core tzdata && rm -rf /var/lib/apt/lists/*
FROM dependencies AS build
COPY CMakeLists.txt ./
COPY include ./include
COPY src ./src
COPY vendor ./vendor
COPY tests ./tests
RUN cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
 && cmake --build build -j4 && ctest --test-dir build --output-on-failure --verbose

FROM debian:bookworm-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl python3 python3-venv python3-aiohttp python3-paho-mqtt \
    python3-pycryptodome python3-numpy python3-pytest python3-pytest-asyncio \
    libssl3 libgnutls30 libdav1d6 libsodium23 libplist3 libcpp-httplib0.11 libpulse0 \
    libxcb1 libxcb-shm0 libxcb-xfixes0 libva2 libva-drm2 libva-x11-2 \
    libdrm2 pulseaudio xvfb x11vnc xauth x11-utils xdotool openbox \
    novnc fonts-liberation fonts-noto-color-emoji libgl1-mesa-dri libegl-mesa0 \
    mesa-vulkan-drivers intel-media-va-driver vainfo \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /tmp/chrome.deb \
 && apt-get update && apt-get install -y /tmp/chrome.deb \
 && rm /tmp/chrome.deb && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home browser \
 && mkdir -p /tmp/.X11-unix /usr/share/google-chrome/extensions /opt/airplayvideo/installed \
 && chmod 1777 /tmp/.X11-unix \
 && chmod 755 /usr/share/google-chrome/extensions /opt/airplayvideo /opt/airplayvideo/installed
RUN apt-get update && apt-get install -y --no-install-recommends python3-xlib && rm -rf /var/lib/apt/lists/*
RUN apt-get update && apt-get install -y --no-install-recommends libpangocairo-1.0-0 fonts-noto-core tzdata && rm -rf /var/lib/apt/lists/*
COPY --from=build /usr/local/lib/ /usr/local/lib/
COPY --from=build /usr/local/bin/ffmpeg /usr/local/bin/ffprobe /usr/local/bin/
COPY --from=youtube-dependencies /opt/node/bin/node /usr/local/bin/node
COPY --from=build /opt/app/build/airplayvideo-engine /usr/local/bin/
COPY --from=build /opt/app/build/measure-sync /usr/local/bin/airplayvideo-measure-sync
COPY --from=build /opt/app/build/sync-content-tests /usr/local/bin/airplayvideo-sync-probe
COPY --from=build /opt/sources/ffmpeg.tar.xz /opt/sources/openh264.tar.gz /opt/sources/ffmpeg-buildconf.txt /opt/dependency-sources/
COPY --from=youtube-dependencies /opt/sources/ /opt/dependency-sources/youtube/
COPY --from=youtube-dependencies /opt/python-wheels/ /opt/dependency-sources/python-wheels/
COPY --from=youtube-dependencies /opt/node/LICENSE /opt/licenses/Node-LICENSE.txt
RUN python3 -m venv --system-site-packages /opt/airplayvideo/venv \
 && /opt/airplayvideo/venv/bin/python -m pip install --no-index --no-deps --require-hashes \
      --find-links=/opt/dependency-sources/python-wheels \
      -r /opt/dependency-sources/youtube/requirements-youtube.txt \
 && /opt/airplayvideo/venv/bin/python -m pip list --format=json > /opt/dependency-sources/python-packages.json
COPY vendor/pair_ap/LICENSE /opt/licenses/pair_ap-MIT.txt
COPY LICENSE /opt/licenses/AirplayVideo-MIT.txt
COPY --from=build /opt/sources/ffmpeg-8.0.1/COPYING.LGPLv2.1 /opt/licenses/FFmpeg-LGPL-2.1.txt
COPY --from=build /opt/sources/openh264-2.6.0/LICENSE /opt/licenses/OpenH264-BSD.txt
COPY service /opt/airplayvideo/service
COPY companion /opt/airplayvideo/companion
RUN chmod 755 /opt/airplayvideo/service/companion_host.py
COPY web /opt/airplayvideo/web
COPY tests /opt/airplayvideo/tests
RUN ldconfig && dpkg-query -W > /opt/dependency-sources/debian-packages.txt
WORKDIR /opt/airplayvideo
ENV PATH="/opt/airplayvideo/venv/bin:${PATH}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN python3 tests/native-dependencies.py --write-report /opt/dependency-sources/native-capabilities.json
EXPOSE 8099 18200-18215/udp
LABEL io.hass.name="AirplayVideo" io.hass.description="Browser, live TV and generated video over AirPlay" io.hass.type="addon" io.hass.arch="amd64" io.hass.version="0.2.15"
CMD ["python3", "-m", "service.main"]
