#!/usr/bin/env python3
"""Individual deployment helper behind up.sh / down.sh / deploy.sh / db-up.sh.

  up       create 1 SG + 1 imported key pair + 1 t3.micro from a commit (W3 scope)
  down     terminate by the IDs in .local/resources.json and read back; --stop only stops
  start    start the kept (stopped) host, report old/new public IP and /health (W4 T1)
  status   read-only: host state, public IP, SG rules, current egress /32
  deploy   install a committed version on the SAME host over SSH, then place app.env (+ W5 db.env)
  sources  add exact /32 SG sources after the egress address changed (prints old/new first)
  db-up    W5: 2 private subnets + local-only route table + DB subnet group + SG-db + private RDS
  db-status  read back the RDS and its network (read-only)

AWS calls go through scripts/lab.py run_aws (learnerlab profile, cleaned env).
Every change prints its exact scope and waits for a typed "yes".
Never prints AWS credentials, private keys or the app tokens.
"""
import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "deploy"))
import lab  # noqa: E402
import make_user_data  # noqa: E402

LOCAL = ROOT / ".local"
RESOURCES = LOCAL / "resources.json"
CONFIG = LOCAL / "config.json"
KNOWN_HOSTS = LOCAL / "known_hosts"
APP_ENV = LOCAL / "app.env"
DB_ENV = LOCAL / "db.env"
INSTANCE_TYPE = "t3.micro"
AMI_PARAM = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
LABEL_RE = re.compile(r"[a-z0-9]{1,16}")
REGION = None


class Stop(Exception):
    pass


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def aws(*args):
    return lab.run_aws(list(args), REGION)


def aws_code(*args):
    """Run a read; return (result, None) or (None, AWS error code)."""
    try:
        return aws(*args), None
    except lab.LabError as exc:
        match = re.search(r": ([A-Za-z0-9._-]+)\. Stop;", str(exc))
        return None, match.group(1) if match else str(exc)


def confirm(lines):
    print("\n".join(lines))
    try:
        answer = input('確認以上範圍請輸入 yes：').strip()
    except EOFError:
        answer = ""
    if answer != "yes":
        raise Stop("已取消，沒有做任何變更。")


def save_json(path, data):
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    lab.atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def egress_ip():
    with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=8) as response:
        return response.read().decode().strip()


def load_config(args):
    cfg = load_json(CONFIG) or {}
    for key in ("group", "owner", "source_cidr", "week", "az"):
        if getattr(args, key, None):
            cfg[key] = getattr(args, key)
    cfg.setdefault("week", "w04")
    cfg.setdefault("az", "us-east-1a")
    for key in ("group", "owner"):
        if not LABEL_RE.fullmatch(cfg.get(key, "")):
            raise Stop(f"{key} 需為 1–16 個小寫英數（組名／組內代號，不用學號姓名）；用 --{key} 或 .local/config.json 提供。")
    try:
        net = ipaddress.ip_network(cfg.get("source_cidr", ""), strict=True)
    except ValueError:
        raise Stop("source_cidr 需為一個 IPv4 /32，例如 203.0.113.5/32。")
    if net.version != 4 or net.prefixlen != 32 or not net.is_global:
        raise Stop("source_cidr 只能是單一公開 IPv4 /32；不得放寬來源。")
    cfg.setdefault("ssh_key", str(Path.home() / ".ssh" / f"inspection-{cfg['group']}-{cfg['owner']}"))
    return cfg


def tags(cfg, name):
    return [{"Key": "course", "Value": lab.COURSE}, {"Key": "week", "Value": cfg["week"]},
            {"Key": "group", "Value": cfg["group"]}, {"Key": "owner", "Value": cfg["owner"]},
            {"Key": "Name", "Value": name}]


def tag_spec(kinds, cfg, name):
    return json.dumps([{"ResourceType": kind, "Tags": tags(cfg, name)} for kind in kinds])


def owned(tag_list, cfg):
    found = {t["Key"]: t["Value"] for t in tag_list or []}
    return (found.get("course") == lab.COURSE and found.get("group") == cfg["group"]
            and found.get("owner") == cfg["owner"])


def curl(url, timeout=8):
    """Returns (curl exit code, HTTP status, body, seconds). HTTP 000 = no HTTP response."""
    began = time.monotonic()
    result = subprocess.run(["curl", "-sS", "--max-time", str(timeout), "-o", "-", "-w", "\n%{http_code}", url],
                            capture_output=True, text=True, check=False)
    body, _, code = result.stdout.rpartition("\n")
    error = result.stderr.strip().splitlines()[-1] if result.returncode and result.stderr.strip() else ""
    return result.returncode, code or "000", body if not error else error, round(time.monotonic() - began, 1)


def wait_health(ip, sha, need_auth=None, limit=480):
    began = time.monotonic()
    while time.monotonic() - began < limit:
        exit_code, code, body, _ = curl(f"http://{ip}/health", timeout=5)
        if code == "200":
            try:
                health = json.loads(body)
            except ValueError:
                health = {}
            if health.get("version") == sha and (need_auth is None or health.get("auth_configured") is need_auth):
                return health, round(time.monotonic() - began, 1)
        time.sleep(3)
    raise Stop(f"/health 在 {limit} 秒內沒有回 200 且 version={sha[:7]}；最後一次：exit={exit_code} HTTP {code}")


def describe_instance(instance_id):
    data, code = aws_code("ec2", "describe-instances", "--instance-ids", instance_id)
    if code:
        return None
    return data["Reservations"][0]["Instances"][0]


def wait_state(instance_id, target, limit=600):
    began = time.monotonic()
    while time.monotonic() - began < limit:
        inst = describe_instance(instance_id)
        if inst and inst["State"]["Name"] == target:
            return inst
        time.sleep(3)
    raise Stop(f"{instance_id} 在 {limit} 秒內沒有變成 {target}。")


def resolve_commit(commit):
    try:
        return subprocess.check_output(["git", "rev-parse", "--verify", "--end-of-options", commit + "^{commit}"],
                                       cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        raise Stop(f"找不到 commit：{commit}")


def build_user_data(commit):
    sha, data = make_user_data.build(commit)
    path = LOCAL / f"user-data-{sha[:12]}.sh"
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)
    return sha, path, len(data)


def current(require_instance=True):
    res = load_json(RESOURCES)
    if not res or (require_instance and "instance" not in res):
        raise Stop("沒有 .local/resources.json 的主機紀錄；主機不存在就先用 up.sh 建立。")
    return res


def ssh_base(cfg, ip):
    return ["ssh", "-i", cfg["ssh_key"], "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={KNOWN_HOSTS}", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10", "-o", "IdentitiesOnly=yes", f"ec2-user@{ip}"]


# ---------------------------------------------------------------- commands
def cmd_up(args):
    cfg = load_config(args)
    ctx = lab.verify()
    old = load_json(RESOURCES)
    if old and old.get("instance"):
        inst = describe_instance(old["instance"]["id"])
        if inst and inst["State"]["Name"] != "terminated":
            raise Stop(f"已有主機 {old['instance']['id']}（{inst['State']['Name']}）；同一時間只能一台。平常更新用 deploy.sh。")
    sha, user_data, size = build_user_data(args.commit)
    ami_id = aws("ssm", "get-parameter", "--name", AMI_PARAM)["Parameter"]["Value"]
    image = aws("ec2", "describe-images", "--image-ids", ami_id)["Images"][0]
    if image["Architecture"] != "x86_64":
        raise Stop("AMI 不是 x86_64。")
    vpc = aws("ec2", "describe-vpcs", "--filters", "Name=is-default,Values=true")["Vpcs"]
    if not vpc:
        raise Stop("沒有預設 VPC；停下來求助，不要自己新建。")
    vpc_id = vpc[0]["VpcId"]
    subnets = aws("ec2", "describe-subnets", "--filters", f"Name=vpc-id,Values={vpc_id}",
                  "Name=default-for-az,Values=true", f"Name=availability-zone,Values={cfg['az']}")["Subnets"]
    if not subnets:
        raise Stop(f"{cfg['az']} 沒有預設子網。")
    subnet_id = subnets[0]["SubnetId"]
    tables = aws("ec2", "describe-route-tables", "--filters", f"Name=association.subnet-id,Values={subnet_id}")["RouteTables"] \
        or aws("ec2", "describe-route-tables", "--filters", f"Name=vpc-id,Values={vpc_id}",
               "Name=association.main,Values=true")["RouteTables"]
    igw = [r for r in tables[0]["Routes"] if r.get("DestinationCidrBlock") == "0.0.0.0/0"
           and r.get("GatewayId", "").startswith("igw-")]
    if not igw:
        raise Stop("子網實際使用的路由表沒有 0.0.0.0/0 → igw-；不是公有子網，停止。")
    key_path = Path(cfg["ssh_key"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    name = f"inspection-{cfg['group']}-{cfg['owner']}"
    confirm([
        "== up.sh 將建立（W3 範圍，一台主機） ==",
        f"帳號末四碼 {ctx['account'][-4:]}，region {REGION}，commit {sha}",
        f"網路：{vpc_id} / {subnet_id}（{cfg['az']}，路由表 {tables[0]['RouteTableId']} 有 0.0.0.0/0 → {igw[0]['GatewayId']}）",
        f"AMI：{ami_id} {image['Name']}（{image['Architecture']}，{image['CreationDate']}）",
        f"1. Security group {name}-{stamp}：入站只有 TCP 22、80，來源 {cfg['source_cidr']}",
        f"2. Key pair {name}-{stamp}：匯入 {key_path}.pub（ed25519，私鑰不離開本機）",
        f"3. EC2 {INSTANCE_TYPE}：根磁碟 gp3 加密、DeleteOnTermination、IMDSv2 required，user data {size} bytes",
        f"標籤：course={lab.COURSE} week={cfg['week']} group={cfg['group']} owner={cfg['owner']}",
        "費用：t3.micro 與 gp3 依當期官方計價；回收：down.sh 以 ID terminate 並讀回",
    ])
    if not key_path.exists():
        key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key_path)], check=True)
    res = {"region": REGION, "account_last4": ctx["account"][-4:], "group": cfg["group"], "owner": cfg["owner"],
           "commit": sha, "source_cidr": cfg["source_cidr"], "history": (old or {}).get("history", [])}
    if old and old.get("instance"):
        res["history"].append({k: v for k, v in old.items() if k != "history"})
    save_json(RESOURCES, res)

    sg_id = aws("ec2", "create-security-group", "--group-name", f"{name}-{stamp}", "--vpc-id", vpc_id,
                "--description", f"W3/W4 inspection {cfg['group']}-{cfg['owner']} 22/80 from one /32",
                "--tag-specifications", tag_spec(["security-group"], cfg, f"{name}-{stamp}"))["GroupId"]
    res["security_group"] = {"id": sg_id, "created_at": utc()}
    save_json(RESOURCES, res)
    rules = [{"IpProtocol": "tcp", "FromPort": port, "ToPort": port,
              "IpRanges": [{"CidrIp": cfg["source_cidr"], "Description": desc}]}
             for port, desc in ((22, "ssh from one /32"), (80, "http from one /32"))]
    aws("ec2", "authorize-security-group-ingress", "--group-id", sg_id, "--ip-permissions", json.dumps(rules))

    key_id = aws("ec2", "import-key-pair", "--key-name", f"{name}-{stamp}",
                 "--public-key-material", f"fileb://{key_path}.pub",
                 "--tag-specifications", tag_spec(["key-pair"], cfg, f"{name}-{stamp}"))["KeyPairId"]
    res["key_pair"] = {"id": key_id, "name": f"{name}-{stamp}", "created_at": utc()}
    save_json(RESOURCES, res)

    root = image["RootDeviceName"]
    run = aws("ec2", "run-instances", "--image-id", ami_id, "--instance-type", INSTANCE_TYPE, "--count", "1",
              "--subnet-id", subnet_id, "--security-group-ids", sg_id, "--key-name", f"{name}-{stamp}",
              "--user-data", f"file://{user_data}",
              "--metadata-options", "HttpTokens=required,HttpEndpoint=enabled",
              "--block-device-mappings", json.dumps([{"DeviceName": root, "Ebs": {
                  "VolumeType": "gp3", "Encrypted": True, "DeleteOnTermination": True}}]),
              "--tag-specifications", tag_spec(["instance", "volume", "network-interface"], cfg, name))
    instance_id = run["Instances"][0]["InstanceId"]
    t0 = time.monotonic()
    res["instance"] = {"id": instance_id, "created_at": utc()}
    save_json(RESOURCES, res)
    print(f"建立 {sg_id}、{key_id}、{instance_id}；等待 running…")

    inst = wait_state(instance_id, "running")
    ip = inst.get("PublicIpAddress")
    timeline = {"running": utc()}
    early = curl(f"http://{ip}/health", timeout=8)
    timeline["early_curl"] = {"at": utc(), "exit": early[0], "http": early[1], "result": early[2][:200], "seconds": early[3]}
    volume = [m["Ebs"]["VolumeId"] for m in inst["BlockDeviceMappings"] if m["DeviceName"] == root][0]
    eni = inst["NetworkInterfaces"][0]["NetworkInterfaceId"]
    res["root_volume"] = {"id": volume, "created_at": utc()}
    res["eni"] = {"id": eni, "created_at": utc()}
    res["instance"]["public_ip"] = ip
    save_json(RESOURCES, res)
    print(f"running：{ip}；early curl exit={early[0]} HTTP {early[1]}（{early[2][:80]}）")

    health, _ = wait_health(ip, sha)
    timeline["health_200"] = utc()
    res["instance"]["timeline"] = timeline
    res["instance"]["ready_seconds"] = round(time.monotonic() - t0, 1)
    save_json(RESOURCES, res)
    print(json.dumps(health, ensure_ascii=False))
    print(f"完成：http://{ip}/health version={sha[:7]}，從 run-instances 到 200 共 {res['instance']['ready_seconds']} 秒")


def check_ownership(res, cfg):
    inst = describe_instance(res["instance"]["id"])
    if inst and not owned(inst.get("Tags"), cfg):
        raise Stop(f"{res['instance']['id']} 的標籤不是本組本人，拒絕處理。")
    sg, code = aws_code("ec2", "describe-security-groups", "--group-ids", res["security_group"]["id"])
    if sg and not owned(sg["SecurityGroups"][0].get("Tags"), cfg):
        raise Stop("SG 標籤不符，拒絕處理。")
    kp, code = aws_code("ec2", "describe-key-pairs", "--key-pair-ids", res["key_pair"]["id"])
    if kp and not owned(kp["KeyPairs"][0].get("Tags"), cfg):
        raise Stop("key pair 標籤不符，拒絕處理。")
    return inst


def cmd_down(args):
    res = current()
    cfg = load_config(argparse.Namespace(group=res["group"], owner=res["owner"], source_cidr=res["source_cidr"],
                                         week=None, az=None))
    lab.verify()
    inst = check_ownership(res, cfg)
    iid = res["instance"]["id"]
    if args.stop:
        if not inst or inst["State"]["Name"] not in ("running", "pending", "stopped"):
            raise Stop(f"{iid} 狀態不能停止。")
        confirm(["== down.sh --stop：只停止、不刪除 ==", f"EC2 {iid}（{inst['State']['Name']}，公開位址 {inst.get('PublicIpAddress')}）",
                 "保留：SG、key pair、根磁碟、ENI；停止期間磁碟仍計費"])
        res["instance"]["last_public_ip"] = inst.get("PublicIpAddress") or res["instance"].get("last_public_ip")
        save_json(RESOURCES, res)
        aws("ec2", "stop-instances", "--instance-ids", iid)
        inst = wait_state(iid, "stopped")
        res["instance"]["stopped_at"] = utc()
        save_json(RESOURCES, res)
        print(f"讀回：{iid} {inst['State']['Name']}，公開位址 {inst.get('PublicIpAddress')}（已收回）；上次位址 {res['instance']['last_public_ip']}")
        return
    ids = [("EC2 instance", iid, "terminate"), ("根 EBS", res["root_volume"]["id"], "隨終止刪除"),
           ("ENI", res["eni"]["id"], "隨終止刪除"), ("Security group", res["security_group"]["id"], "以 ID 刪除"),
           ("Key pair", res["key_pair"]["id"], "以 ID 刪除")]
    confirm(["== down.sh：只回收 .local/resources.json 裡、標籤已核對的 ID =="] +
            [f"{kind:16} {rid:24} {how}" for kind, rid, how in ids])
    if inst and inst["State"]["Name"] != "terminated":
        aws("ec2", "terminate-instances", "--instance-ids", iid)
    wait_state(iid, "terminated")
    for attempt in range(40):
        _, code = aws_code("ec2", "delete-security-group", "--group-id", res["security_group"]["id"])
        if code in (None, "InvalidGroup.NotFound"):
            break
        if code != "DependencyViolation":
            raise Stop(f"刪除 SG 失敗：{code}")
        time.sleep(5)
    aws_code("ec2", "delete-key-pair", "--key-pair-id", res["key_pair"]["id"])
    readback = {}
    for attempt in range(40):
        inst = describe_instance(iid)
        readback = {
            "instance": inst["State"]["Name"] if inst else "not found",
            "root_volume": aws_code("ec2", "describe-volumes", "--volume-ids", res["root_volume"]["id"])[1] or "still exists",
            "eni": aws_code("ec2", "describe-network-interfaces", "--network-interface-ids", res["eni"]["id"])[1] or "still exists",
            "security_group": aws_code("ec2", "describe-security-groups", "--group-ids", res["security_group"]["id"])[1] or "still exists",
            "key_pair": aws_code("ec2", "describe-key-pairs", "--key-pair-ids", res["key_pair"]["id"])[1] or "still exists",
        }
        if "still exists" not in readback.values():
            break
        time.sleep(5)
    res["reclaimed"] = {"at": utc(), "readback": readback}
    save_json(RESOURCES, res)
    print("讀回（" + res["reclaimed"]["at"] + "）：")
    for key, value in readback.items():
        print(f"  {key:15} {value}")
    if "still exists" in readback.values():
        raise Stop("仍有資源存在，稍後再執行 down.sh 讀回。")


def cmd_start(args):
    res = current()
    cfg = load_config(argparse.Namespace(group=res["group"], owner=res["owner"], source_cidr=res["source_cidr"],
                                         week=None, az=None))
    lab.verify()
    inst = check_ownership(res, cfg)
    iid = res["instance"]["id"]
    old_ip = res["instance"].get("last_public_ip")
    now_ip = egress_ip() + "/32"
    confirm(["== 啟動上週保留的主機 ==", f"EC2 {iid}（{inst['State']['Name']}），上次公開位址 {old_ip}"])
    aws("ec2", "start-instances", "--instance-ids", iid)
    t0 = time.monotonic()
    inst = wait_state(iid, "running")
    new_ip = inst.get("PublicIpAddress")
    running_s = round(time.monotonic() - t0, 1)
    health, _ = wait_health(new_ip, res["commit"])
    ready_s = round(time.monotonic() - t0, 1)
    res["instance"].update(public_ip=new_ip, started_again_at=utc())
    res["t1"] = {"old_ip": old_ip, "new_ip": new_ip, "running_seconds": running_s, "health_seconds": ready_s,
                 "sg_source": res["source_cidr"], "egress_now": now_ip, "health": health}
    save_json(RESOURCES, res)
    print(f"舊位址 {old_ip} → 新位址 {new_ip}；running {running_s} 秒、/health 200 {ready_s} 秒")
    print(f"出口 /32：SG 來源 {res['source_cidr']}，現在 {now_ip}，" + ("沒變，不動 SG" if now_ip == res["source_cidr"] else "已改變：列舊／新，經審查者核准後再改"))
    print(json.dumps(health, ensure_ascii=False))


def cmd_status(args):
    res = current()
    lab.verify()
    inst = describe_instance(res["instance"]["id"])
    sg, _ = aws_code("ec2", "describe-security-groups", "--group-ids", res["security_group"]["id"])
    print(f"instance {res['instance']['id']}: {inst['State']['Name'] if inst else 'not found'}, public ip {inst.get('PublicIpAddress') if inst else None}")
    if sg:
        for perm in sg["SecurityGroups"][0]["IpPermissions"]:
            print(f"  SG in tcp/{perm.get('FromPort')} from {[r['CidrIp'] for r in perm.get('IpRanges', [])]} {perm.get('Ipv6Ranges') or ''}")
    print(f"egress now {egress_ip()}/32, SG source {res['source_cidr']}")


def cmd_sources(args):
    """Add exact /32 sources (e.g. a NAT pool whose egress rotates); prints old/new and waits for yes."""
    res = current()
    cfg = load_config(argparse.Namespace(group=res["group"], owner=res["owner"], source_cidr=res["source_cidr"],
                                         week=None, az=None))
    for cidr in args.add:
        load_config(argparse.Namespace(group=cfg["group"], owner=cfg["owner"], source_cidr=cidr, week=None, az=None))
    lab.verify()
    check_ownership(res, cfg)
    sg_id = res["security_group"]["id"]
    perms = aws("ec2", "describe-security-groups", "--group-ids", sg_id)["SecurityGroups"][0]["IpPermissions"]
    old = sorted({r["CidrIp"] for p in perms for r in p.get("IpRanges", [])})
    new = [c for c in args.add if c not in old]
    if not new:
        print(f"沒有需要新增的來源；目前 {old}")
        return
    confirm([f"== SG {sg_id} 來源變更（只新增精確 /32，TCP 22、80） ==", f"舊：{old}", f"新：{sorted(old + new)}",
             "理由：" + args.reason])
    rules = [{"IpProtocol": "tcp", "FromPort": port, "ToPort": port,
              "IpRanges": [{"CidrIp": cidr, "Description": f"{desc} (added: {args.reason[:60]})"} for cidr in new]}
             for port, desc in ((22, "ssh from one /32"), (80, "http from one /32"))]
    aws("ec2", "authorize-security-group-ingress", "--group-id", sg_id, "--ip-permissions", json.dumps(rules))
    res.setdefault("source_changes", []).append({"at": utc(), "old": old, "added": new, "reason": args.reason})
    save_json(RESOURCES, res)
    perms = aws("ec2", "describe-security-groups", "--group-ids", sg_id)["SecurityGroups"][0]["IpPermissions"]
    for perm in perms:
        print(f"讀回 tcp/{perm.get('FromPort')}: {sorted(r['CidrIp'] for r in perm.get('IpRanges', []))} ipv6={perm.get('Ipv6Ranges')}")


def free_cidrs(vpc_cidr, taken, count):
    """First `count` /24 blocks inside the VPC that overlap no existing subnet."""
    taken = [ipaddress.ip_network(c) for c in taken]
    found = []
    for net in ipaddress.ip_network(vpc_cidr).subnets(new_prefix=24):
        if not any(net.overlaps(t) for t in taken):
            found.append(str(net))
            if len(found) == count:
                return found
    raise Stop(f"{vpc_cidr} 裡找不到 {count} 段不重疊的 /24。")


def db_readback(db_id):
    db = aws("rds", "describe-db-instances", "--db-instance-identifier", db_id)["DBInstances"][0]
    return {"status": db["DBInstanceStatus"], "PubliclyAccessible": db["PubliclyAccessible"],
            "engine": f"{db['Engine']} {db['EngineVersion']}", "class": db["DBInstanceClass"],
            "storage": f"{db['AllocatedStorage']} GiB {db['StorageType']}", "encrypted": db["StorageEncrypted"],
            "multi_az": db["MultiAZ"], "az": db.get("AvailabilityZone"), "db_name": db.get("DBName"),
            "endpoint": (db.get("Endpoint") or {}).get("Address"),
            "subnet_group": db["DBSubnetGroup"]["DBSubnetGroupName"],
            "security_groups": [g["VpcSecurityGroupId"] for g in db["VpcSecurityGroups"]]}


def cmd_db_up(args):
    """W5 T2: 2 private subnets + local-only route table + DB subnet group + SG-db + private RDS PostgreSQL."""
    res = current()
    cfg = load_config(argparse.Namespace(group=res["group"], owner=res["owner"], source_cidr=res["source_cidr"],
                                         week=None, az=None))
    ctx = lab.verify()
    inst = check_ownership(res, cfg)
    if not inst or inst["State"]["Name"] == "terminated":
        raise Stop("主機不存在；先用 up.sh 建立，SG-db 的來源要是主機現在用的 SG。")
    if res.get("db", {}).get("rds"):
        raise Stop(f"已經有 RDS {res['db']['rds']['id']}；不要重跑 db-up.sh（會建出第二台）。")
    vpc_id = inst["VpcId"]
    host_sgs = [g["GroupId"] for g in inst["SecurityGroups"]]
    if host_sgs != [res["security_group"]["id"]]:
        raise Stop(f"主機的 SG {host_sgs} 與 resources.json 不符；停止。")
    host_sg = host_sgs[0]
    vpc_cidr = aws("ec2", "describe-vpcs", "--vpc-ids", vpc_id)["Vpcs"][0]["CidrBlock"]
    existing = aws("ec2", "describe-subnets", "--filters", f"Name=vpc-id,Values={vpc_id}")["Subnets"]
    cidrs = free_cidrs(vpc_cidr, [s["CidrBlock"] for s in existing], 2)
    host_az = inst["Placement"]["AvailabilityZone"]
    zones = [z["ZoneName"] for z in aws("ec2", "describe-availability-zones", "--filters",
                                        "Name=state,Values=available", "Name=zone-type,Values=availability-zone")
             ["AvailabilityZones"]]
    azs = [host_az] + [z for z in zones if z != host_az][:1]
    if len(azs) < 2:
        raise Stop("找不到第二個 AZ。")
    name = f"inspection-{cfg['group']}-{cfg['owner']}"
    db_id = f"{name}-db"
    db_user = "inspection_app"
    confirm([
        "== db-up.sh 將建立（W5 T2，私有資料庫） ==",
        f"帳號末四碼 {ctx['account'][-4:]}，region {REGION}，VPC {vpc_id}（{vpc_cidr}）",
        f"1. 私有子網 ×2：{cidrs[0]}（{azs[0]}）、{cidrs[1]}（{azs[1]}）；不與既有 {len(existing)} 個子網重疊",
        f"   新路由表 {name}-private：只有 local 路由，明確關聯上面兩個子網",
        f"2. DB 子網群組 {name}-dbsubnets；SG {name}-db：入站只有 TCP 5432，來源 = 主機 SG {host_sg}",
        f"3. RDS {db_id}：PostgreSQL、db.t3.micro、20 GiB gp3、不公開、儲存加密、單一 AZ（{azs[0]}）、資料庫 inspection",
        "4. 密碼由本腳本產生、寫進 .local/db.env（600）；經 600 暫存檔交給 AWS CLI，不出現在命令列、不顯示",
        "5. 每項 ID 寫進 .local/resources.json；結束讀回 available 與 PubliclyAccessible=false",
        f"標籤：course={lab.COURSE} week=w05 group={cfg['group']} owner={cfg['owner']}",
        "費用：db.t3.micro 與 20 GiB gp3 依當期官方計價；停止最多 7 天會被 AWS 自動啟動",
    ])
    tcfg = dict(cfg, week="w05")
    db = res.setdefault("db", {})

    def record(key, rid, **extra):
        db[key] = dict({"id": rid, "created_at": utc()}, **extra)
        save_json(RESOURCES, res)

    subnet_ids = []
    for i, (cidr, az) in enumerate(zip(cidrs, azs), 1):
        sid = aws("ec2", "create-subnet", "--vpc-id", vpc_id, "--cidr-block", cidr, "--availability-zone", az,
                  "--tag-specifications", tag_spec(["subnet"], tcfg, f"{name}-private-{i}"))["Subnet"]["SubnetId"]
        record(f"subnet_{i}", sid, cidr=cidr, az=az)
        subnet_ids.append(sid)
    rtb = aws("ec2", "create-route-table", "--vpc-id", vpc_id,
              "--tag-specifications", tag_spec(["route-table"], tcfg, f"{name}-private"))["RouteTable"]["RouteTableId"]
    record("route_table", rtb)
    db["route_table"]["associations"] = [
        aws("ec2", "associate-route-table", "--route-table-id", rtb, "--subnet-id", sid)["AssociationId"]
        for sid in subnet_ids]
    save_json(RESOURCES, res)
    group_name = f"{name}-dbsubnets"
    aws("rds", "create-db-subnet-group", "--db-subnet-group-name", group_name,
        "--db-subnet-group-description", f"W5 private subnets {cfg['group']}-{cfg['owner']}",
        "--subnet-ids", *subnet_ids, "--tags", json.dumps(tags(tcfg, group_name)))
    record("subnet_group", group_name)
    sg_db = aws("ec2", "create-security-group", "--group-name", f"{name}-db", "--vpc-id", vpc_id,
                "--description", f"W5 RDS {cfg['group']}-{cfg['owner']}: 5432 from host SG only",
                "--tag-specifications", tag_spec(["security-group"], tcfg, f"{name}-db"))["GroupId"]
    record("security_group", sg_db, source_sg=host_sg)
    aws("ec2", "authorize-security-group-ingress", "--group-id", sg_db, "--ip-permissions", json.dumps([{
        "IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
        "UserIdGroupPairs": [{"GroupId": host_sg, "Description": "postgres from inspection host SG"}]}]))

    password = secrets.token_urlsafe(24)  # URL-safe: no / @ " or space, which RDS rejects
    lab.atomic_write(DB_ENV, f"DB_HOST=\nDB_PORT=5432\nDB_NAME=inspection\nDB_USER={db_user}\nDB_PASSWORD={password}\n")
    request = {"DBInstanceIdentifier": db_id, "Engine": "postgres", "DBInstanceClass": "db.t3.micro",
               "AllocatedStorage": 20, "StorageType": "gp3", "StorageEncrypted": True, "PubliclyAccessible": False,
               "MultiAZ": False, "AvailabilityZone": azs[0], "DBName": "inspection", "MasterUsername": db_user,
               "MasterUserPassword": password, "DBSubnetGroupName": group_name, "VpcSecurityGroupIds": [sg_db],
               "BackupRetentionPeriod": 1, "DeletionProtection": False, "AutoMinorVersionUpgrade": True,
               "Tags": tags(tcfg, db_id)}
    fd, tmp = tempfile.mkstemp(prefix=".rds-", suffix=".json", dir=LOCAL)
    try:
        with os.fdopen(fd, "w") as stream:
            os.chmod(tmp, 0o600)
            json.dump(request, stream)
        aws("rds", "create-db-instance", "--cli-input-json", f"file://{tmp}")
    finally:
        os.unlink(tmp)
    t0 = time.monotonic()
    record("rds", db_id)
    print(f"建立 {subnet_ids[0]}、{subnet_ids[1]}、{rtb}、{group_name}、{sg_db}、{db_id}；等待 available（約 7 分鐘）…")
    status = None
    while time.monotonic() - t0 < 1800:
        status = db_readback(db_id)["status"]
        if status == "available":
            break
        time.sleep(20)
    else:
        raise Stop(f"{db_id} 30 分鐘內沒有變成 available（最後 {status}）；不要重跑，稍後用 db-status 讀回。")
    readback = db_readback(db_id)
    db["rds"]["minutes_to_available"] = round((time.monotonic() - t0) / 60, 1)
    db["rds"]["readback"] = readback
    save_json(RESOURCES, res)
    env = DB_ENV.read_text().replace("DB_HOST=\n", f"DB_HOST={readback['endpoint']}\n", 1)
    lab.atomic_write(DB_ENV, env)
    print_db_readback(res)


def print_db_readback(res):
    db = res["db"]
    rb = db_readback(db["rds"]["id"])
    tables = aws("ec2", "describe-route-tables", "--route-table-ids", db["route_table"]["id"])["RouteTables"][0]
    sg = aws("ec2", "describe-security-groups", "--group-ids", db["security_group"]["id"])["SecurityGroups"][0]
    print(f"讀回（{utc()}）：")
    print(f"  RDS ...{db['rds']['id'][-4:]}  status={rb['status']}  PubliclyAccessible={rb['PubliclyAccessible']}")
    print(f"  {rb['engine']}  {rb['class']}  {rb['storage']}  encrypted={rb['encrypted']}  multi_az={rb['multi_az']}  az={rb['az']}  db={rb['db_name']}")
    for i in (1, 2):
        s = db[f"subnet_{i}"]
        print(f"  subnet ...{s['id'][-4:]}  {s['cidr']}  {s['az']}")
    routes = [f"{r.get('DestinationCidrBlock')}→{r.get('GatewayId') or r.get('NatGatewayId') or '?'}" for r in tables["Routes"]]
    assoc = sorted("..." + a["SubnetId"][-4:] for a in tables["Associations"] if a.get("SubnetId"))
    print(f"  route table ...{tables['RouteTableId'][-4:]}  routes={routes}  associated={assoc}")
    for p in sg["IpPermissions"]:
        print(f"  SG-db ...{sg['GroupId'][-4:]}  in tcp/{p.get('FromPort')}  from SG {['...' + g['GroupId'][-4:] for g in p.get('UserIdGroupPairs', [])]}"
              f"  cidrs={[r['CidrIp'] for r in p.get('IpRanges', [])]}")
    if "minutes_to_available" in db["rds"]:
        print(f"  從 create-db-instance 到 available：{db['rds']['minutes_to_available']} 分鐘")


def cmd_db_status(args):
    res = current()
    lab.verify()
    if not res.get("db", {}).get("rds"):
        raise Stop("resources.json 沒有 RDS 紀錄。")
    print_db_readback(res)


def cmd_deploy(args):
    sha = resolve_commit("HEAD")
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "app/service.py", "deploy/nginx.conf"],
                           cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    if dirty:
        raise Stop("app/service.py 或 deploy/nginx.conf 有未 commit 的修改；只部署已 commit 的版本，先 commit。")
    if not APP_ENV.exists() or (APP_ENV.stat().st_mode & 0o777) != 0o600:
        raise Stop(".local/app.env 不存在或權限不是 600；停止部署。")
    names = {line.split("=", 1)[0] for line in APP_ENV.read_text().splitlines() if "=" in line and line.split("=", 1)[1]}
    if not {"REPORTER_TOKEN", "OPERATOR_TOKEN"} <= names:
        raise Stop(".local/app.env 缺少 REPORTER_TOKEN 或 OPERATOR_TOKEN（不顯示內容）。")
    secret_files = [APP_ENV]
    if DB_ENV.exists():  # W5: the DB settings go into the same root-only secret file
        if (DB_ENV.stat().st_mode & 0o777) != 0o600:
            raise Stop(".local/db.env 權限不是 600；停止部署。")
        db_names = {line.split("=", 1)[0] for line in DB_ENV.read_text().splitlines()
                    if "=" in line and line.split("=", 1)[1]}
        if not {"DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"} <= db_names:
            raise Stop(".local/db.env 缺少 DB_HOST／DB_NAME／DB_USER／DB_PASSWORD（RDS 還沒 available？不顯示內容）。")
        secret_files.append(DB_ENV)
    res = current()
    cfg = load_config(argparse.Namespace(group=res["group"], owner=res["owner"], source_cidr=res["source_cidr"],
                                         week=None, az=None))
    lab.verify()
    inst = check_ownership(res, cfg)
    if not inst or inst["State"]["Name"] != "running":
        raise Stop(f"{res['instance']['id']} 不是 running；先啟動主機。")
    ip = inst["PublicIpAddress"]
    sha, user_data, size = build_user_data(sha)
    confirm(["== deploy.sh：更新同一台主機 ==", f"目標主機：{res['instance']['id']} ec2-user@{ip}",
             f"commit：{sha}（{subprocess.check_output(['git', 'log', '-1', '--format=%s', sha], cwd=ROOT, text=True).strip()}）",
             f"步驟：SSH 執行安裝腳本（{size} bytes）→ {' + '.join(p.name for p in secret_files)} 經 stdin 放到 "
             "/etc/inspection/app.env（root 600）→ 重啟 inspection → 驗 /health"])
    base = ssh_base(cfg, ip)
    with user_data.open("rb") as script:
        run = subprocess.run(base + ["sudo bash -s"], stdin=script, capture_output=True, text=True, check=False)
    if run.returncode:
        raise Stop(f"安裝腳本失敗（exit {run.returncode}）：{run.stderr.strip().splitlines()[-1:]}")
    print("1/3 安裝腳本完成")
    payload = b"".join(p.read_bytes().rstrip(b"\n") + b"\n" for p in secret_files)
    # /etc/inspection stays 755 (the service user reads rds-ca.pem there); app.env itself is root 600.
    run = subprocess.run(base + ["sudo install -d -m 755 -o root -g root /etc/inspection && "
                                 "sudo install -m 600 -o root -g root /dev/stdin /etc/inspection/app.env"],
                         input=payload, capture_output=True, check=False)
    if run.returncode:
        raise Stop(f"放置秘密檔失敗（exit {run.returncode}）。")
    run = subprocess.run(base + ["sudo systemctl restart inspection && sudo stat -c '%U:%G %a %n' /etc/inspection/app.env"],
                         capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL)
    if run.returncode:
        raise Stop(f"重啟 inspection 失敗（exit {run.returncode}）。")
    print("2/3 秘密檔：" + run.stdout.strip())
    health, _ = wait_health(ip, sha, need_auth=True, limit=120)
    if DB_ENV in secret_files and health.get("db_configured") is not True:
        raise Stop("db.env 已放上主機，但 /health 的 db_configured 不是 true；看 sudo journalctl -u inspection -n 30。")
    res = current()  # re-read: another helper (e.g. db-up) may have written resources.json meanwhile
    res["deployed"] = {"commit": sha, "at": utc(), "public_ip": ip, "health": health}
    save_json(RESOURCES, res)
    print("3/3 " + json.dumps(health, ensure_ascii=False))
    print(f"完成：version = {sha[:7]}，auth_configured = true，db_configured = {str(health.get('db_configured')).lower()}")


def main():
    global REGION
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up")
    up.add_argument("--commit", default="HEAD")
    for name in ("group", "owner", "source_cidr", "week", "az"):
        up.add_argument("--" + name.replace("_", "-"), dest=name)
    down = sub.add_parser("down")
    down.add_argument("--stop", action="store_true", help="只停止主機，保留到下週")
    sub.add_parser("start")
    sub.add_parser("status")
    sub.add_parser("deploy")
    sub.add_parser("db-up")
    sub.add_parser("db-status")
    sources = sub.add_parser("sources")
    sources.add_argument("--add", nargs="+", required=True, metavar="IP/32")
    sources.add_argument("--reason", required=True)
    args = parser.parse_args()
    try:
        REGION = lab.context()["region"]
        {"up": cmd_up, "down": cmd_down, "start": cmd_start, "status": cmd_status, "deploy": cmd_deploy,
         "sources": cmd_sources, "db-up": cmd_db_up, "db-status": cmd_db_status}[args.command](args)
    except (Stop, lab.LabError) as exc:
        print("STOP: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
