#!/usr/bin/env python3
"""BGP route policy lab 自动测试。

用法:
    ./test_lab.py              启动环境、依序运行所有情境、输出 PASS/FAIL(任一 FAIL 则 exit 1)
    ./test_lab.py --keep       测试后保留容器(缺省测完会清理)
    ./test_lab.py s03 s04      只跑指定情境(以名称前缀比对)
只需要 python3(>=3.6)、docker 与 docker compose v2。
"""
import json
import os
import subprocess
import sys
import time

LAB = os.path.dirname(os.path.abspath(__file__))
PE = "lab-pe"
PEERS = ["172.30.1.2", "172.30.2.2", "172.30.3.2", "172.30.4.2"]  # r1 r2 r3 r4(iBGP)
R1, R2, R3, R4 = PEERS
TIMEOUT = 40  # 每个断言最长轮询秒数(收敛等待)

results = []


def run(cmd, stdin=None, check=True):
    p = subprocess.run(cmd, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, cwd=LAB)
    if check and p.returncode != 0:
        raise RuntimeError("command failed: %s\n%s" % (" ".join(cmd), p.stdout))
    return p.stdout


def vt(node, *cmds):
    """在容器 lab-<node> 运行 vtysh -c ...,回传文本输出。"""
    args = ["docker", "exec", "lab-" + node if not node.startswith("lab-") else node, "vtysh"]
    for c in cmds:
        args += ["-c", c]
    return run(args, check=False)


def vj(node, cmd):
    out = vt(node, cmd)
    try:
        return json.loads(out)
    except ValueError:
        return {}


def paths(node, prefix):
    """回传某前缀在 node BGP 表中的所有 path(list of dict);没有则 []。"""
    return vj(node, "show bgp ipv4 unicast %s json" % prefix).get("paths", [])


def best(node, prefix):
    for p in paths(node, prefix):
        if p.get("bestpath", {}).get("overall"):
            return p
    return None


def apply_config(container, path):
    run(["docker", "exec", "-i", container, "vtysh", "-f", "/dev/stdin"], stdin=open(os.path.join(LAB, path)).read())


def wait_for(fn, timeout=TIMEOUT, interval=1.0):
    """轮询 fn() 直到回传真值或逾时;例外视为未成功。"""
    end = time.time() + timeout
    while True:
        try:
            if fn():
                return True
        except Exception:
            pass
        if time.time() >= end:
            return False
        time.sleep(interval)


def check(desc, fn, timeout=TIMEOUT):
    ok = wait_for(fn, timeout)
    results.append(ok)
    print("  [%s] %s" % ("PASS" if ok else "FAIL", desc))
    return ok


def established():
    peers = vj(PE, "show bgp ipv4 unicast summary json").get("peers", {})
    return all(peers.get(ip, {}).get("state") == "Established" for ip in PEERS)


def load_pe(scn):
    run(["docker", "restart", PE])
    wait_for(lambda: "bgpd" in vt(PE, "show daemons"), 20, 0.5)
    apply_config(PE, "scenarios/" + scn)
    if not wait_for(established, 60):
        print("  [FAIL] 四个 BGP session 未在时限内 Established")
        results.append(False)


def has(node, prefix):
    return len(paths(node, prefix)) > 0


def attr(node, prefix, key, peer=None):
    for p in paths(node, prefix):
        if peer is None or p.get("peer", {}).get("peerId") == peer:
            return p.get(key)
    return None


def comm(node, prefix, key="community"):
    return (attr(node, prefix, key) or {}).get("string", "")


def aspath(node, prefix, peer=None):
    return (attr(node, prefix, "aspath", peer) or {}).get("string")


def best_peer(node, prefix):
    b = best(node, prefix)
    return b["peer"]["peerId"] if b else None


# ---------------------------------------------------------------- 情境
def s00():
    check("ebgp-requires-policy:r1/r2/r3 session Established 但 peerState=Policy、收 0 条",
          lambda: all(vj(PE, "show bgp ipv4 unicast summary json")["peers"][ip]["peerState"] == "Policy"
                      and vj(PE, "show bgp ipv4 unicast summary json")["peers"][ip]["pfxRcd"] == 0
                      for ip in (R1, R2, R3)))
    check("pe 的 BGP 表没有 10.1.0.0/24", lambda: not has(PE, "10.1.0.0/24"))
    check("r1 没有收到 pe 任何路由(outbound 也被挡)", lambda: not has("r1", "20.0.0.0/24"))


def s01():
    check("pe 收下 10.1.0.0/24 与 10.1.1.0/24",
          lambda: has(PE, "10.1.0.0/24") and has(PE, "10.1.1.0/24"))
    check("prefix-list deny:10.1.2.0/24 被过滤", lambda: not has(PE, "10.1.2.0/24"))
    check("implicit deny:192.0.2.0/24 被过滤", lambda: not has(PE, "192.0.2.0/24"))
    recv = lambda: set(vj(PE, "show bgp ipv4 unicast neighbors %s received-routes json" % R1).get("receivedRoutes", {}))
    check("soft-reconfiguration: received-routes 仍看得到 r1 送来的 4 条原始路由(含被过滤的)",
          lambda: {"10.1.0.0/24", "10.1.1.0/24", "10.1.2.0/24", "192.0.2.0/24"} <= recv())
    check("outbound PERMIT-ALL:r2 收得到 10.1.0.0/24", lambda: has("r2", "10.1.0.0/24"))
    # 在线修改 policy,再 clear soft in 刷新
    vt(PE, "configure terminal", "no ip prefix-list CUST-IN seq 5")
    vt(PE, "clear bgp ipv4 unicast %s soft in" % R1)
    check("移除 deny 并 clear bgp soft in 后 10.1.2.0/24 出现", lambda: has(PE, "10.1.2.0/24"))
    check("192.0.2.0/24 仍被过滤(不在 10.1.0.0/16 内)", lambda: not has(PE, "192.0.2.0/24"))


def s02():
    check("pe 收下 r2 的 20.0.0.0/24 与 30.0.0.0/24",
          lambda: has(PE, "20.0.0.0/24") and has(PE, "30.0.0.0/24"))
    check("as-path 含 65099 的 20.0.1.0/24 被过滤(以 _65099_ 比对)", lambda: not has(PE, "20.0.1.0/24"))


def s03():
    check("30.0.0.0/24 有两条 path(r2、r3)", lambda: len(paths(PE, "30.0.0.0/24")) == 2)
    check("来自 r3 的 path local-pref 被改成 200",
          lambda: attr(PE, "30.0.0.0/24", "locPrf", R3) == 200)
    check("来自 r2 的 path 维持缺省 local-pref 100",
          lambda: attr(PE, "30.0.0.0/24", "locPrf", R2) in (None, 100))  # eBGP 默认值不显示于 JSON,等同 100
    check("最佳路径为 r3(selectionReason=Local Pref)",
          lambda: best_peer(PE, "30.0.0.0/24") == R3 and best(PE, "30.0.0.0/24")["bestpath"]["selectionReason"] == "Local Pref")
    check("local-pref 经 iBGP 传给 r4(r4 看到 locPrf=200)",
          lambda: attr("r4", "40.0.0.0/24", "locPrf") == 200)
    check("未做 next-hop-self:r4 无法解析 next-hop,路由不进 RIB",
          lambda: not attr("r4", "40.0.0.0/24", "valid") and "40.0.0.0/24" not in vt("r4", "show ip route bgp"))


def s04():
    check("30.0.0.0/24 有两条 path", lambda: len(paths(PE, "30.0.0.0/24")) == 2)
    check("r2 path weight=100", lambda: attr(PE, "30.0.0.0/24", "weight", R2) == 100)
    check("r3 path local-pref=200", lambda: attr(PE, "30.0.0.0/24", "locPrf", R3) == 200)
    check("weight 优先于 local-pref:最佳路径切换成 r2",
          lambda: best_peer(PE, "30.0.0.0/24") == R2 and best(PE, "30.0.0.0/24")["bestpath"]["selectionReason"] == "Weight")


def s05():
    check("outbound set metric:r2 看到 10.1.0.0/24 的 MED=50",
          lambda: has(PE, "10.1.0.0/24") and attr("r2", "10.1.0.0/24", "metric") == 50)
    check("outbound prepend:r3 看到 AS-path '65000 65000 65000 65001'",
          lambda: aspath("r3", "10.1.0.0/24") == "65000 65000 65000 65001")
    check("r2 的 AS-path 没被 prepend('65000 65001')", lambda: aspath("r2", "10.1.0.0/24") == "65000 65001")


def s06():
    check("10.1.0.0/24 在 pe 被标 community 65000:100 与 large-community 65000:1:100",
          lambda: comm(PE, "10.1.0.0/24") == "65000:100" and comm(PE, "10.1.0.0/24", "largeCommunity") == "65000:1:100")
    check("10.1.1.0/24 被标 community 65000:200", lambda: comm(PE, "10.1.1.0/24") == "65000:200")
    check("outbound community-list:r2 只收到 GOLD(10.1.0.0/24)且保留 community",
          lambda: comm("r2", "10.1.0.0/24") == "65000:100")
    check("r2 没有 10.1.1.0/24、10.1.2.0/24", lambda: not has("r2", "10.1.1.0/24") and not has("r2", "10.1.2.0/24"))
    check("r3 收到 SILVER 路由且被加上 no-export",
          lambda: comm("r3", "10.1.1.0/24") == "65000:200 no-export")
    check("r3 没有 GOLD 路由 10.1.0.0/24", lambda: not has("r3", "10.1.0.0/24"))


def s07():
    check("seq 10 命中:10.1.0.0/24 有 community 65000:100", lambda: comm(PE, "10.1.0.0/24") == "65000:100")
    check("continue 20:10.1.0.0/24 同时套用 seq 20 的 local-pref 150",
          lambda: attr(PE, "10.1.0.0/24", "locPrf") == 150)
    check("seq 10 未命中:10.1.1.0/24 没有 community 但有 local-pref 150",
          lambda: comm(PE, "10.1.1.0/24") == "" and attr(PE, "10.1.1.0/24", "locPrf") == 150)


def s08():
    check("next-hop-self:r4 的 40.0.0.0/24 next-hop 变成 pe(172.30.4.3)",
          lambda: vj("r4", "show ip route 40.0.0.0/24 json").get("40.0.0.0/24", [{}])[0]
          .get("nexthops", [{}])[0].get("ip") == "172.30.4.3")
    check("r4 的路由保有 local-pref 200 并进入 RIB",
          lambda: attr("r4", "40.0.0.0/24", "locPrf") == 200 and attr("r4", "40.0.0.0/24", "valid") is True)


def s09():
    check("route-map 未定义:r1 session 起来但 pe 收 0 条",
          lambda: vj(PE, "show bgp ipv4 unicast summary json")["peers"][R1]["pfxRcd"] == 0 and not has(PE, "10.1.0.0/24"))
    check("route-map 未定义:r2/r3 因 ebgp-requires-policy 仍是 Policy",
          lambda: vj(PE, "show bgp ipv4 unicast summary json")["peers"][R2]["peerState"] == "Policy")
    check("show route-map 查不到 NO-SUCH-MAP", lambda: "NO-SUCH-MAP" not in vt(PE, "show route-map"))


def s10():
    check("no bgp ebgp-requires-policy:无 policy 也收到所有路由",
          lambda: all(has(PE, p) for p in ("10.1.0.0/24", "10.1.2.0/24", "192.0.2.0/24", "20.0.1.0/24", "40.0.0.0/24")))
    check("20.0.1.0/24 的 AS-path 为 '65002 65099 65088'", lambda: aspath(PE, "20.0.1.0/24") == "65002 65099 65088")


SCENARIOS = [
    ("s00-no-policy.conf", s00), ("s01-prefix-list.conf", s01), ("s02-as-path.conf", s02),
    ("s03-local-pref.conf", s03), ("s04-weight.conf", s04), ("s05-out-med-prepend.conf", s05),
    ("s06-community.conf", s06), ("s07-continue.conf", s07), ("s08-next-hop-self.conf", s08),
    ("s09-undefined-route-map.conf", s09), ("s10-no-ebgp-requires-policy.conf", s10),
]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    keep = "--keep" in sys.argv
    print("== 启动环境 ==")
    run(["docker", "compose", "up", "-d"])
    try:
        for r in ("r1", "r2", "r3", "r4"):
            wait_for(lambda r=r: "bgpd" in vt(r, "show daemons"), 30, 0.5)
            apply_config("lab-" + r, "routers/%s/frr.conf" % r)
        for scn, fn in SCENARIOS:
            if args and not any(scn.startswith(a) for a in args):
                continue
            print("== %s ==" % scn)
            load_pe(scn)
            fn()
    finally:
        if not keep:
            print("== 清理环境 ==")
            run(["docker", "compose", "down", "-v", "--remove-orphans"], check=False)
    passed, total = sum(results), len(results)
    print("\n结果:%d/%d PASS" % (passed, total))
    print("ALL PASS" if passed == total and total else "FAILED")
    sys.exit(0 if passed == total and total else 1)


if __name__ == "__main__":
    main()
