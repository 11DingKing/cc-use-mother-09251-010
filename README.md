# 假期补能保供复盘

本项目用于建设面向业务人员的纯服务端系统。代码按领域模型、应用服务、持久化与接口边界组织；时间、标识和外部输入应通过可替换端口接入，以便稳定复现状态变化。运行数据与本地配置不得写入源码目录。

假期结束后，各地需要说明移动充电、排队组织和信息发布究竟改善了哪些时段，而不是只提交一份总量报表。本服务把计划版本、现场事件、容量快照、公众查询和救援记录按时间关联到节日窗口的时间桶上，支持可复算的指标口径、迟到资料驱动的新版本、签发后不可更改的结论与差异来源解释。

## 架构

```
service_09251_010/
├── ports.py                  # 可替换端口：时钟、标识生成器
├── domain/                   # 领域层（纯函数，无外部状态）
│   ├── windows.py            #   节日窗口：IANA 时区 + 本地日界 → UTC 时间桶
│   ├── metrics.py            #   指标口径引擎：count/sum/avg/max/min/percentile/ratio/utilization
│   ├── fingerprint.py        #   输入指纹：窗口 + 口径版本 + 证据清单的 SHA-256
│   ├── models.py             #   复盘、证据、计算运行、版本、复核
│   └── errors.py             #   领域错误 → HTTP 状态映射
├── persistence/sqlite_store.py  # SQLite 持久化：单连接 + 写锁，签发用条件 UPDATE
├── services/
│   ├── review_service.py     # 应用服务：补录去重、计算续算、签发、复核、差异、导出
│   └── auth.py               # API 密钥、角色权限范围、个体信息脱敏
├── interfaces/wsgi_app.py    # 接口边界：标准库 WSGI 路由（无第三方依赖）
└── __main__.py               # python3 -m service_09251_010 serve
```

## 核心语义

- **证据去重**：按 `(复盘, 类型, 来源, 外部编号)` 幂等；重放返回 `duplicates`，同键不同内容返回 `conflicts` 且不覆盖原记录。
- **跨时区节日窗口**：窗口以 `{"tz": "Asia/Shanghai", "start": "2026-10-01", "end": "2026-10-08"}` 声明（本地日界、结束端排除），证据时刻必须带时区偏移，统一归一到 UTC 切桶；桶标签保留窗口时区的本地时刻，便于直接说明“哪个时段改善”。
- **可复算口径**：指标定义带版本（`name@vN`），引擎是纯函数；每次计算把窗口、口径版本与证据清单（业务键 + 内容哈希，不含内部行号）做成输入指纹，同输入必同指纹。
- **迟到资料 → 新版本**：补录后 `uncomputed_evidence_count` 增加，重新计算产生新版本；输入未变化时计算是空操作。已签发版本永不改变，`/diff` 给出指标分桶变化与证据增减（差异来源）。
- **签发**：条件 UPDATE 保证并发下同一版本只有一个赢家；签发序号在写锁内单调分配。
- **复核**：按版本冻结的清单与口径快照重算，校验证据哈希、逐指标结果与输入指纹，发现篡改即 `mismatch`。
- **续算**：计算按指标分步落库；中断（异常、进程重启）后调用 `resume` 沿用冻结输入续算，已完成步骤不重算。服务启动时自动把遗留 running 运行标记为可续算。
- **权限**：`X-API-Key` 认证；角色 `admin / analyst / issuer / viewer / auditor`。个体信息（救援当事人、公众查询用户标识）仅 `pii:read` 范围可见，其余主体收到 `***` 脱敏结果。

## API 概览（前缀 `/api/v1`）

| 方法 | 路径 | 说明 | 所需范围 |
|---|---|---|---|
| POST | `/metric-definitions` | 定义带版本的指标口径 | `metrics:write` |
| GET | `/metric-definitions` | 列出口径 | `review:read` |
| POST | `/reviews` | 创建复盘（窗口、基线、绑定口径） | `review:write` |
| GET | `/reviews/{id}` | 复盘详情（版本列表、未计算证据数） | `review:read` |
| POST | `/reviews/{id}/metrics` | 追加绑定口径（下一版本生效） | `review:write` |
| POST | `/reviews/{id}/evidence:batch` | 批量补录证据（幂等去重） | `evidence:write` |
| GET | `/reviews/{id}/evidence?kind=` | 列出证据（按范围脱敏） | `review:read` |
| POST | `/reviews/{id}/compute` | 计算新版本（输入未变则空操作） | `compute:run` |
| GET/POST | `/calculation-runs/{run_id}` `/resume` | 查看运行 / 中断续算 | `review:read` / `compute:run` |
| GET | `/reviews/{id}/versions[/{n}]` | 版本列表 / 详情 | `review:read` |
| POST | `/reviews/{id}/versions/{n}/issue` | 签发（并发安全，签发后不可改） | `review:issue` |
| POST | `/reviews/{id}/versions/{n}/recheck` | 发起复核（重算校验指纹） | `recheck:run` |
| GET | `/reviews/{id}/versions/{n}/export` | 导出机器可读 JSON（带指纹与清单哈希） | `export:read` |
| GET | `/reviews/{id}/diff?from=&to=` | 版本差异与差异来源 | `review:read` |

证据类型：`plan_version`（计划版本）、`field_event`（现场事件）、`capacity_snapshot`（容量快照）、`public_query`（公众查询）、`rescue_record`（救援记录）。

## 运行

```bash
python3 -m service_09251_010 serve --host 127.0.0.1 --port 8000 --db /path/review.db --keys-file keys.json
```

- 数据库默认取 `SERVICE_09251_010_DB` 或用户数据目录，绝不写入源码目录。
- `keys.json` 形如 `{"keys": {"<密钥>": {"name": "...", "role": "admin|analyst|issuer|viewer|auditor"}}}`；缺省使用内置开发密钥（仅限本地调试）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：证据去重、跨时区节日窗口（含夏令时 25 小时）、并发签发、中断续算、复核篡改检测、权限脱敏与端到端冒烟。

## 编译检查

```bash
python3 -m compileall -q service_09251_010 tests
```
