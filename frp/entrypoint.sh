#!/bin/sh

usage() { echo "Usage: $0 -s|-c " 1>&2; exit 1; }

case $1 in
    -s)
        ./frps -c /config/frps.ini
        ;;
    -c)
        ./frpc -c /config/frpc.ini
        ;;
    *)
        usage
        ;;
esac

