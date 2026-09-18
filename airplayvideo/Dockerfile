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
RUN curl -fL https://ffmpeg.org/releases/ffmpeg-8.0.1.tar.xz -o ffmpeg.tar.xz \
 && echo '05ee0b03119b45c0bdb4df654b96802e909e0a752f72e4fe3794f487229e5a41  ffmpeg.tar.xz' | sha256sum -c - \
 && tar -xf ffmpeg.tar.xz && cd ffmpeg-8.0.1 \
 && ./configure --prefix=/usr/local --enable-shared --disable-static \
    --disable-gpl --disable-nonfree --disable-autodetect --disable-everything \
    --disable-doc --disable-debug --enable-ffmpeg --enable-ffprobe \
    --enable-libopenh264 --enable-libpulse --enable-libxcb --enable-vaapi \
    --enable-protocol=file,pipe,http,tcp --enable-indev=xcbgrab,pulse,lavfi \
    --enable-demuxer=mpegts,mov,matroska,h264,wav \
    --enable-decoder=wrapped_avframe,rawvideo,h264,mpeg2video,aac,ac3,mp2,mp3,pcm_s16le,pcm_s16be,pcm_f32le \
    --enable-encoder=libopenh264,h264_vaapi,aac,pcm_s16le,pcm_s16be,png \
    --enable-muxer=mpegts,mp4,matroska,h264,wav,image2,rawvideo,null \
    --enable-parser=h264,mpegvideo,aac,ac3,mpegaudio \
    --enable-bsf=h264_mp4toannexb,aac_adtstoasc \
    --enable-filter=testsrc2,sine,aresample,format,scale,null,anull,bwdif,buffer,buffersink,abuffer,abuffersink,setpts,asetpts \
 && make -j4 && make install && ldconfig \
 && ffmpeg -buildconf > /opt/sources/ffmpeg-buildconf.txt 2>&1
WORKDIR /opt/app
FROM dependencies AS build
COPY CMakeLists.txt ./
COPY include ./include
COPY src ./src
COPY vendor ./vendor
COPY tests ./tests
RUN cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
 && cmake --build build -j4 && ctest --test-dir build --output-on-failure

FROM debian:bookworm-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl python3 python3-aiohttp python3-paho-mqtt \
    python3-pycryptodome python3-numpy python3-pytest python3-pytest-asyncio \
    libssl3 libsodium23 libplist3 libcpp-httplib0.11 libpulse0 \
    libxcb1 libxcb-shm0 libxcb-xfixes0 libva2 libva-drm2 libva-x11-2 \
    libdrm2 pulseaudio xvfb x11vnc xauth x11-utils xdotool openbox \
    novnc fonts-liberation fonts-noto-color-emoji libgl1-mesa-dri intel-media-va-driver \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /tmp/chrome.deb \
 && apt-get update && apt-get install -y /tmp/chrome.deb \
 && rm /tmp/chrome.deb && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home browser \
 && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
COPY --from=build /usr/local/lib/ /usr/local/lib/
COPY --from=build /usr/local/bin/ffmpeg /usr/local/bin/ffprobe /usr/local/bin/
COPY --from=build /opt/app/build/airplayvideo-engine /usr/local/bin/
COPY --from=build /opt/sources/ffmpeg.tar.xz /opt/sources/openh264.tar.gz /opt/sources/ffmpeg-buildconf.txt /opt/dependency-sources/
COPY vendor/pair_ap/LICENSE /opt/licenses/pair_ap-MIT.txt
COPY LICENSE /opt/licenses/AirplayVideo-MIT.txt
COPY --from=build /opt/sources/ffmpeg-8.0.1/COPYING.LGPLv2.1 /opt/licenses/FFmpeg-LGPL-2.1.txt
COPY --from=build /opt/sources/openh264-2.6.0/LICENSE /opt/licenses/OpenH264-BSD.txt
COPY service /opt/airplayvideo/service
COPY web /opt/airplayvideo/web
COPY tests /opt/airplayvideo/tests
RUN ldconfig && dpkg-query -W > /opt/dependency-sources/debian-packages.txt
WORKDIR /opt/airplayvideo
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8099 18200-18215/udp
LABEL io.hass.name="AirplayVideo" io.hass.description="Browser and HDHomeRun video over AirPlay" io.hass.type="addon" io.hass.arch="amd64" io.hass.version="0.1.2"
CMD ["python3", "-m", "service.main"]
