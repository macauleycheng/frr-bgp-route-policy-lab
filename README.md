# 用 Docker + FRR 学习 BGP Route Policy

这个 lab 用 5 个 FRR 容器组成一个小型 BGP 网络,逐一示范 route-map、prefix-list、as-path access-list、community 等 policy 工具。每个情境有一份完整的 `pe` 配置档(`scenarios/`)、文档中的预期输出,以及自动断言(`test_lab.py`)。

文中的输出皆拷贝自实际运行结果(FRR `v8.4.1`,见最后一节的环境说明)。

## 目录

```
docker-compose.yml     拓朴:pe + r1~r4,固定 IP,所有 link 为 internal network
common/                daemons(激活 bgpd)、空的 vtysh.conf
routers/r1..r4/        对等节点的固定配置(只负责发送路由,不含实验 policy)
scenarios/s00..s10     套用在 pe 的各情境配置(每份都是完整配置)
test_lab.py            启动 -> 逐情境套用 -> 轮询断言 -> PASS/FAIL -> 清理
clean.sh               手动清理
```

## 快速开始

需求:docker、docker compose v2、python3。

```bash
./test_lab.py            # 跑全部情境,最后印出 "ALL PASS"(exit 0)或 "FAILED"(exit 1);测完自动清理
./test_lab.py s03 s04    # 只跑指定情境
./test_lab.py --keep     # 测完保留容器,方便自己进去玩
./clean.sh               # 手动清理容器与网络
```

手动操作某个情境(例如 s03):

```bash
docker compose up -d
for r in r1 r2 r3 r4; do docker exec -i lab-$r vtysh -f /dev/stdin < routers/$r/frr.conf; done
docker restart lab-pe && sleep 3                       # 切换情境前重启 pe,清掉前一个情境的配置
docker exec -i lab-pe vtysh -f /dev/stdin < scenarios/s03-local-pref.conf
docker exec -it lab-pe vtysh                           # 进入交互式 CLI
```

## 拓朴

```
                 AS 65001            AS 65002            AS 65003
                  r1                  r2                  r3
               (客户端)             (上游 A)            (上游 B)
              172.30.1.2          172.30.2.2          172.30.3.2
                   |                   |                   |
              172.30.1.3          172.30.2.3          172.30.3.3
                   +-------------------+-------------------+
                                       pe  AS 65000  (所有 policy 都套在这里)
                                   172.30.4.3
                                       |
                                  172.30.4.2
                                       r4  AS 65000 (iBGP)
```

各节点发送的路由(皆为 `ip route ... blackhole` 加 `network`):

| 节点 | 发送的前缀 | 备注 |
|------|-----------|------|
| r1 | `10.1.0.0/24` `10.1.1.0/24` `10.1.2.0/24` `192.0.2.0/24` | 客户端 |
| r2 | `20.0.0.0/24` `20.0.1.0/24` `30.0.0.0/24` | `20.0.1.0/24` 的 AS-path 被 r2 伪造成 `65002 65099 65088`,用来练习 as-path 过滤 |
| r3 | `30.0.0.0/24` `40.0.0.0/24` | `30.0.0.0/24` 与 r2 重复,用来练习最佳路径选择 |
| r4 | 无 | 只用来观察 iBGP 内属性的传递 |

r1~r3 都设了 `no bgp ebgp-requires-policy`,让它们单纯发送路由;`pe` 保留 FRR 缺省行为。

常用观察指令(都在 `pe` 上):

```bash
docker exec lab-pe vtysh -c 'show bgp ipv4 unicast'
```

---

## 1. 先看没有 policy 会怎样(情境 s00)

FRR 缺省打开 `bgp ebgp-requires-policy`:eBGP 邻居若没有配置 inbound 与 outbound policy,session 虽然会创建,但**不接收也不发送任何路由**。`scenarios/s00-no-policy.conf` 的 pe 完全没有 route-map:

```
docker exec lab-pe vtysh -c 'show bgp ipv4 unicast summary'
...
Neighbor        V         AS   MsgRcvd   MsgSent   TblVer  InQ OutQ  Up/Down State/PfxRcd   PfxSnt Desc
172.30.1.2      4      65001         8         7        0    0    0 00:00:13     (Policy) (Policy) N/A
172.30.2.2      4      65002         9         7        0    0    0 00:00:13     (Policy) (Policy) N/A
172.30.3.2      4      65003         8         7        0    0    0 00:00:13     (Policy) (Policy) N/A
172.30.4.2      4      65000         7         7        0    0    0 00:00:13            0        0 N/A

docker exec lab-pe vtysh -c 'show bgp ipv4 unicast'
No BGP prefixes displayed, 0 exist
```

重点:

- `(Policy)` 出现在 `State/PfxRcd` 与 `PfxSnt`,表示被 `ebgp-requires-policy` 挡住。
- iBGP(172.30.4.2,AS 相同)不受影响,仍显示 `0`。
- r1 同样收不到 pe 的任何路由(`show bgp ipv4 unicast` 只有自己发送的 4 条)。
- 解法有两种:写 route-map(本 lab 的做法,见情境 s01 起),或 `no bgp ebgp-requires-policy`(情境 s10)。

## 2. route-map 基本结构

```
route-map <NAME> <permit|deny> <sequence>
 match <条件>        ! 可多条;同一个 sequence 内的不同 match 是 AND
 set <动作>
 continue <seq>      ! 命中后不要结束,跳到指定 sequence
 on-match next|goto  ! 与 continue 类似的旧式语法
```

规则:

1. 依 sequence 由小到大逐一比对,**第一个命中的 sequence 决定结果**(permit 就放行并套用 `set`,deny 就丢弃),之后的 sequence 不再看。
2. sequence 没有任何 `match` 代表「全部命中」。`route-map PERMIT-ALL permit 10`(本 lab 到处使用)就是放行全部。
3. **结尾有 implicit deny**:所有 sequence 都没命中,路由被丢弃。
4. 同一个 `match` 类型的多个值是 OR(例如 prefix-list 内多行);不同类型之间是 AND。
5. route-map 套用在邻居的某个方向:`neighbor X route-map NAME in|out`(写在 `address-family ipv4 unicast` 内)。

## 3. prefix-list 过滤(情境 s01)

`scenarios/s01-prefix-list.conf` 的重点:

```
ip prefix-list CUST-IN seq 5 deny 10.1.2.0/24
ip prefix-list CUST-IN seq 10 permit 10.1.0.0/16 le 24
!
route-map FROM-R1 permit 10
 match ip address prefix-list CUST-IN
!
router bgp 65000
 address-family ipv4 unicast
  neighbor 172.30.1.2 route-map FROM-R1 in
  neighbor 172.30.1.2 soft-reconfiguration inbound
```

- prefix-list 同样由 seq 小到大比对,第一个命中的决定结果,**结尾也有 implicit deny**。
- `10.1.0.0/16 le 24` 代表「落在 10.1.0.0/16 内、遮罩长度 ≤ 24」(`ge`/`le` 控制遮罩长度范围)。
- `192.0.2.0/24` 没有任何 prefix-list 条目命中,被 implicit deny 丢弃。

预期结果:r1 的 10.1.2.0/24 与 192.0.2.0/24 都被挡,10.1.0.0/24、10.1.1.0/24 被接受:

```
docker exec lab-pe vtysh -c 'show bgp ipv4 unicast'
   Network          Next Hop            Metric LocPrf Weight Path
*> 10.1.0.0/24      172.30.1.2               0             0 65001 i
*> 10.1.1.0/24      172.30.1.2               0             0 65001 i
*> 20.0.0.0/24      172.30.2.2               0             0 65002 i
*> 20.0.1.0/24      172.30.2.2               0             0 65002 65099 65088 i
*  30.0.0.0/24      172.30.3.2               0             0 65003 i
*>                  172.30.2.2               0             0 65002 i
*> 40.0.0.0/24      172.30.3.2               0             0 65003 i
```

### 3.1 被过滤的路由去哪了:soft-reconfiguration inbound 与 received-routes

`show ... received-routes` 显示的是 **inbound policy 套用前**的原始路由,前提是该邻居开了 `soft-reconfiguration inbound`:

```
docker exec lab-pe vtysh -c 'show bgp ipv4 unicast neighbors 172.30.1.2 received-routes'
   Network          Next Hop            Metric LocPrf Weight Path
*> 10.1.0.0/24      172.30.1.2               0             0 65001 i
*> 10.1.1.0/24      172.30.1.2               0             0 65001 i
*> 10.1.2.0/24      172.30.1.2               0             0 65001 i
*> 20.0.0.0/24      172.30.1.2                             0 65001 65000 65002 i
...
*> 192.0.2.0/24     172.30.1.2               0             0 65001 i

Total number of prefixes 8 (6 filtered)
```

观察:

- 10.1.2.0/24 与 192.0.2.0/24 在这里看得到(原始数据),但不在 `show bgp ipv4 unicast`(已被 policy 过滤)。
- r1 也把 pe 之前发给它的路由送了回来(AS-path 内含 65000),它们同样被 `FROM-R1` 挡掉,这是 `6 filtered` 的由来。
- `advertised-routes` 显示 **outbound policy 套用后**实际送给邻居的路由:

```
docker exec lab-pe vtysh -c 'show bgp ipv4 unicast neighbors 172.30.2.2 advertised-routes'
   Network          Next Hop            Metric LocPrf Weight Path
*> 10.1.0.0/24      0.0.0.0                                0 65001 i
*> 10.1.1.0/24      0.0.0.0                                0 65001 i
*> 20.0.0.0/24      0.0.0.0                                0 65002 i
...
Total number of prefixes 6
```

### 3.2 修改 policy 后刷新:clear bgp

测试脚本在在线移除 deny 条目,再用 soft clear 重新套用 inbound policy(不会中断 session):

```bash
docker exec lab-pe vtysh -c 'configure terminal' -c 'no ip prefix-list CUST-IN seq 5'
docker exec lab-pe vtysh -c 'clear bgp ipv4 unicast 172.30.1.2 soft in'
```

```
*> 10.1.0.0/24      172.30.1.2               0             0 65001 i
*> 10.1.1.0/24      172.30.1.2               0             0 65001 i
*> 10.1.2.0/24      172.30.1.2               0             0 65001 i
```

10.1.2.0/24 出现,192.0.2.0/24 仍被挡(不在 10.1.0.0/16 内)。

- `soft in`:用 soft-reconfiguration 存下的原始路由(或向对方发 route-refresh)重新跑 inbound policy。
- `soft out`:重新跑 outbound policy 并重送。
- 不加 `soft` 的 `clear bgp ...` 会重置 session,正式环境尽量避免。

## 4. as-path access-list(情境 s02)

as-path access-list 用正则表达式比对 AS-path。`scenarios/s02-as-path.conf`:

```
bgp as-path access-list HAS-65099 permit _65099_
!
route-map FROM-R2 deny 10
 match as-path HAS-65099
route-map FROM-R2 permit 20
```

要点:

- `_` 代表 AS 的边界(空白、开头、结尾),所以 `_65099_` 比对「AS-path 中出现 65099 这个 AS」。常见写法:`^65002$`(只有 AS 65002 自己)、`^65002_`(来自邻居 65002 的所有路由)、`_65088$`(由 65088 起源)。
- **这里的 access-list 是「用来比对」,permit 表示命中。**真正丢弃路由的是 route-map 的 `deny 10`。这是初学者最常搞混的地方。
- `permit 20` 没有 match,作为「其余全放行」,避免 implicit deny。
- 本 lab 用的 Alpine/musl 版 FRR 有个小怪癖:正规式「结尾的 `_`」(例如要匹配 path 最后一个 AS 的 `_65099_`)不会命中位于 AS-path 最末端的 AS(实测 `_65099_` 对 `65002 65099` 不命中,`_65099$` 才命中)。因此 r2 把假的 AS 放在中间(`65002 65099 65088`)。实务上若要涵盖末端,写成 `_65099$|_65099_` 或单独再加一条 `_65099$`。

预期结果:20.0.1.0/24 消失:

```
docker exec lab-pe vtysh -c 'show bgp ipv4 unicast'
   Network          Next Hop            Metric LocPrf Weight Path
*> 10.1.0.0/24      172.30.1.2               0             0 65001 i
...
*> 20.0.0.0/24      172.30.2.2               0             0 65002 i
*  30.0.0.0/24      172.30.3.2               0             0 65003 i
*>                  172.30.2.2               0             0 65002 i
*> 40.0.0.0/24      172.30.3.2               0             0 65003 i
*> 192.0.2.0/24     172.30.1.2               0             0 65001 i

docker exec lab-pe vtysh -c 'show route-map FROM-R2'
...
BGP:
route-map: FROM-R2 Invoked: 6 Optimization: enabled Processed Change: false
 deny, sequence 10 Invoked 2
  Match clauses:
    as-path HAS-65099
...
 permit, sequence 20 Invoked 4
```

`show route-map NAME` 的 `Invoked N` 是各 sequence 命中次数,是检查「policy 到底有没有被走到」的好工具。这里 deny 命中 2 次(20.0.1.0/24 被挡),permit 20 命中 4 次。

## 5. 最佳路径选择与 set 动作

BGP 选路顺序(节录,由高到低):**weight(Cisco/FRR 本机专属,大者优先)> local-preference(大者优先)> 本机起源 > AS-path 短者 > origin > MED(小者优先,仅比较同一邻居 AS)> eBGP 优于 iBGP > IGP metric > router-id...** policy 可以改写前面几项,因此能操控选路。

`30.0.0.0/24` 同时从 r2、r3 收到,是观察选路的最佳素材。没有 policy 时两条 path 属性相同,由后面的决胜规则决定(与收到的先后、router-id 有关)。

### 5.1 local-preference(情境 s03)

```
route-map FROM-R3 permit 10
 set local-preference 200
...
  neighbor 172.30.3.2 route-map FROM-R3 in
```

```
docker exec lab-pe vtysh -c 'show bgp ipv4 unicast'
   Network          Next Hop            Metric LocPrf Weight Path
...
*> 30.0.0.0/24      172.30.3.2               0    200      0 65003 i
*                   172.30.2.2               0             0 65002 i
*> 40.0.0.0/24      172.30.3.2               0    200      0 65003 i

docker exec lab-pe vtysh -c 'show bgp ipv4 unicast 30.0.0.0/24'
  65003
    172.30.3.2 from 172.30.3.2 (10.255.0.4)
      Origin IGP, metric 0, localpref 200, valid, external, best (Local Pref)
  65002
    172.30.2.2 from 172.30.2.2 (10.255.0.2)
      Origin IGP, metric 0, valid, external
```

- `best (Local Pref)` 说明选路理由。未配置的 eBGP path 在表中 LocPrf 栏为空,实际视为默认值 100。
- local-preference 只在同一个 AS 内传递,在 iBGP 上 r4 也看得到:

```
docker exec lab-r4 vtysh -c 'show bgp ipv4 unicast'
   Network          Next Hop            Metric LocPrf Weight Path
  i10.1.0.0/24      172.30.1.2               0    100      0 65001 i
...
  i30.0.0.0/24      172.30.3.2               0    200      0 65003 i
  i40.0.0.0/24      172.30.3.2               0    200      0 65003 i
```

- 注意 r4 的每一行前面没有 `*>`,而且 `show ip route bgp` 是空的。原因是 iBGP 缺省**不改写 next-hop**,r4 不知道怎幺到 172.30.3.2,路由无法使用。见 5.5。

### 5.2 weight 优先于 local-preference(情境 s04)

```
route-map FROM-R2 permit 10
 set weight 100
route-map FROM-R3 permit 10
 set local-preference 200
```

```
*  30.0.0.0/24      172.30.3.2               0    200      0 65003 i
*>                  172.30.2.2               0           100 65002 i

docker exec lab-pe vtysh -c 'show bgp ipv4 unicast 30.0.0.0/24'
Paths: (2 available, best #2, table default)
  65003
      Origin IGP, metric 0, localpref 200, valid, external
  65002
      Origin IGP, metric 0, weight 100, valid, external, best (Weight)
```

与 s03 相比,最佳路径从 r3 切换到 r2。weight 只在本机有意义,不会传给其他路由器。

### 5.3 MED 与 AS-path prepend(情境 s05,outbound)

outbound policy 影响「邻居怎幺看我」:

```
route-map TO-R2 permit 10
 set metric 50
route-map TO-R3 permit 10
 set as-path prepend 65000 65000
```

```
docker exec lab-r2 vtysh -c 'show bgp ipv4 unicast 10.1.0.0/24'
  65000 65001
    172.30.2.3 from 172.30.2.3 (10.255.0.3)
      Origin IGP, metric 50, valid, external, best (First path received)

docker exec lab-r3 vtysh -c 'show bgp ipv4 unicast'
   Network          Next Hop            Metric LocPrf Weight Path
*> 10.1.0.0/24      172.30.3.3                             0 65000 65000 65000 65001 i
*> 20.0.0.0/24      172.30.3.3                             0 65000 65000 65000 65002 i
```

- r2 看到 MED 50,r3 看到 AS-path 多了两个 65000(送出时 FRR 还会再自动加一个自己的 AS,共三个)。
- prepend 让路径「看起来更长」,是影响**对方**入向流量的常见手段。MED 缺省只在同一邻居 AS 的多条路径间比较。

### 5.4 next-hop

`set ip next-hop <addr>` 可在 route-map 内改写 next-hop(例如 `set ip next-hop peer-address`)。iBGP 场景最常用的是 `neighbor X next-hop-self`。

### 5.5 iBGP next-hop-self(情境 s08)

```
router bgp 65000
 address-family ipv4 unicast
  neighbor 172.30.4.2 next-hop-self
```

```
docker exec lab-r4 vtysh -c 'show bgp ipv4 unicast'
   Network          Next Hop            Metric LocPrf Weight Path
*>i10.1.0.0/24      172.30.4.3               0    100      0 65001 i
...
*>i30.0.0.0/24      172.30.4.3               0    200      0 65003 i
*>i40.0.0.0/24      172.30.4.3               0    200      0 65003 i

docker exec lab-r4 vtysh -c 'show ip route bgp'
B>* 10.1.0.0/24 [200/0] via 172.30.4.3, eth0, weight 1, 00:00:09
B>* 30.0.0.0/24 [200/0] via 172.30.4.3, eth0, weight 1, 00:00:09
```

next-hop 变成 pe 本身(172.30.4.3),路由有了 `*>` 并进入 RIB,local-preference 200 也被保留。

## 6. community / large-community(情境 s06)

community 是挂在路由上的标签,常用来「在入口标记、在出口依标签决定动作」。`scenarios/s06-community.conf`:

```
ip prefix-list GOLD seq 5 permit 10.1.0.0/24
!
bgp community-list standard CL-GOLD permit 65000:100
bgp community-list standard CL-SILVER permit 65000:200
!
route-map FROM-R1 permit 10
 match ip address prefix-list GOLD
 set community 65000:100
 set large-community 65000:1:100
route-map FROM-R1 permit 20
 set community 65000:200
!
route-map TO-R2 permit 10
 match community CL-GOLD
!
route-map TO-R3 permit 10
 match community CL-SILVER
 set community no-export additive
```

- inbound 把 10.1.0.0/24 标成 `65000:100`(GOLD),其他 r1 路由标成 `65000:200`(SILVER)。
- outbound 给 r2 的 route-map 只放行 GOLD,给 r3 的只放行 SILVER,并附加 well-known community `no-export`。
- `set community` 缺省**取代**既有 community,要保留原有的要加 `additive`。
- large-community 格式是 `ASN:值1:值2`(支持 4-byte ASN),用法和 community 一致,对应 `bgp large-community-list`、`match large-community`。
- FRR 缺省就会把 community 传给邻居,不需要 `send-community`。

```
docker exec lab-pe vtysh -c 'show bgp ipv4 unicast 10.1.0.0/24'
  65001
    172.30.1.2 from 172.30.1.2 (10.255.0.1)
      Origin IGP, metric 0, valid, external, best (First path received)
      Community: 65000:100
      Large Community: 65000:1:100

docker exec lab-pe vtysh -c 'show bgp ipv4 unicast community 65000:100'
*> 10.1.0.0/24      172.30.1.2               0             0 65001 i

docker exec lab-pe vtysh -c 'show bgp community-list'
Named Community standard list CL-GOLD
    permit 65000:100
Named Community standard list CL-SILVER
    permit 65000:200
```

r2 只收到 GOLD 的 10.1.0.0/24(r1 其他前缀与 r3 路由都没有 community 65000:100,被 implicit deny):

```
docker exec lab-r2 vtysh -c 'show bgp ipv4 unicast'
   Network          Next Hop            Metric LocPrf Weight Path
*> 10.1.0.0/24      172.30.2.3                             0 65000 65001 i
*> 20.0.0.0/24      0.0.0.0                  0         32768 i
...
```

r3 只收到 SILVER 的路由,且被加上 `no-export`(带这个 community 的路由不会再往别的 eBGP 邻居传,所以 `Not advertised to any peer`):

```
docker exec lab-r3 vtysh -c 'show bgp ipv4 unicast 10.1.1.0/24'
Paths: (1 available, best #1, table default, not advertised to EBGP peer)
  Not advertised to any peer
  65000 65001
    172.30.3.3 from 172.30.3.3 (10.255.0.3)
      Origin IGP, valid, external, best (First path received)
      Community: 65000:200 no-export
```

## 7. continue 与 on-match(情境 s07)

缺省第一个命中的 sequence 就结束。`continue` 让路由在命中并套用 `set` 后,继续往指定 sequence 走:

```
route-map FROM-R1 permit 10
 match ip address prefix-list GOLD
 set community 65000:100
 continue 20
route-map FROM-R1 permit 20
 set local-preference 150
```

```
docker exec lab-pe vtysh -c 'show bgp ipv4 unicast'
*> 10.1.0.0/24      172.30.1.2               0    150      0 65001 i
*> 10.1.1.0/24      172.30.1.2               0    150      0 65001 i

docker exec lab-pe vtysh -c 'show bgp ipv4 unicast 10.1.0.0/24'
      Origin IGP, metric 0, localpref 150, valid, external, best (First path received)
      Community: 65000:100

docker exec lab-pe vtysh -c 'show route-map FROM-R1'
BGP:
route-map: FROM-R1 Invoked: 8 ...
 permit, sequence 10 Invoked 2
  Match clauses:
    ip address prefix-list GOLD
  Set clauses:
    community 65000:100
  Action:
    Goto 20
 permit, sequence 20 Invoked 8
  Set clauses:
    local-preference 150
```

- 10.1.0.0/24 同时得到 community(seq 10)与 local-preference 150(seq 20)。
- 10.1.1.0/24 没有命中 seq 10,直接落到 seq 20,只有 local-preference。
- 若拿掉 `continue 20`,10.1.0.0/24 命中 seq 10 后就结束,不会得到 local-preference 150(`Action: Goto 20` 也会变成 `Exit routemap`)。
- `on-match next` 是更旧的写法:命中后继续下一个 sequence;`on-match goto N` 对应 `continue N`。新配置建议用 `continue`。

## 8. 常见陷阱

### 8.1 eBGP 没有 policy 时什幺都不收也不送(s00、s09)

见第 1 节。排查:`show bgp summary` 的 `State/PfxRcd` 若显示 `(Policy)` 就是这个原因。

### 8.2 引用了不存在的 route-map(情境 s09)

`neighbor 172.30.1.2 route-map NO-SUCH-MAP in`,但从未定义这个 route-map。配置时 vtysh 会警告,结果在此 FRR 版本是**该方向的路由全部被丢弃**:

```
[load s09-undefined-route-map.conf]
The route-map 'NO-SUCH-MAP' does not exist.

docker exec lab-pe vtysh -c 'show bgp ipv4 unicast summary'
172.30.1.2      4      65001         8         7        0    0    0 00:00:13            0 (Policy)
172.30.2.2      4      65002         9         7        0    0    0 00:00:13     (Policy) (Policy)

docker exec lab-pe vtysh -c 'show route-map'
ZEBRA:
BGP:
```

- r1 的 inbound 显示 `0`(有 route-map 名称但查不到,全部被拒);outbound 因为没有 outbound policy,仍是 `(Policy)`。
- 不同版本、不同厂商对「引用不存在的 route-map」的行为不一致(有的视同全部放行),**不要依赖它**,上线前用 `show route-map` 确认。

### 8.3 implicit deny

route-map、prefix-list、access-list 结尾都有隐含 deny。只想挡一个前缀却忘了放行其余,就会误杀全部。s02 的 `permit 20` 就是用来避免这件事。

### 8.4 access-list 的 permit 不等于放行

as-path / community-list 的 `permit` 只是「命中」,放行或丢弃由 route-map 的 `permit/deny` 决定(第 4 节)。

### 8.5 关闭 ebgp-requires-policy(情境 s10)

`no bgp ebgp-requires-policy` 让没有 policy 的 eBGP 也交换路由(包含不该接收的,例如 192.0.2.0/24 与带有 AS 65099 的路由):

```
docker exec lab-pe vtysh -c 'show bgp ipv4 unicast summary'
Neighbor        V         AS   MsgRcvd   MsgSent   TblVer  InQ OutQ  Up/Down State/PfxRcd   PfxSnt Desc
172.30.1.2      4      65001        11        11        0    0    0 00:00:13            4        8 N/A
172.30.2.2      4      65002        11        11        0    0    0 00:00:13            3        8 N/A
172.30.3.2      4      65003        11        11        0    0    0 00:00:13            2        8 N/A
172.30.4.2      4      65000         7        11        0    0    0 00:00:13            0        8 N/A
```

这适合实验,不建议用在正式环境:这个保护机制正是为了避免你无意间把全部路由收进来或送出去(route leak)。

### 8.6 修改后没生效

- inbound 要 `clear bgp ... soft in`,outbound 要 `soft out`(第 3.2 节)。
- 没开 `soft-reconfiguration inbound` 的邻居看不到 `received-routes`,`soft in` 会改用 route-refresh,要求对端重送。
- policy 名称大小写敏感。

## 9. 调试指令速查

全部都可以写成 `docker exec lab-pe vtysh -c '<指令>'`,也可以 `docker exec -it lab-pe vtysh` 进交互模式。

| 目的 | 指令 |
|------|------|
| 邻居状态与收/送前缀数、`(Policy)` | `show bgp ipv4 unicast summary` |
| BGP 表 | `show bgp ipv4 unicast` |
| 单一前缀所有 path、选路理由、community | `show bgp ipv4 unicast 30.0.0.0/24` |
| 加 `json` 便于脚本处理 | `show bgp ipv4 unicast 30.0.0.0/24 json` |
| policy 套用前的原始路由(需 soft-reconfig) | `show bgp ipv4 unicast neighbors 172.30.1.2 received-routes` |
| policy 套用后送给邻居的路由 | `show bgp ipv4 unicast neighbors 172.30.2.2 advertised-routes` |
| route-map 内容与各 sequence 命中次数 | `show route-map [NAME]` |
| prefix-list / as-path / community-list | `show ip prefix-list NAME`、`show bgp as-path-access-list`、`show bgp community-list` |
| 依条件查找 | `show bgp ipv4 unicast community 65000:100`、`... large-community 65000:1:100`、`... regexp _65002_`、`... prefix-list NAME`、`... route-map NAME`、`... filter-list NAME` |
| 邻居细节(配置的 policy、capability) | `show bgp neighbors 172.30.1.2` |
| 实际进入 RIB 的 BGP 路由 | `show ip route bgp` |
| 目前配置 | `show running-config` |
| 刷新 policy | `clear bgp ipv4 unicast <neighbor> soft in|out` |

## 10. 练习题

先自己做,再用 `show` 指令验证。每题都可以仿照 `scenarios/` 的文档,拷贝一份修改后用「快速开始」的方式套用。

1. 在 s01 把 policy 改成只接受 `10.1.0.0/24`(提示:`permit 10.1.0.0/24`)。再改成接受 `10.1.0.0/16` 内遮罩长度介于 24 到 25 的所有前缀(`ge`、`le`)。
2. 在 s02 改成只接受「由 r2 直接起源」的路由(`^65002$`)。哪些前缀会被接受?20.0.1.0/24 呢?
3. 在 s03 让 pe 优先经由 r2 到 30.0.0.0/24,但对 40.0.0.0/24 仍走 r3(提示:用 prefix-list 在 `FROM-R2` 内以不同 sequence 配置)。
4. 在 s04 把 weight 拿掉,只留 local-preference:r2 设 300、r3 设 200,最佳路径应是谁?`best (...)` 的理由是什幺?
5. 在 s05 对 r3 的 outbound prepend 改成 `set as-path prepend last-as 3`,观察 AS-path 的差异。
6. 在 s06 添加第三种标签,把 192.0.2.0/24 标成 `65000:999`,并在 outbound 给 r2 的 route-map 用 community-list **deny** 这个标签。
7. 在 s07 把 `continue 20` 拿掉再套用,对照 `show route-map` 与 10.1.0.0/24 的 local-preference 有何不同。
8. 让 s08 的 r4 看到 pe 的 next-hop 但不要用 `next-hop-self`,改在 outbound route-map 内 `set ip next-hop 172.30.4.3`。可行吗?
9. 设计一个「只允许客户 r1 的 /24 且 AS-path 长度不超过 1」的 inbound policy(提示:`match as-path` 搭配 `^65001$`)。
10. 让 pe 送给 r3 的路由只包含自己 AS 起源与客户 r1 的路由,不要把 r2 的路由转给 r3(典型的「对等不转送」policy)。

## 11. 环境说明与注意事项

- Image:`frrouting/frr:v8.4.1`(固定 tag,实际拉取并测试)。Docker Hub API 列出的 tag 最新为 `v8.4.1`,尝试拉取 `v9.1.0`、`v9.1.3`、`v8.5.4`、`10.0.1` 等在本机皆失败(not found);`latest` 是更旧的 8.4 版,因此以 `v8.4.1` 为准。其他版本的输出格式可能略有不同。
- 容器不使用 `--network host`,所有 link 都是 `internal: true` 的 docker network(`172.30.1.0/24` ~ `172.30.4.0/24`),不会修改主机路由或 iptables。
- 容器名称:`lab-pe`、`lab-r1` ~ `lab-r4`;compose 项目名称 `frr-bgp-policy-lab`。
- 配置是用 `vtysh -f` 在容器启动后套用的,不使用 `frr.conf` 挂载;切换 pe 的情境时以 `docker restart lab-pe` 清除旧配置。
- 测试以轮询取代固定 sleep:每个断言最多等 40 秒,session 最多等 60 秒。完整跑完约 1.5 分钟(包含重启与等待收敛,实测 1 分 23 秒)。
- 清理:`./clean.sh`(或 `docker compose down -v`)。image 会保留,要移除请手动 `docker rmi frrouting/frr:v8.4.1`。
