#!/bin/sh
# Synthetic route/address facts for credential tests that have --network none.
# This file is never copied into the gateway image.
case "$*" in
    "-4 route show default")
        printf '%s\n' "default via 192.0.2.1 dev eth0"
        ;;
    "-4 -o address show dev eth0 scope global")
        printf '%s\n' "2: eth0 inet 192.0.2.2/24 scope global eth0"
        ;;
    *) exit 1 ;;
esac
