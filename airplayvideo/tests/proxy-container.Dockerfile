# Browser-only fixture: no native engine, media dependencies, accounts, or TVs.
# Build from airplayvideo/ with --platform linux/amd64 (official Chrome package).
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl python3 python3-aiohttp python3-pycryptodome \
    pulseaudio xvfb x11vnc xauth x11-utils xdotool openbox python3-xlib \
    fonts-liberation fonts-noto-color-emoji \
 && curl -fL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /tmp/chrome.deb \
 && apt-get install -y --no-install-recommends /tmp/chrome.deb \
 && rm /tmp/chrome.deb && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home browser \
 && mkdir -p /tmp/.X11-unix /usr/share/google-chrome/extensions /opt/airplayvideo/installed \
 && chmod 1777 /tmp/.X11-unix \
 && chmod 755 /usr/share/google-chrome/extensions /opt/airplayvideo /opt/airplayvideo/installed
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pytest python3-pytest-asyncio python3-paho-mqtt python3-numpy nodejs \
 && rm -rf /var/lib/apt/lists/*
COPY service /opt/airplayvideo/service
COPY companion /opt/airplayvideo/companion
COPY web /opt/airplayvideo/web
COPY tests /opt/airplayvideo/tests
RUN chmod 755 /opt/airplayvideo/service/companion_host.py
WORKDIR /opt/airplayvideo
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/airplayvideo
CMD ["python3", "tests/browser-proxy.py"]
