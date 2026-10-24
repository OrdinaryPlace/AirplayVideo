#!/bin/sh
# Synthetic-credential startup probe, mounted over Gluetun only by the tests.
# Never prints environment contents or credential values.
set -eu
touch /data/probe-started

for key in SUPERVISOR_TOKEN HASSIO_TOKEN OPENVPN_USER OPENVPN_PASSWORD \
           WIREGUARD_PRIVATE_KEY HTTP_PROXY HTTPS_PROXY ALL_PROXY \
           FIREWALL_OUTBOUND_SUBNETS; do
    if printenv "$key" >/dev/null; then
        printf '%s\n' "Unexpected inherited environment reached the child." >&2
        exit 1
    fi
done

[ "$VPN_SERVICE_PROVIDER" = surfshark ]
[ "$SERVER_COUNTRIES" = Canada ]
[ "$HTTPPROXY_LISTENING_ADDRESS" = 192.0.2.2:8888 ]
[ "$HTTP_CONTROL_SERVER_ADDRESS" = 127.0.0.1:8000 ]
[ "$HEALTH_SERVER_ADDRESS" = 127.0.0.1:9999 ]
[ "$HEALTH_SMALL_CHECK_TYPE" = dns ]
[ "$PPROF_ENABLED" = off ]
[ "$PPROF_BLOCK_PROFILE_RATE" = 0 ]
[ "$PPROF_MUTEX_PROFILE_RATE" = 0 ]
[ "$PPROF_HTTP_SERVER_ADDRESS" = 127.0.0.1:6060 ]
[ "$HTTPPROXY_LOG" = off ]
[ "$FIREWALL_ENABLED_DISABLING_IT_SHOOTS_YOU_IN_YOUR_FOOT" = on ]
[ "$(stat -c '%u:%g:%a' /data/credentials)" = 0:0:700 ]
[ "$(stat -c '%u:%g:%a' /data/options.json)" = 0:0:600 ]

check_secret() {
    [ "$(stat -c '%u:%g:%a' "$2")" = 0:0:600 ]
    jq -j --arg field "$1" '.[$field]' /data/options.json > /tmp/probe-expected
    cmp -s /tmp/probe-expected "$2"
}
check_secret openvpn_username "$OPENVPN_USER_SECRETFILE"
check_secret openvpn_password "$OPENVPN_PASSWORD_SECRETFILE"
check_secret wireguard_private_key "$WIREGUARD_PRIVATE_KEY_SECRETFILE"
check_secret wireguard_address "$WIREGUARD_ADDRESSES_SECRETFILE"

# Startup must not leave the temporary JSON copy or partial credential files.
[ "$(find /data/credentials -type f | wc -l)" -eq 4 ]
printf '{"protocol":"%s","city":"%s","private_files":true,"clean_environment":true}\n' \
    "$VPN_TYPE" "$SERVER_CITIES"
