# 用 Docker + FRR 學習 BGP Route Policy

這個 lab 用 5 個 FRR 容器組成一個小型 BGP 網路,逐一示範 route-map、prefix-list、as-path access-list、community 等 policy 工具。每個情境有一份完整的 `pe` 設定檔(`scenarios/`)、文件中的預期輸出,以及自動斷言(`test_lab.py`)。

文中的輸出皆複製自實際執行結果(FRR `v8.4.1`,見最後一節的環境說明)。

## 目錄

```
docker-compose.yml     拓樸:pe + r1~r4,固定 IP,所有 link 為 internal network
common/                daemons(啟用 bgpd)、空的 vtysh.conf
routers/r1..r4/        對等節點的固定設定(只負責發送路由,不含實驗 policy)
scenarios/s00..s10     套用在 pe 的各情境設定(每份都是完整設定)
test_lab.py            啟動 -> 逐情境套用 -> 輪詢斷言 -> PASS/FAIL -> 清理
clean.sh               手動清理
```

## 快速開始

需求:docker、docker compose v2、python3。

```bash
./test_lab.py            # 跑全部情境,最後印出 "ALL PASS"(exit 0)或 "FAILED"(exit 1);測完自動清理
./test_lab.py s03 s04    # 只跑指定情境
./test_lab.py --keep     # 測完保留容器,方便自己進去玩
./clean.sh               # 手動清理容器與網路
```

手動操作某個情境(例如 s03):

```bash
docker compose up -d
for r in r1 r2 r3 r4; do docker exec -i lab-$r vtysh -f /dev/stdin < routers/$r/frr.conf; done
docker restart lab-pe && sleep 3                       # 切換情境前重啟 pe,清掉前一個情境的設定
docker exec -i lab-pe vtysh -f /dev/stdin < scenarios/s03-local-pref.conf
docker exec -it lab-pe vtysh                           # 進入互動式 CLI
```

## 拓樸

```
                 AS 65001            AS 65002            AS 65003
                  r1                  r2                  r3
               (客戶端)             (上游 A)            (上游 B)
              172.30.1.2          172.30.2.2          172.30.3.2
                   |                   |                   |
              172.30.1.3          172.30.2.3          172.30.3.3
                   +-------------------+-------------------+
                                       pe  AS 65000  (所有 policy 都套在這裡)
                                   172.30.4.3
                                       |
                                  172.30.4.2
                                       r4  AS 65000 (iBGP)
```

各節點發送的路由(皆為 `ip route ... blackhole` 加 `network`):

| 節點 | 發送的前綴 | 備註 |
|------|-----------|------|
| r1 | `10.1.0.0/24` `10.1.1.0/24` `10.1.2.0/24` `192.0.2.0/24` | 客戶端 |
| r2 | `20.0.0.0/24` `20.0.1.0/24` `30.0.0.0/24` | `20.0.1.0/24` 的 AS-path 被 r2 偽造成 `65002 65099 65088`,用來練習 as-path 過濾 |
| r3 | `30.0.0.0/24` `40.0.0.0/24` | `30.0.0.0/24` 與 r2 重複,用來練習最佳路徑選擇 |
| r4 | 無 | 只用來觀察 iBGP 內屬性的傳遞 |

r1~r3 都設了 `no bgp ebgp-requires-policy`,讓它們單純發送路由;`pe` 保留 FRR 預設行為。

常用觀察指令(都在 `pe` 上):

```bash
docker exec lab-pe vtysh -c 'show bgp ipv4 unicast'
```

---

## 1. 先看沒有 policy 會怎樣(情境 s00)

FRR 預設開啟 `bgp ebgp-requires-policy`:eBGP 鄰居若沒有設定 inbound 與 outbound policy,session 雖然會建立,但**不接收也不發送任何路由**。`scenarios/s00-no-policy.conf` 的 pe 完全沒有 route-map:

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

重點:

- `(Policy)` 出現在 `State/PfxRcd` 與 `PfxSnt`,表示被 `ebgp-requires-policy` 擋住。
- iBGP(172.30.4.2,AS 相同)不受影響,仍顯示 `0`。
- r1 同樣收不到 pe 的任何路由(`show bgp ipv4 unicast` 只有自己發送的 4 條)。
- 解法有兩種:寫 route-map(本 lab 的做法,見情境 s01 起),或 `no bgp ebgp-requires-policy`(情境 s10)。

## 2. route-map 基本結構

```
route-map <NAME> <permit|deny> <sequence>
 match <條件>        ! 可多條;同一個 sequence 內的不同 match 是 AND
 set <動作>
 continue <seq>      ! 命中後不要結束,跳到指定 sequence
 on-match next|goto  ! 與 continue 類似的舊式語法
```

規則:

1. 依 sequence 由小到大逐一比對,**第一個命中的 sequence 決定結果**(permit 就放行並套用 `set`,deny 就丟棄),之後的 sequence 不再看。
2. sequence 沒有任何 `match` 代表「全部命中」。`route-map PERMIT-ALL permit 10`(本 lab 到處使用)就是放行全部。
3. **結尾有 implicit deny**:所有 sequence 都沒命中,路由被丟棄。
4. 同一個 `match` 類型的多個值是 OR(例如 prefix-list 內多行);不同類型之間是 AND。
5. route-map 套用在鄰居的某個方向:`neighbor X route-map NAME in|out`(寫在 `address-family ipv4 unicast` 內)。

## 3. prefix-list 過濾(情境 s01)

`scenarios/s01-prefix-list.conf` 的重點:

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

- prefix-list 同樣由 seq 小到大比對,第一個命中的決定結果,**結尾也有 implicit deny**。
- `10.1.0.0/16 le 24` 代表「落在 10.1.0.0/16 內、遮罩長度 ≤ 24」(`ge`/`le` 控制遮罩長度範圍)。
- `192.0.2.0/24` 沒有任何 prefix-list 條目命中,被 implicit deny 丟棄。

預期結果:r1 的 10.1.2.0/24 與 192.0.2.0/24 都被擋,10.1.0.0/24、10.1.1.0/24 被接受:

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

### 3.1 被過濾的路由去哪了:soft-reconfiguration inbound 與 received-routes

`show ... received-routes` 顯示的是 **inbound policy 套用前**的原始路由,前提是該鄰居開了 `soft-reconfiguration inbound`:

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

觀察:

- 10.1.2.0/24 與 192.0.2.0/24 在這裡看得到(原始資料),但不在 `show bgp ipv4 unicast`(已被 policy 過濾)。
- r1 也把 pe 之前發給它的路由送了回來(AS-path 內含 65000),它們同樣被 `FROM-R1` 擋掉,這是 `6 filtered` 的由來。
- `advertised-routes` 顯示 **outbound policy 套用後**實際送給鄰居的路由:

```
docker exec lab-pe vtysh -c 'show bgp ipv4 unicast neighbors 172.30.2.2 advertised-routes'
   Network          Next Hop            Metric LocPrf Weight Path
*> 10.1.0.0/24      0.0.0.0                                0 65001 i
*> 10.1.1.0/24      0.0.0.0                                0 65001 i
*> 20.0.0.0/24      0.0.0.0                                0 65002 i
...
Total number of prefixes 6
```

### 3.2 修改 policy 後刷新:clear bgp

測試腳本在線上移除 deny 條目,再用 soft clear 重新套用 inbound policy(不會中斷 session):

```bash
docker exec lab-pe vtysh -c 'configure terminal' -c 'no ip prefix-list CUST-IN seq 5'
docker exec lab-pe vtysh -c 'clear bgp ipv4 unicast 172.30.1.2 soft in'
```

```
*> 10.1.0.0/24      172.30.1.2               0             0 65001 i
*> 10.1.1.0/24      172.30.1.2               0             0 65001 i
*> 10.1.2.0/24      172.30.1.2               0             0 65001 i
```

10.1.2.0/24 出現,192.0.2.0/24 仍被擋(不在 10.1.0.0/16 內)。

- `soft in`:用 soft-reconfiguration 存下的原始路由(或向對方發 route-refresh)重新跑 inbound policy。
- `soft out`:重新跑 outbound policy 並重送。
- 不加 `soft` 的 `clear bgp ...` 會重置 session,正式環境盡量避免。

## 4. as-path access-list(情境 s02)

as-path access-list 用正規表示式比對 AS-path。`scenarios/s02-as-path.conf`:

```
bgp as-path access-list HAS-65099 permit _65099_
!
route-map FROM-R2 deny 10
 match as-path HAS-65099
route-map FROM-R2 permit 20
```

要點:

- `_` 代表 AS 的邊界(空白、開頭、結尾),所以 `_65099_` 比對「AS-path 中出現 65099 這個 AS」。常見寫法:`^65002$`(只有 AS 65002 自己)、`^65002_`(來自鄰居 65002 的所有路由)、`_65088$`(由 65088 起源)。
- **這裡的 access-list 是「用來比對」,permit 表示命中。**真正丟棄路由的是 route-map 的 `deny 10`。這是初學者最常搞混的地方。
- `permit 20` 沒有 match,作為「其餘全放行」,避免 implicit deny。
- 本 lab 用的 Alpine/musl 版 FRR 有個小怪癖:正規式「結尾的 `_`」(例如要匹配 path 最後一個 AS 的 `_65099_`)不會命中位於 AS-path 最末端的 AS(實測 `_65099_` 對 `65002 65099` 不命中,`_65099$` 才命中)。因此 r2 把假的 AS 放在中間(`65002 65099 65088`)。實務上若要涵蓋末端,寫成 `_65099$|_65099_` 或單獨再加一條 `_65099$`。

預期結果:20.0.1.0/24 消失:

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

`show route-map NAME` 的 `Invoked N` 是各 sequence 命中次數,是檢查「policy 到底有沒有被走到」的好工具。這裡 deny 命中 2 次(20.0.1.0/24 被擋),permit 20 命中 4 次。

## 5. 最佳路徑選擇與 set 動作

BGP 選路順序(節錄,由高到低):**weight(Cisco/FRR 本機專屬,大者優先)> local-preference(大者優先)> 本機起源 > AS-path 短者 > origin > MED(小者優先,僅比較同一鄰居 AS)> eBGP 優於 iBGP > IGP metric > router-id...** policy 可以改寫前面幾項,因此能操控選路。

`30.0.0.0/24` 同時從 r2、r3 收到,是觀察選路的最佳素材。沒有 policy 時兩條 path 屬性相同,由後面的決勝規則決定(與收到的先後、router-id 有關)。

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

- `best (Local Pref)` 說明選路理由。未設定的 eBGP path 在表中 LocPrf 欄為空,實際視為預設值 100。
- local-preference 只在同一個 AS 內傳遞,在 iBGP 上 r4 也看得到:

```
docker exec lab-r4 vtysh -c 'show bgp ipv4 unicast'
   Network          Next Hop            Metric LocPrf Weight Path
  i10.1.0.0/24      172.30.1.2               0    100      0 65001 i
...
  i30.0.0.0/24      172.30.3.2               0    200      0 65003 i
  i40.0.0.0/24      172.30.3.2               0    200      0 65003 i
```

- 注意 r4 的每一行前面沒有 `*>`,而且 `show ip route bgp` 是空的。原因是 iBGP 預設**不改寫 next-hop**,r4 不知道怎麼到 172.30.3.2,路由無法使用。見 5.5。

### 5.2 weight 優先於 local-preference(情境 s04)

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

與 s03 相比,最佳路徑從 r3 切換到 r2。weight 只在本機有意義,不會傳給其他路由器。

### 5.3 MED 與 AS-path prepend(情境 s05,outbound)

outbound policy 影響「鄰居怎麼看我」:

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

- r2 看到 MED 50,r3 看到 AS-path 多了兩個 65000(送出時 FRR 還會再自動加一個自己的 AS,共三個)。
- prepend 讓路徑「看起來更長」,是影響**對方**入向流量的常見手段。MED 預設只在同一鄰居 AS 的多條路徑間比較。

### 5.4 next-hop

`set ip next-hop <addr>` 可在 route-map 內改寫 next-hop(例如 `set ip next-hop peer-address`)。iBGP 場景最常用的是 `neighbor X next-hop-self`。

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

next-hop 變成 pe 本身(172.30.4.3),路由有了 `*>` 並進入 RIB,local-preference 200 也被保留。

## 6. community / large-community(情境 s06)

community 是掛在路由上的標籤,常用來「在入口標記、在出口依標籤決定動作」。`scenarios/s06-community.conf`:

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

- inbound 把 10.1.0.0/24 標成 `65000:100`(GOLD),其他 r1 路由標成 `65000:200`(SILVER)。
- outbound 給 r2 的 route-map 只放行 GOLD,給 r3 的只放行 SILVER,並附加 well-known community `no-export`。
- `set community` 預設**取代**既有 community,要保留原有的要加 `additive`。
- large-community 格式是 `ASN:值1:值2`(支援 4-byte ASN),用法和 community 一致,對應 `bgp large-community-list`、`match large-community`。
- FRR 預設就會把 community 傳給鄰居,不需要 `send-community`。

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

r2 只收到 GOLD 的 10.1.0.0/24(r1 其他前綴與 r3 路由都沒有 community 65000:100,被 implicit deny):

```
docker exec lab-r2 vtysh -c 'show bgp ipv4 unicast'
   Network          Next Hop            Metric LocPrf Weight Path
*> 10.1.0.0/24      172.30.2.3                             0 65000 65001 i
*> 20.0.0.0/24      0.0.0.0                  0         32768 i
...
```

r3 只收到 SILVER 的路由,且被加上 `no-export`(帶這個 community 的路由不會再往別的 eBGP 鄰居傳,所以 `Not advertised to any peer`):

```
docker exec lab-r3 vtysh -c 'show bgp ipv4 unicast 10.1.1.0/24'
Paths: (1 available, best #1, table default, not advertised to EBGP peer)
  Not advertised to any peer
  65000 65001
    172.30.3.3 from 172.30.3.3 (10.255.0.3)
      Origin IGP, valid, external, best (First path received)
      Community: 65000:200 no-export
```

## 7. continue 與 on-match(情境 s07)

預設第一個命中的 sequence 就結束。`continue` 讓路由在命中並套用 `set` 後,繼續往指定 sequence 走:

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

- 10.1.0.0/24 同時得到 community(seq 10)與 local-preference 150(seq 20)。
- 10.1.1.0/24 沒有命中 seq 10,直接落到 seq 20,只有 local-preference。
- 若拿掉 `continue 20`,10.1.0.0/24 命中 seq 10 後就結束,不會得到 local-preference 150(`Action: Goto 20` 也會變成 `Exit routemap`)。
- `on-match next` 是更舊的寫法:命中後繼續下一個 sequence;`on-match goto N` 對應 `continue N`。新設定建議用 `continue`。

## 8. 常見陷阱

### 8.1 eBGP 沒有 policy 時什麼都不收也不送(s00、s09)

見第 1 節。排查:`show bgp summary` 的 `State/PfxRcd` 若顯示 `(Policy)` 就是這個原因。

### 8.2 引用了不存在的 route-map(情境 s09)

`neighbor 172.30.1.2 route-map NO-SUCH-MAP in`,但從未定義這個 route-map。設定時 vtysh 會警告,結果在此 FRR 版本是**該方向的路由全部被丟棄**:

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

- r1 的 inbound 顯示 `0`(有 route-map 名稱但查不到,全部被拒);outbound 因為沒有 outbound policy,仍是 `(Policy)`。
- 不同版本、不同廠商對「引用不存在的 route-map」的行為不一致(有的視同全部放行),**不要依賴它**,上線前用 `show route-map` 確認。

### 8.3 implicit deny

route-map、prefix-list、access-list 結尾都有隱含 deny。只想擋一個前綴卻忘了放行其餘,就會誤殺全部。s02 的 `permit 20` 就是用來避免這件事。

### 8.4 access-list 的 permit 不等於放行

as-path / community-list 的 `permit` 只是「命中」,放行或丟棄由 route-map 的 `permit/deny` 決定(第 4 節)。

### 8.5 關閉 ebgp-requires-policy(情境 s10)

`no bgp ebgp-requires-policy` 讓沒有 policy 的 eBGP 也交換路由(包含不該接收的,例如 192.0.2.0/24 與帶有 AS 65099 的路由):

```
docker exec lab-pe vtysh -c 'show bgp ipv4 unicast summary'
Neighbor        V         AS   MsgRcvd   MsgSent   TblVer  InQ OutQ  Up/Down State/PfxRcd   PfxSnt Desc
172.30.1.2      4      65001        11        11        0    0    0 00:00:13            4        8 N/A
172.30.2.2      4      65002        11        11        0    0    0 00:00:13            3        8 N/A
172.30.3.2      4      65003        11        11        0    0    0 00:00:13            2        8 N/A
172.30.4.2      4      65000         7        11        0    0    0 00:00:13            0        8 N/A
```

這適合實驗,不建議用在正式環境:這個保護機制正是為了避免你無意間把全部路由收進來或送出去(route leak)。

### 8.6 修改後沒生效

- inbound 要 `clear bgp ... soft in`,outbound 要 `soft out`(第 3.2 節)。
- 沒開 `soft-reconfiguration inbound` 的鄰居看不到 `received-routes`,`soft in` 會改用 route-refresh,要求對端重送。
- policy 名稱大小寫敏感。

## 9. 除錯指令速查

全部都可以寫成 `docker exec lab-pe vtysh -c '<指令>'`,也可以 `docker exec -it lab-pe vtysh` 進互動模式。

| 目的 | 指令 |
|------|------|
| 鄰居狀態與收/送前綴數、`(Policy)` | `show bgp ipv4 unicast summary` |
| BGP 表 | `show bgp ipv4 unicast` |
| 單一前綴所有 path、選路理由、community | `show bgp ipv4 unicast 30.0.0.0/24` |
| 加 `json` 便於腳本處理 | `show bgp ipv4 unicast 30.0.0.0/24 json` |
| policy 套用前的原始路由(需 soft-reconfig) | `show bgp ipv4 unicast neighbors 172.30.1.2 received-routes` |
| policy 套用後送給鄰居的路由 | `show bgp ipv4 unicast neighbors 172.30.2.2 advertised-routes` |
| route-map 內容與各 sequence 命中次數 | `show route-map [NAME]` |
| prefix-list / as-path / community-list | `show ip prefix-list NAME`、`show bgp as-path-access-list`、`show bgp community-list` |
| 依條件查詢 | `show bgp ipv4 unicast community 65000:100`、`... large-community 65000:1:100`、`... regexp _65002_`、`... prefix-list NAME`、`... route-map NAME`、`... filter-list NAME` |
| 鄰居細節(設定的 policy、capability) | `show bgp neighbors 172.30.1.2` |
| 實際進入 RIB 的 BGP 路由 | `show ip route bgp` |
| 目前設定 | `show running-config` |
| 刷新 policy | `clear bgp ipv4 unicast <neighbor> soft in|out` |

## 10. 練習題

先自己做,再用 `show` 指令驗證。每題都可以仿照 `scenarios/` 的檔案,複製一份修改後用「快速開始」的方式套用。

1. 在 s01 把 policy 改成只接受 `10.1.0.0/24`(提示:`permit 10.1.0.0/24`)。再改成接受 `10.1.0.0/16` 內遮罩長度介於 24 到 25 的所有前綴(`ge`、`le`)。
2. 在 s02 改成只接受「由 r2 直接起源」的路由(`^65002$`)。哪些前綴會被接受?20.0.1.0/24 呢?
3. 在 s03 讓 pe 優先經由 r2 到 30.0.0.0/24,但對 40.0.0.0/24 仍走 r3(提示:用 prefix-list 在 `FROM-R2` 內以不同 sequence 設定)。
4. 在 s04 把 weight 拿掉,只留 local-preference:r2 設 300、r3 設 200,最佳路徑應是誰?`best (...)` 的理由是什麼?
5. 在 s05 對 r3 的 outbound prepend 改成 `set as-path prepend last-as 3`,觀察 AS-path 的差異。
6. 在 s06 新增第三種標籤,把 192.0.2.0/24 標成 `65000:999`,並在 outbound 給 r2 的 route-map 用 community-list **deny** 這個標籤。
7. 在 s07 把 `continue 20` 拿掉再套用,對照 `show route-map` 與 10.1.0.0/24 的 local-preference 有何不同。
8. 讓 s08 的 r4 看到 pe 的 next-hop 但不要用 `next-hop-self`,改在 outbound route-map 內 `set ip next-hop 172.30.4.3`。可行嗎?
9. 設計一個「只允許客戶 r1 的 /24 且 AS-path 長度不超過 1」的 inbound policy(提示:`match as-path` 搭配 `^65001$`)。
10. 讓 pe 送給 r3 的路由只包含自己 AS 起源與客戶 r1 的路由,不要把 r2 的路由轉給 r3(典型的「對等不轉送」policy)。

## 11. 環境說明與注意事項

- Image:`frrouting/frr:v8.4.1`(固定 tag,實際拉取並測試)。Docker Hub API 列出的 tag 最新為 `v8.4.1`,嘗試拉取 `v9.1.0`、`v9.1.3`、`v8.5.4`、`10.0.1` 等在本機皆失敗(not found);`latest` 是更舊的 8.4 版,因此以 `v8.4.1` 為準。其他版本的輸出格式可能略有不同。
- 容器不使用 `--network host`,所有 link 都是 `internal: true` 的 docker network(`172.30.1.0/24` ~ `172.30.4.0/24`),不會修改主機路由或 iptables。
- 容器名稱:`lab-pe`、`lab-r1` ~ `lab-r4`;compose 專案名稱 `frr-bgp-policy-lab`。
- 設定是用 `vtysh -f` 在容器啟動後套用的,不使用 `frr.conf` 掛載;切換 pe 的情境時以 `docker restart lab-pe` 清除舊設定。
- 測試以輪詢取代固定 sleep:每個斷言最多等 40 秒,session 最多等 60 秒。完整跑完約 1.5 分鐘(包含重啟與等待收斂,實測 1 分 23 秒)。
- 清理:`./clean.sh`(或 `docker compose down -v`)。image 會保留,要移除請手動 `docker rmi frrouting/frr:v8.4.1`。
