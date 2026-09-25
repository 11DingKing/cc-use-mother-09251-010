# 假期补能保供复盘

面向业务人员的纯服务端复盘系统：把**计划版本、现场事件、容量快照、公众查询、救援记录**按时间关联，
回答"移动充电、排队组织、信息发布究竟改善了哪些时段"，而不是只交一份总量报表。

## 核心能力

- **可复算的指标口径**：指标算法注册在引擎中，口径参数（spec）在版本上冻结；分小时/分天输出，直接看出改善时段。
- **输入指纹**：每条证据按规范化 JSON 计算 SHA-256；每个版本记录输入指纹与结果指纹，任何机器重算结果一致。
- **迟到资料 → 新版本**：资料晚到时新建复盘版本；已签发（及被替代）版本的结果与指纹永久冻结，并可查看差异来源（新增/消失证据、逐桶变化）。
- **可续算**：计算分批落检查点（游标 + 中间态），任务中断或进程重启后 `recover` 自动续到完成，结果与一次算完相同。
- **并发签发安全**：所有读改写走 `BEGIN IMMEDIATE` 事务，并发签发恰有一方成功；旧版本不能在高版本签发后反超。
- **个体信息保护**：救援/查询中的姓名、电话、车牌等标识字段按角色字段级剔除；导出只含聚合结果与证据指纹。
- **跨时区节日窗口**：窗口按事发地时区声明，内部统一 UTC；正确处理 DST 春跳（23 桶）/秋回（25 桶）。
- **证据去重**：同一复盘内同内容（键序无关）只生效一次，重复上报保留痕迹但标记 `duplicate_of`。

## 架构与分层

```
domain/         领域模型：枚举、实体、规范化指纹、时区窗口（无基础设施依赖）
application/    应用服务：用例编排、指标引擎（可注册口径）、端口协议、鉴权脱敏
persistence/    SQLite 仓储适配器（WAL + 立即事务；运行数据不进源码目录）
interfaces/     HTTP JSON API 边界（标准库 http.server）
app.py          组合根；__main__.py 为启动入口
```

时间、标识生成、仓储均通过端口注入，测试可替换为固定时钟与内存库。

## 运行

仅依赖 Python 3.11 标准库。

```bash
python3 -m service_09251_010 --host 127.0.0.1 --port 8080
# 数据库默认取 $HOLIDAY_REVIEW_DB，其次 $XDG_DATA_HOME/holiday-review/review.db，最后 /tmp/holiday-review/review.db
```

鉴权通过请求头：`X-Actor: 张三`，`X-Roles: admin,analyst,signer,read_pii`（角色可缺省）。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/reviews` | 创建复盘（名称、时区、本地节日窗口） |
| GET | `/api/reviews` `/api/reviews/{id}` | 列表/详情 |
| POST | `/api/reviews/{id}/evidence` | 补充证据（五类 kind） |
| GET | `/api/reviews/{id}/evidence` | 证据列表（按角色脱敏；`?include_duplicates=true`） |
| POST | `/api/reviews/{id}/versions` | 冻结口径并创建复盘版本 |
| GET | `/api/reviews/{id}/versions` | 版本列表 |
| POST | `/api/versions/{id}/compute[?batch_size=N]` | 发起/推进计算（可分批续算） |
| POST | `/api/versions/{id}/sign` | 签发（signer/admin） |
| GET | `/api/versions/{id}` `/export` | 版本详情 / 机器可读导出 |
| GET | `/api/reviews/{id}/diff?from=1&to=2` | 版本差异（证据来源 + 逐桶变化） |
| POST | `/api/reviews/{id}/challenges` | 发起复核 |
| POST | `/api/challenges/{id}/resolve` | 处理复核（analyst/admin） |
| POST | `/api/recover` | 重启后续算所有未完成版本（admin） |
| GET | `/api/metrics` | 可用指标口径目录 |

证据 `kind`：`plan_version` / `field_event` / `capacity_snapshot` / `public_query` / `rescue_record`。
内置口径：移动充电派出次数、排队平均时长、排队峰值、桩可用率、公众查询量、查询响应 P95、救援平均到场时长；
可用 `register_metric()` 扩展。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：去重、跨时区与 DST 节日窗口、口径复算确定性、分批续算与重启恢复、
迟到资料版本差异、签发不可变、并发签发/并发去重/并发建版、PII 字段级权限、完整 HTTP 工作流。

## 编译检查

```bash
python3 -m compileall -q service_09251_010 tests
```
