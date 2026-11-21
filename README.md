# 流域归属与观测服务

站点观测按**采样时的流域归属**供数。站点转属、历史更正、历史补录在一笔事务内完成，
提交时统一校验完整性；API 与任意直连 SQL 会话（READ COMMITTED）受同一套数据库约束。

## 架构

```
┌────────────┐   HTTP    ┌─────────────────┐   SQL    ┌──────────────────┐
│ 客户端/curl │ ────────▶ │ FastAPI (uvicorn) │ ──────▶ │ PostgreSQL 16     │
│ 直连 psql   │ ──────────┼──────────────────┼────────▶ │ 约束在库内强制执行 │
└────────────┘           └─────────────────┘          └──────────────────┘
```

- `db/init.sql` —— 全部约束逻辑（表、**增量**延迟约束触发器、站点行锁、供数视图），数据库启动时装载，持久化于系统目录
- `api/` —— FastAPI 应用（变更集 + 转属/拆分/合并/更正便捷端点 + 供数查询）
- `docker-compose.yml` —— `db`(postgres:16-alpine) + `api`，健康检查串联启动顺序
- `examples/` —— 转属、历史更正、直连 SQL、并发冲突 四个示例
- `scripts/demo_small_vs_big.py` —— 小站/大站合法写入与违规回滚的延迟对照演示
- `tests/` —— pytest 集成测试（15 个用例）+ 性能回归测试（5 个用例）

## 数据模型与约束

- `stations(station_id, name)` —— 站点，同时充当**每站一把的锁锚点**
- `attribution_periods(period_id, station_id, watershed, valid_from, valid_to)`
  —— 归属时段，`[valid_from, valid_to)` **左闭右开**，`valid_to` 可空（至今有效），`CHECK (valid_to > valid_from)`
- `observations(observation_id, station_id, observed_at, value)` —— 观测
- `observation_attribution`（视图）—— 供数：观测按 `observed_at ∈ [valid_from, valid_to)` 关联流域

两条完整性规则由 **`DEFERRABLE INITIALLY DEFERRED` 约束触发器在提交时**统一校验：

1. 同一站点任意两个时段不得重叠（`tstzrange … && …`，GiST 索引加速）；
2. 每条观测在采样时刻必须落入本站某一个时段（不重叠 ⇒ 至多一段；覆盖检查 ⇒ 至少一段；合起来**恰好一段**）。

事务**中途允许暂时违规**（先拆/删时段再补、先补观测再改时段均可），**提交时必须全部满足**，
否则整笔回滚。任何写路径（API、变更集、直连 SQL）都走同一套约束。

### 增量提交校验（提交延迟与站点历史行数解耦）

早期实现的提交校验对受影响站点做**整站反连接扫描**，大站（N 条历史观测）每新增一条
合法观测都要付 O(N) 成本，写入延迟随历史线性增长。现改为会话临时表驱动的**增量校验**
（临时表均为 `ON COMMIT DELETE ROWS`，事务结束自动清空）：

- `dirty_observations`：本事务插入/更新的观测（删除不登记——删观测不会制造孤儿）；
- `dirty_periods`：本事务插入/更新的时段 id（删除不登记——删时段不会制造重叠）；
- `dirty_ranges`：本事务插/改/删时段的旧区间与新区间；
- 提交时按站点去重（`pending_station_checks`），每站只做三件事：
  1. 仅脏时段 id 对终态做 GiST 重叠探测；
  2. 仅脏观测逐行做归属点探测（主键 + GiST 索引，常数级 buffer）；
  3. 仅在脏区间内做观测索引范围扫描，抓因时段变更而失去归属的老观测。

完备性依据：终态中的任何重叠对至少含一个本事务脏时段；任何无归属观测要么自身被插/改，
要么其原覆盖时段被插/改/删且采样时刻落在登记的旧/新区间内。临时表无统计信息，
空表也会被优化器估成嵌套循环，因此 2a/2b 先做空表短路（纯观测补录时 `dirty_ranges` 恒空）。
仅当转属/更正/合并触及 `valid_to IS NULL` 的开放时段时，脏区间为 +∞，该站才退化为全量
观测扫描——这是结构性的（开放终点确实可能影响全部历史观测），且不属于高频补录路径。
全量审计函数 `check_station_integrity(station_id)` 保留，供人工运维兜底核查。

### 并发设计（撤段 vs 补录）

- 行级 BEFORE 触发器在每次写时段/观测前对该站点行执行 `SELECT … FOR UPDATE`
  （多站按字典序加锁，降低死锁概率）——**同站并发写被串行化**；
- 在 READ COMMITTED 下，锁等待解除后提交时校验能看到对方**已提交**的最新状态：
  - 撤段先提交 ⇒ 补录提交时发现观测无归属 ⇒ **补录整笔拒绝**；
  - 补录先提交 ⇒ 撤段提交时发现会留下孤儿观测 ⇒ **撤段整笔拒绝**；
  - 两种时序都不会出现"各自通过却留下无归属观测"；
- 不同站点持不同锁行，**无关站互不阻塞、可各自提交**；
- 死锁（多站交叉加锁）由 PG 检测，API 映射为 409，可整笔重试；
- 禁止 `TRUNCATE`（会绕过行触发器）；约束保存在系统目录，**重启后仍有效**。

## 快速开始

```bash
docker compose up -d --build --wait
curl http://localhost:8000/health        # {"status":"ok"}
xdg-open http://localhost:8000/docs      # Swagger UI（或浏览器打开）
```

数据库同时暴露在 `localhost:5432`（用户/密码/库名均为 `watershed`），便于直连 SQL。

## API 一览

| 方法/路径 | 说明 |
|---|---|
| `POST /stations` · `GET /stations` | 建站 / 列表 |
| `GET /stations/{sid}/periods` | 查某站归属时段 |
| `POST /changesets` | **核心写入口**：一笔事务按序执行多个 `insert/update/delete_period`、`insert/update/delete_observation` 操作，提交时统一校验，失败整笔回滚（409） |
| `POST /observations` | 单条补录（等价于单操作变更集） |
| `POST /stations/{sid}/transfer` | 转属：`at` 时刻截断当前时段并开启新流域时段 |
| `POST /periods/{pid}/split` | 在 `at` 处拆分为两段（右段可换流域） |
| `POST /stations/{sid}/merge` | 合并 `at` 处首尾相接的两段 |
| `POST /stations/{sid}/correct` | 历史更正：把子区间改属另一流域（自动切分原时段） |
| `GET /stations/{sid}/observations` · `GET /attributed-observations` | 供数：按采样时刻关联流域（只见已提交状态） |

`update_period` 中 `valid_to` 缺省表示不修改；显式置空（恢复"至今有效"）用 `"clear_valid_to": true`。

## 示例

（示例脚本依赖 `curl` 与 `jq`；SQL 示例也可用本地 `psql "$DATABASE_URL" -f examples/03_direct_sql.sql` 运行）

```bash
bash examples/01_transfer.sh                # 转属：历史观测归属不变, 新观测属新流域
bash examples/02_historical_correction.sh   # 历史更正：子区间改属 + 同事务补录
docker compose exec -T db psql postgresql://watershed:watershed@localhost:5432/watershed \
  -f - < examples/03_direct_sql.sql          # 直连 SQL：中途违规→提交前修复→成功; 提交仍违规→整笔回滚
bash examples/04_concurrent_conflict.sh      # 并发：撤段 vs 补录, 必有一方整笔被拒
```

`04` 的场景 1 依赖正常的锁等待/唤醒语义，请在 Docker/常规 Linux 环境运行
（个别阻断进程间信号的沙箱中，锁等待方无法被唤醒）。

## 测试

```bash
python3 -m venv .venv && .venv/bin/pip install -r tests/requirements.txt
.venv/bin/python -m pytest tests -v          # 需要 compose 栈已启动
# 可选: API_URL / DATABASE_URL 指向其他实例; PG_LOCK_WAIT_OK=1 启用真并发锁等待用例
# 性能用例可调: PERF_HISTORY_ROWS=100000(默认 50000)
bash scripts/selftest.sh                     # 一键: 起栈 + 全部示例 + pytest
bash scripts/restart_check.sh                # 重启 db/api 后约束仍生效(409/SQL ERROR)

# 小站 vs 大站延迟对照(合法补录同量级; 违规补录 409 整笔回滚, 行数不增加)
API_URL=http://localhost:8000 \
DATABASE_URL=postgresql://watershed:watershed@localhost:5432/watershed \
python3 scripts/demo_small_vs_big.py
```

## 验证状态（如实说明）

已在 **PostgreSQL 16.16 + FastAPI（本机 conda 环境，与 compose 相同的 init.sql 与 uvicorn 启动命令）** 上验证：

- ✅ 集成测试 **19 通过、1 按环境跳过**：原 15 用例（14 通过 1 跳过）+ 新增 5 个性能/增量校验用例
  （大站延迟不随历史增长、大站违规补录回滚、观测时刻变更不留孤儿、时段平移不留孤儿、
  2000 行批量变更集按站去重一次检完）；
- ✅ 性能 A/B（每条补录独立提交，同机同数据）：

  | 站点历史行数 | 旧实现（整站扫描）平均 | 新实现（增量校验）平均 |
  |---|---:|---:|
  | 1,000 | 7.0 ms | 4.4 ms |
  | 10,000 | 53.2 ms | 2.5 ms |
  | 50,000 | 268.0 ms | 2.5 ms |
  | 100,000 | 701.7 ms | 2.5 ms |
  | 200,000 | 1269.6 ms | 2.5 ms |

  新实现各档持平（约 2.5 ms），20 万行站提交校验的实际计划只触达 6 个共享 buffer（脏行主键
  + GiST 点探测），与历史行数无关；旧实现同档为 40 万 buffer 的嵌套循环反连接；
- ✅ 10 个增量校验边界用例（直连 SQL）：时刻变更/时段平移/站间转移的违规拒绝与整笔回滚、
  中途违规提交前修复、`SET CONSTRAINTS IMMEDIATE` 后提交仍强检、TRUNCATE 禁止、多站混合
  事务一站违规全笔回滚、开放终点转属后供数正确；
- ✅ 示例 1（转属）、2（历史更正）、3（直连 SQL 三类违规均拒绝）实际运行输出正确；
  `scripts/demo_small_vs_big.py` 经 HTTP API 实测大站 10 万行补录与小站同量级、违规 409 回滚；
- ✅ 约束仍为持久化目录对象（`pg_trigger.tgdeferrable=t, tginitdeferred=t`），
  全新 initdb 集群重放 `init.sql` 后生效；全量审计函数 `check_station_integrity()` 保留可用；
- ✅ `/docs` 与 `/openapi.json` 可访问（HTTP 200）。

未能在本环境验证（本机无 Docker，且沙箱阻断进程间信号导致**锁等待无法被唤醒**）：

- ⚠️ `docker compose up` 的端到端启动未实际执行；compose/Dockerfile 未改动，已做结构与语法校验，
  栈内各组件（同一 `init.sql` 的 PG16、同一 uvicorn 命令）已分别本机验证；
- ⚠️ 真并发下"锁等待→唤醒→提交时被拒"的跨进程路径（`PG_LOCK_WAIT_OK=1` 的用例、
  示例 04 场景 1）在本沙箱跳过/挂起；已验证行锁本身有效（等待方 `pg_stat_activity`
  显示 `wait_event_type=Lock`），其**拒绝逻辑**由顺序化等价用例与边界用例覆盖，
  锁等待/唤醒本身为 PostgreSQL 既有机制；
- ⚠️ `scripts/restart_check.sh` 的进程重启未在本沙箱执行（无法向进程发信号）。

## 设计取舍与限制

- 约束用延迟触发器而非排他约束：覆盖检查需要"观测必须有归属"这类跨表规则，
  且要求事务中途可违规、提交时统一校验；
- 提交校验改为脏数据驱动的增量检查：按站点去重（会话临时表，`ON COMMIT DELETE ROWS`），
  一笔事务改 N 行只对该站检一次；站点行再次被修改时"已检"标记失效（兼容 `SET CONSTRAINTS IMMEDIATE`）；
  临时表无统计信息，空表先短路避免优化器选成嵌套循环全扫；
- 触及开放终点时段（转属/更正/合并，`valid_to IS NULL`）的事务仍会全量扫描该站观测，
  这是语义上不可避免的（开放区间可能覆盖任意历史时刻），属低频维护路径；高频补录路径不受影响；
- 超级用户/表主仍可 `DISABLE TRIGGER` 或 `session_replication_role=replica` 绕过——
  数据库层面无法防止拥有者自毁，生产上应收回应用账号的 DDL 权限；
- 超大批量导入建议走 `/changesets` 单事务提交（每站仅一次增量校验，2000 行实测亚秒级）。
