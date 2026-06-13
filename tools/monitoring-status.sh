#!/bin/sh
# kg-hub 监控全景(汇总逻辑在 VPS:/root/uptime/status.sh)
exec ssh -o BatchMode=yes -o ConnectTimeout=12 root@oc-vps-aliyun-us /root/uptime/status.sh
