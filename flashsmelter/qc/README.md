# 化验取样与放行质控台（QC）

把化验室「群里发照片、批次能不能用没人说得清」换成一套有状态机的流程：

```
物料/规格/取样点配置
      │
登记批次 ──按计划自动生成取样点与频次
      │
现场取样（样品号可扫可查）
      │
录入化验单 + 化验单照片 ──逐项按规格自动判定
      │
合格 ───────────────► 审核通过 ─► 合格待放行
不合格 ─► 自动进隔离区（批次物理位置同步改为"不合格隔离区"，放行被锁死）
      │                    │
      ├─ 发起复检（留样重测）┴─ 复检通过并审核 ─► 覆盖原不合格结论 ─► 合格待放行
      └─ 申请让步接收 ─► 质量经理审批 ─► 让步获批待放行（拒绝则继续隔离）
                              │
                          放行：重新校验当前结论，锁定所依据的每份化验单
                              │
                          追溯：批次 → 放行单 → 化验单 → 逐项结果与原始照片
```

## 设计口径（系统强制，无法绕过）

1. **判定不靠人记**：化验单每个检验项按物料规格的上下限自动判定，任一项不合格则整单不合格、批次立即隔离。
2. **审核与判定分离**：化验单提交后为「待审核」，审核通过才参与放行判定；审核驳回的单子作废。
3. **不合格必须卡住**：待取样、检验中、待审核、隔离、复检进行中、让步审批中，任一状态下放行请求都会被拒绝并返回具体卡点；**拒绝动作也写审计流水**。
4. **复检必须留痕**：复检单记录原因/申请人，自动生成归并到原取样点的复检样；复检报告必须挂复检单，审核通过后覆盖原不合格报告并自动关闭复检单。
5. **让步必须审批**：申请—审批两段式，记录原因、处置方式（如限配比）、审批人与意见；未批准不能作为放行依据。
6. **放行即锁单**：放行时逐点重新校验有效结论，并把每个取样点当前有效的化验单写入放行单（`release_reports`）。放行/拒收是终态，单据不可再删改。
7. **全程可追**：每个动作（含被拒绝的）写 `audit_events`，批次档案可按时间线还原；放行单可下钻到化验单及原始照片。

## 启动

```bash
# 演示数据（3 个典型批次：复检合格 / 让步审批中被卡 / 已放行）
python3 -m flashsmelter.qc --root var/qc seed

# 启动网页控制台（默认 127.0.0.1:8090）
python3 -m flashsmelter.qc --root var/qc serve
# 浏览器打开 http://127.0.0.1:8090/qc/
```

也可以用安装后的命令：`flashsmelter-qc serve`。环境变量前缀 `FLASHSMELTER_QC_`（`ROOT`/`HOST`/`PORT`/`MAX_BODY_BYTES`/`MAX_ATTACHMENT_BYTES`）。

数据全部在 `--root` 下：`qc.sqlite3`（WAL，外键开启）与 `attachments/`（化验单照片，原子落盘）。进程重启后状态由库重建。

## 页面

- **批次与放行**：批次看板（按状态计数/筛选）、批次详情（取样点、判定汇总、复检单、让步单、放行单）、登记批次、取样、录化验单（可多选照片）、放行、拒收、追溯档案。
- **化验单**：全部化验单与审核入口，点开可看逐项结果和照片缩略图。
- **复检 / 让步**：复检单列表、让步审批（批准/拒绝）。
- **物料与取样计划**：物料、检验规格（上下限）、取样点（每批点数与频次）。
- **审计流水**：所有动作，含被门控拒绝的尝试及原因。

## JSON API（动作注册表）

所有动作都是 `POST /api/qc/<域>/<动作>`（只读动作也支持 GET），错误码与主平台一致（`validation-error` 400 / `state-transition-rejected` 409 / `not-found` 404 …）。

| 域 | 动作 |
|---|---|
| material | `create` `list` |
| spec | `set`（同名即修改） |
| plan | `set_point`（按物料配置取样点/点数/频次） |
| batch | `create` `list` `get` `reject` `trace` |
| sample | `add_point`（临时加取） `collect` |
| report | `submit`（含 `values` 与 base64 `attachments`） `list` `get` `review` |
| retest | `create` `close` `list` |
| concession | `apply` `decide` `list` |
| release | `create` `list` |
| audit | `list` |
| 附件 | `GET /api/qc/attachments/{id}`（原始照片字节，中文文件名按 RFC 5987 编码） |

示例：

```bash
curl -s -X POST http://127.0.0.1:8090/api/qc/release/create \
  -H 'Content-Type: application/json' \
  -d '{"batch_id":"<id>","actor":"仓库-周涛","destination":"配料仓"}'
```

不满足条件时：

```json
{
  "error": "state-transition-rejected",
  "message": "批次不满足放行条件，已卡住",
  "status": 409,
  "details": {"problems": ["取样点 B 判定不合格，需复检通过或让步审批通过",
                           "让步接收申请还在审批中"]}
}
```

CLI 调试：`python3 -m flashsmelter.qc --root var/qc call batch.list --params-json '{}'`。

## 批次状态

`pending_sampling → pending_results → pending_review → pass → released`，异常分支：

- 任一取样点不合格：`quarantined`（不合格隔离区）
- 发起复检：`pending_retest`；复检通过覆盖原结论后回 `pass`
- 申请让步：`concession_pending`；批准后 `concession_approved`（可让步放行），拒绝回 `quarantined`
- 拒收：`rejected`（终态，进行中的复检/让步自动关闭）

单据编号按天发号：化验单 `LAB-YYYYMMDD-NNNN`、复检 `RT-`、让步 `CN-`、放行 `RL-`。
