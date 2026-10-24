#!/bin/sh
# No credential is a command argument, environment value, or log message.
set -eu
umask 077
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

[ "$#" -eq 0 ] || fail "Start the gateway without command arguments."
[ "$(id -u)" -eq 0 ] || fail "The gateway must start as its container root user."

options=/data/options.json
private=/data/credentials
storage=/data/gluetun
snapshot=
temporary=

cleanup() {
    if [ -n "$snapshot" ]; then rm -f -- "$snapshot"; fi
    if [ -n "$temporary" ]; then rm -f -- "$temporary"; fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

[ ! -L /data ] && [ -d /data ] || fail "The gateway private data directory is unavailable."
[ ! -L "$options" ] && [ -f "$options" ] || fail "Save the gateway options before starting it."
[ "$(stat -c '%s' "$options")" -le 65536 ] || fail "Gateway options are too large."
[ ! -L "$private" ] && [ ! -L "$storage" ] || fail "Gateway private directory permissions need attention."
mkdir -p "$private" "$storage"
chown 0:0 "$private" "$storage" "$options"
chmod 700 "$private" "$storage"
chmod 600 "$options"

# Validate one immutable snapshot so a simultaneous options update cannot mix
# fields from different configurations. It is removed before Gluetun starts.
snapshot=$(mktemp "$private/.options.XXXXXX")
if ! jq -cse -f /opt/surfshark-gateway/validate-options.jq "$options" > "$snapshot" 2>/dev/null; then
    fail "Gateway options are incomplete or invalid; check the selected protocol and manual credentials."
fi

vpn_type=$(jq -r '.vpn_type' "$snapshot")
server_city=$(jq -r '.server_city' "$snapshot")
openvpn_protocol=$(jq -r '.openvpn_protocol' "$snapshot")

# Discover this container's existing bridge address before creating any VPN.
# A concrete IPv4 avoids Go's possible dual-stack wildcard listener; this is
# not an invented client/source ACL and does not change host networking.
bridge_interface=$(ip -4 route show default | awk '
    $1 == "default" { for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit } }')
[ -n "$bridge_interface" ] || fail "The gateway internal IPv4 interface is unavailable."
bridge_ip=$(ip -4 -o address show dev "$bridge_interface" scope global | awk '
    NR == 1 { split($4, address, "/"); print address[1] }')
case "$bridge_ip" in
    ""|*[!0-9.]*) fail "The gateway internal IPv4 address is unavailable." ;;
esac

write_secret() {
    field=$1
    destination=$2
    [ ! -L "$destination" ] || fail "Gateway credential file permissions need attention."
    if [ -e "$destination" ] && [ ! -f "$destination" ]; then
        fail "Gateway credential file permissions need attention."
    fi
    temporary=$(mktemp "$private/.write.XXXXXX")
    jq -j --arg field "$field" '.[$field]' "$snapshot" > "$temporary"
    chown 0:0 "$temporary"
    chmod 600 "$temporary"
    mv -f -- "$temporary" "$destination"
    temporary=
}

# Refresh every file, including inactive empty fields, so changing protocol
# does not retain a stale generated credential outside Supervisor's options.
write_secret openvpn_username "$private/openvpn-user"
write_secret openvpn_password "$private/openvpn-password"
write_secret wireguard_private_key "$private/wireguard-key"
write_secret wireguard_address "$private/wireguard-address"
rm -f -- "$snapshot"
snapshot=

# Use a complete environment allowlist. In particular, neither Supervisor
# token, inherited plaintext VPN credentials, proxy variables nor firewall
# overrides reach Gluetun. Only credential file paths are passed to the child.
exec env -i \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOME=/root TZ=UTC \
    VPN_SERVICE_PROVIDER=surfshark VPN_TYPE="$vpn_type" \
    SERVER_COUNTRIES=Canada SERVER_CITIES="$server_city" \
    OPENVPN_PROTOCOL="$openvpn_protocol" \
    OPENVPN_USER_SECRETFILE="$private/openvpn-user" \
    OPENVPN_PASSWORD_SECRETFILE="$private/openvpn-password" \
    WIREGUARD_PRIVATE_KEY_SECRETFILE="$private/wireguard-key" \
    WIREGUARD_ADDRESSES_SECRETFILE="$private/wireguard-address" \
    HTTPPROXY=on HTTPPROXY_LISTENING_ADDRESS="$bridge_ip:8888" \
    HTTPPROXY_LOG=off HTTPPROXY_STEALTH=on \
    HTTP_CONTROL_SERVER_ADDRESS=127.0.0.1:8000 HTTP_CONTROL_SERVER_LOG=off \
    HEALTH_SERVER_ADDRESS=127.0.0.1:9999 HEALTH_SMALL_CHECK_TYPE=dns \
    FIREWALL_ENABLED_DISABLING_IT_SHOOTS_YOU_IN_YOUR_FOOT=on \
    FIREWALL_INPUT_PORTS=8888 \
    DNS_SERVER=on DNS_KEEP_NAMESERVER=off \
    DNS_UPSTREAM_RESOLVER_TYPE=dot DNS_UPSTREAM_RESOLVERS=cloudflare \
    DNS_UPSTREAM_IPV6=off \
    BLOCK_ADS=off BLOCK_SURVEILLANCE=off BLOCK_MALICIOUS=off \
    STORAGE_FILEPATH="$storage/servers.json" UPDATER_PERIOD=0 \
    LOG_LEVEL=warn VERSION_INFORMATION=off \
    /gluetun-entrypoint
