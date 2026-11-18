# 深度登记 门店积分到期与退货逆向冲销

规模：中型。使用者：连锁零售会员运营专员。核心规则：消费先用最早到期批次；退款按原消费分配逆向恢复，已过期部分不得复活。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 事件采信 | 执行并复核事件采信 | `POST /api/intake` | `app/modules/intake/rules.py` | `tests/test_intake.py` |
| M2 权益规则 | 执行并复核权益规则 | `POST /api/policy` | `app/modules/policy/rules.py` | `tests/test_policy.py` |
| M3 积分归批 | 执行并复核积分归批 | `POST /api/lots` | `app/modules/lots/rules.py` | `tests/test_lots.py` |
| M4 消费到期 | 执行并复核消费到期 | `POST /api/consumption` | `app/modules/consumption/rules.py` | `tests/test_consumption.py` |
| M5 退货回拨 | 执行并复核退货回拨 | `POST /api/refund` | `app/modules/refund/rules.py` | `tests/test_refund.py` |
| M6 余额对账 | 执行并复核余额对账 | `POST /api/reconcile` | `app/modules/reconcile/rules.py` | `tests/test_reconcile.py` |
| M7 期末归档 | 执行并复核期末归档 | `POST /api/archive` | `app/modules/archive/rules.py` | `tests/test_archive.py` |
| M8 规则迁移 | 执行并发布积分到期规则迁移 | `POST /api/migration`、`POST /api/runs/{run_id}/migration/publish` | `app/modules/migration/rules.py` | `tests/test_migration.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | GB18030权益中文 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_gb18030_real` |
| TRAP-02 | BOM表头 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_bom_real` |
| TRAP-08 | 会员前导零 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_member_leading_zero` |
| TRAP-04 | 重复表头 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_repeated_header` |
| TRAP-05 | 缺列记录 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_column_bad` |
| TRAP-14 | 编号冲突 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_duplicate_conflict` |
| TRAP-18 | 空态 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_empty_three` |
| TRAP-07 | 哨兵 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/policy/rules.py` | `tests/test_policy.py::test_sentinel_real` |
| TRAP-11 | UTC归一 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/policy/rules.py` | `tests/test_policy.py::test_utc_real` |
| TRAP-06 | 全角积分 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/policy/rules.py` | `tests/test_policy.py::test_fullwidth_points` |
| TRAP-23 | 权益半开窗 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/policy/rules.py` | `tests/test_policy.py::test_rule_end_exclusive` |

## 对账规则

原始逻辑输入=采纳行+隔离行+空行+重复表头+同值重复。业务计算按 DATA-DICTIONARY.md 分别核验，不能把上级与下级重复加总。所有隔离保留编号、原值、原始行号和稳定原因码。

## 已知不覆盖

数据来自仓内样例或用户粘贴文件，不连接外部业务生产系统。平台不执行实际资金划转或行政审批。

## 规则迁移（M8）

裁决原则：**保留旧版、只处理切换后的影响，不整体重算历史**。切换前的批次、消费、退款按旧规则版本原样保留；M8 用旧规则把事件重放到切换时点 `migration.cutover_at`（默认 2026-12-01T00:00+08:00），取此时点仍有余额的旧批次作为迁移基准，因此切换前后余额可逐户核对。

新规则目标版本 `migration.target_version`（默认 `2026.12.1-r1`）采用统一有效期 `migration.validity_days`（默认45天）：新到期 = 原发放时刻 + 有效期。逐批次裁决差异去向：

| 去向码 | 条件 | 处理 |
|---|---|---|
| `EARLY_EXPIRY` | 新到期 ≤ 切换时点 | 未保留差异，提前到期作废，不建新版批次，金额计入 `dropped` |
| `RESCHEDULED` | 新到期晚于切换时点且与旧到期不同 | 保留余额，建新版批次并按新到期管理，缩短/延期均记此码 |
| `UNCHANGED` | 新旧到期恰好一致 | 保留余额，建新版批次，去向可追溯 |

守恒裁决（总量与逐会员两层）：`旧版在切余额 = 新版保留 + 未保留差异`。任一不平衡或基线（行守恒、积分守恒、退货核验）不通过，则 `eligible=false`、发布状态 `BLOCKED`，发布接口返回 `MIGRATION_BLOCKED` 并保留旧版余额。新版批次 `id = 原批次号@目标版本`，带 `source_lot`、`source_event`、`issued_at`、`rule_version`，可追溯到版本与原发放事件。

发布幂等：`migration_status` 持久化在 runs 表（旧库自动补列，默认 `未发布`）；重复发布已发布批次直接返回当前状态，不新增批次、不重复计入差异。重跑沿用不可变输入快照，规则未变时批号与新版批次号不变。迁移证据随 `GET /api/export/{run_id}` 写入 `migration.json`。本模块只做版本迁移，不设计新权益。
