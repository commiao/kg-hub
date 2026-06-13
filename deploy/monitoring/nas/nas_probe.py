#!/usr/bin/env python3
# NAS->VPS 反向探针:监控 VPS 上的服务(openclaw),补"VPS 整体挂"盲区。
# 边沿触发(挂了报一次、恢复报一次),状态存 /probe/state。读 /probe/targets.conf。
# 纯标准库;跑在 NAS 已有的 kg-hub-server 镜像里(无需拉新镜像)。
import os, json, time, urllib.request, urllib.error

BASE = "/probe"; STATE = os.path.join(BASE, "state"); CONF = os.path.join(BASE, "targets.conf")
os.makedirs(STATE, exist_ok=True)


def feishu(url, text):
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"msg_type": "text", "content": {"text": text}}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def http_code(url):
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


now = time.strftime("%Y-%m-%d %H:%M:%S")
try:
    lines = open(CONF).read().splitlines()
except Exception:
    lines = []
for line in lines:
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    p = line.split("|")
    if len(p) < 3:
        continue
    name, url, webhook = p[0], p[1], p[2]
    if not webhook:
        wf = os.path.join(BASE, "webhook.conf")
        webhook = open(wf).read().strip() if os.path.exists(wf) else ""
    if not webhook:
        continue
    thr = int(p[3]) if len(p) > 3 and p[3].strip() else 3
    sf = os.path.join(STATE, name + ".fails"); ss = os.path.join(STATE, name + ".status")
    fails = int((open(sf).read() or "0")) if os.path.exists(sf) else 0
    status = open(ss).read().strip() if os.path.exists(ss) else "UP"
    code = http_code(url)
    if code == 200:
        if status == "DOWN":
            feishu(webhook, f"✅ [{name}] 恢复 (HTTP 200) @ {now}")
        open(sf, "w").write("0"); open(ss, "w").write("UP")
    else:
        fails += 1; open(sf, "w").write(str(fails))
        if fails >= thr and status != "DOWN":
            feishu(webhook, f"🔴 [{name}] 不可达 (code={code}, 连续失败{fails}次) @ {now}")
            open(ss, "w").write("DOWN")
