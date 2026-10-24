# Never include rejected input in diagnostics. The entrypoint redirects jq's
# output into a private snapshot and returns one fixed error on rejection.
def plain($maximum):
  type == "string" and length <= $maximum and
  (test("[\u0000-\u001f\u007f]") | not);

def ipv4_prefix:
  if test("^([0-9]{1,3}\\.){3}[0-9]{1,3}/[0-9]{1,2}$") then
    split("/") as $parts |
    ($parts[0] | split(".") | map(tonumber)) as $octets |
    all($octets[]; . >= 0 and . <= 255) and
    ($parts[1] | tonumber) >= 0 and ($parts[1] | tonumber) <= 32
  else false end;

if length == 1 then .[0] else empty end |
select(type == "object") |
select((keys - ["vpn_type", "server_city", "openvpn_protocol",
                "openvpn_username", "openvpn_password",
                "wireguard_private_key", "wireguard_address"]) | length == 0) |
select(.vpn_type == "openvpn" or .vpn_type == "wireguard") |
select(.openvpn_protocol == "udp" or .openvpn_protocol == "tcp") |
select(.server_city | plain(60) and test("^[A-Za-z][A-Za-z -]{0,59}$")) |
select(.openvpn_username | plain(512)) |
select(.openvpn_password | plain(4096)) |
select(.wireguard_private_key | plain(100)) |
select(.wireguard_address | plain(64)) |
select(if .vpn_type == "openvpn" then
         (.openvpn_username | length > 0) and
         (.openvpn_password | length > 0)
       else
         (.wireguard_private_key | test("^[A-Za-z0-9+/]{43}=$")) and
         (.wireguard_address | ipv4_prefix)
       end)
