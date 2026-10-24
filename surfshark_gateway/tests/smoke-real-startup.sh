#!/bin/sh
# Real Gluetun constructor smoke; Docker supplies no external network link.
# Credentials are synthetic. Never print child logs, settings, or responses.
set -eu
gateway_pid=
cleanup() {
    if [ -n "$gateway_pid" ]; then
        kill -TERM "$gateway_pid" 2>/dev/null || true
        wait "$gateway_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

active_interfaces=$(ip -o link show up | awk '{print $2}')
[ "$active_interfaces" = lo: ] || {
    printf '%s\n' 'The real startup smoke requires only loopback networking.' >&2
    exit 1
}
# The program requires Ethernet, a local subnet, and a default route before
# its constructors. A dummy interface has no peer or external connection.
ip link add gateway-smoke type dummy >/dev/null 2>&1
ip -4 address add 192.0.2.2/24 dev gateway-smoke >/dev/null 2>&1
ip link set gateway-smoke up >/dev/null 2>&1
ip -4 route add default via 192.0.2.1 dev gateway-smoke >/dev/null 2>&1
/opt/surfshark-gateway/run.sh >/tmp/gateway-real-startup.log 2>&1 &
gateway_pid=$!

health_ready=false
attempt=0
while [ "$attempt" -lt 50 ]; do
    if wget -T 1 -S -O /dev/null http://127.0.0.1:9999 \
        >/dev/null 2>/tmp/gateway-real-health.log; then
        health_ready=true
    elif grep -Eq 'HTTP/[0-9.]+ [0-9]{3}' /tmp/gateway-real-health.log; then
        # A down-VPN HTTP response still proves the real constructors ran.
        health_ready=true
    fi
    if [ "$health_ready" = true ]; then break; fi
    if ! kill -0 "$gateway_pid" 2>/dev/null; then break; fi
    attempt=$((attempt + 1))
    sleep 0.1
done

if grep -Eq 'panic:|fatal error:|reading [^ ]+ settings:|creating Pprof server:' \
    /tmp/gateway-real-startup.log; then
    printf '%s\n' 'Real Gluetun initialization failed its panic/configuration check.' >&2
    exit 1
fi
[ "$health_ready" = true ] || {
    printf '%s\n' 'Real Gluetun did not reach its local health listener.' >&2
    exit 1
}
printf '%s\n' '{"real_initialization":true,"external_network":false,"panic_or_config_error":false}'
