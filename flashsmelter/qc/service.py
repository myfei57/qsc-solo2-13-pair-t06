"""取样—判定—隔离—复检/让步—放行的领域状态机。

关键口径（全部在服务层强制，任何入口都绕不过去）：

1. 化验单逐项按规格上下限判定，有一项不合格则整单不合格；
2. 化验单录入后批次立即进入隔离，待报告审核通过才解除；
3. 复检必须针对原样品发起、留样重测，复检报告通过即覆盖原不合格结论；
4. 让步接收必须走申请—审批，未审批通过不能作为放行依据；
5. 放行时重新校验当前有效结论，并把所依据的每份化验单锁进放行单；
6. 被放行引用的化验单/批次不可再删除或改写。
"""

from __future__ import annotations

import base64
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..errors import (
    ConflictError,
    NotFoundError,
    StateTransitionError,
    ValidationError,
)
from ..params import Params
from .settings import QcSettings
from .store import QcStore, row_to_dict, rows_to_dicts, utc_now

ActionHandler = Callable[[Params], Mapping[str, Any]]

# 批次状态
B_PENDING_SAMPLING = "pending_sampling"  # 待取样
B_PENDING_RESULTS = "pending_results"  # 已取样待结果
B_PENDING_REVIEW = "pending_review"  # 有报告待审核
B_QUARANTINED = "quarantined"  # 不合格隔离
B_PENDING_RETEST = "pending_retest"  # 复检中
B_CONCESSION_PENDING = "concession_pending"  # 让步审批中
B_PASS = "pass"  # 合格待放行
B_CONCESSION_APPROVED = "concession_approved"  # 让步获批待放行
B_RELEASED = "released"  # 已放行
B_REJECTED = "rejected"  # 拒收/报废

TERMINAL_BATCH_STATES = {B_RELEASED, B_REJECTED}

STATUS_ZONE = {
    B_PENDING_SAMPLING: "待检区",
    B_PENDING_RESULTS: "待检区",
    B_PENDING_REVIEW: "待检区",
    B_QUARANTINED: "不合格隔离区",
    B_PENDING_RETEST: "复检区",
    B_CONCESSION_PENDING: "不合格隔离区",
    B_PASS: "合格待放区",
    B_CONCESSION_APPROVED: "让步待放区",
    B_RELEASED: "已放行",
    B_REJECTED: "废品区",
}

SAMPLE_PLANNED = "planned"
SAMPLE_TAKEN = "taken"
SAMPLE_TESTING = "testing"
SAMPLE_PASS = "pass"
SAMPLE_FAIL = "fail"

REPORT_PENDING = "pending"
REPORT_APPROVED = "approved"
REPORT_REJECTED = "rejected"  # 审核驳回（记录作废，不参与判定）

RETEST_OPEN = "open"
RETEST_CLOSED = "closed"

CONCESSION_PENDING = "pending"
CONCESSION_APPROVED = "approved"
CONCESSION_REJECTED = "rejected"


def _new_id() -> str:
    return secrets.token_hex(8)


def _decode_base64(data: str) -> bytes:
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception as exc:  # ValueError/binascii.Error
        raise ValidationError("附件内容必须是 base64 编码", details={"field": "content_base64"}) from exc
    if not raw:
        raise ValidationError("附件内容为空", details={"field": "content_base64"})
    return raw


def _result_verdict(value: float | None, minimum: float | None, maximum: float | None) -> str:
    if value is None:
        return REPORT_PENDING
    if minimum is not None and value < minimum:
        return "fail"
    if maximum is not None and value > maximum:
        return "fail"
    return "pass"


class QualityService:
    """QC 动作注册表与状态机。"""

    def __init__(self, settings: QcSettings, store: QcStore | None = None) -> None:
        settings.validate()
        settings.ensure_directories()
        self.settings = settings
        self.store = store or QcStore(settings.db_path)
        self._actions: dict[str, ActionHandler] = {}
        self._register()

    # ============================================================== 注册
    def _register(self) -> None:
        def register(name: str) -> Callable[[ActionHandler], ActionHandler]:
            def decorate(handler: ActionHandler) -> ActionHandler:
                if name in self._actions:
                    raise ValidationError("动作重复注册", details={"action": name})
                self._actions[name] = handler
                return handler

            return decorate

        # ---------- 基础数据
        @register("material.create")
        def _(p: Params) -> Mapping[str, Any]:
            code = p.text("code")
            name = p.text("name")
            unit = p.text("unit", required=False, default="kg")
            if self.store.query_one("SELECT id FROM materials WHERE code = ?", (code,)):
                raise ConflictError("物料编码已存在", details={"code": code})
            mid = _new_id()
            self.store.execute(
                "INSERT INTO materials(id, code, name, unit, created_at) VALUES (?, ?, ?, ?, ?)",
                (mid, code, name, unit, utc_now()),
            )
            return {"material_id": mid, "code": code, "name": name}

        @register("material.list")
        def _(_p: Params) -> Mapping[str, Any]:
            rows = self.store.query_all(
                "SELECT * FROM materials ORDER BY code"
            )
            materials = rows_to_dicts(rows)
            for material in materials:
                material["specs"] = self._specs(material["id"])
                material["sampling_plan"] = self._plan_points(material["id"])
            return {"materials": materials}

        @register("spec.set")
        def _(p: Params) -> Mapping[str, Any]:
            material = self._require_material(p.text("material_id"))
            name = p.text("name")
            method = p.text("method", required=False, default="")
            unit = p.text("unit", required=False, default="")
            minimum = p.optional_number("min_value")
            maximum = p.optional_number("max_value")
            seq = p.integer("seq", required=False, default=0)
            if minimum is None and maximum is None:
                raise ValidationError("检验项至少要给一个上限或下限", details={"spec": name})
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ValidationError("检验项下限不能大于上限", details={"spec": name})
            existing = self.store.query_one(
                "SELECT id FROM test_specs WHERE material_id = ? AND name = ?",
                (material["id"], name),
            )
            if existing:
                self.store.execute(
                    "UPDATE test_specs SET method=?, min_value=?, max_value=?, unit=?, seq=?"
                    " WHERE id=?",
                    (method, minimum, maximum, unit, seq, existing["id"]),
                )
                spec_id = existing["id"]
            else:
                spec_id = _new_id()
                self.store.execute(
                    "INSERT INTO test_specs(id, material_id, name, method, min_value, max_value, unit, seq)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (spec_id, material["id"], name, method, minimum, maximum, unit, seq),
                )
            return {"spec_id": spec_id, "material_code": material["code"], "name": name}

        @register("plan.set_point")
        def _(p: Params) -> Mapping[str, Any]:
            material = self._require_material(p.text("material_id"))
            point_code = p.text("point_code")
            point_name = p.text("point_name")
            quantity = p.integer("quantity", minimum=1, maximum=100)
            frequency = p.text("frequency", required=False, default="每批")
            seq = p.integer("seq", required=False, default=0)
            existing = self.store.query_one(
                "SELECT id FROM sampling_plans WHERE material_id = ? AND point_code = ?",
                (material["id"], point_code),
            )
            if existing:
                self.store.execute(
                    "UPDATE sampling_plans SET point_name=?, quantity=?, frequency=?, seq=?"
                    " WHERE id=?",
                    (point_name, quantity, frequency, seq, existing["id"]),
                )
                point_id = existing["id"]
            else:
                point_id = _new_id()
                self.store.execute(
                    "INSERT INTO sampling_plans(id, material_id, point_code, point_name, quantity, frequency, seq)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (point_id, material["id"], point_code, point_name, quantity, frequency, seq),
                )
            return {"point_id": point_id, "point_code": point_code, "quantity": quantity}

        # ---------- 批次
        @register("batch.create")
        def _(p: Params) -> Mapping[str, Any]:
            material = self._require_material(p.text("material_id"))
            batch_no = p.text("batch_no")
            if self.store.query_one("SELECT id FROM batches WHERE batch_no = ?", (batch_no,)):
                raise ConflictError("批次号已存在", details={"batch_no": batch_no})
            points = self._plan_points(material["id"])
            if not points:
                raise ValidationError(
                    "该物料尚未配置取样点，无法安排取样",
                    details={"material_code": material["code"]},
                )
            supplier = p.text("supplier", required=False, default="")
            quantity = p.optional_number("quantity", minimum=0.0)
            note = p.text("note", required=False, default="", max_length=500)
            now = utc_now()
            bid = _new_id()
            self.store.execute(
                "INSERT INTO batches(id, batch_no, material_id, supplier, quantity, received_at,"
                " location, status, note, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    bid, batch_no, material["id"], supplier, quantity, now,
                    STATUS_ZONE[B_PENDING_SAMPLING], B_PENDING_SAMPLING, note, now, now,
                ),
            )
            sample_count = 0
            for point in points:
                for n in range(int(point["quantity"])):
                    sample_count += 1
                    code = point["point_code"] if point["quantity"] == 1 else f"{point['point_code']}-{n + 1}"
                    name = point["point_name"] if point["quantity"] == 1 else f"{point['point_name']}第{n + 1}点"
                    self._insert_sample(
                        bid, code, name, int(point["seq"]) * 100 + n,
                        required=1, planned_at=now,
                    )
            return {"batch_id": bid, "batch_no": batch_no, "samples_planned": sample_count}

        @register("batch.list")
        def _(p: Params) -> Mapping[str, Any]:
            status = p.optional_text("status")
            sql = (
                "SELECT b.*, m.code AS material_code, m.name AS material_name, m.unit AS unit"
                " FROM batches b JOIN materials m ON m.id = b.material_id"
            )
            params: Sequence[Any] = ()
            if status:
                sql += " WHERE b.status = ?"
                params = (status,)
            sql += " ORDER BY b.received_at DESC, b.batch_no DESC LIMIT 500"
            batches = rows_to_dicts(self.store.query_all(sql, params))
            return {"count": len(batches), "batches": batches}

        @register("batch.get")
        def _(p: Params) -> Mapping[str, Any]:
            return self._batch_view(p.text("batch_id"))

        @register("batch.reject")
        def _(p: Params) -> Mapping[str, Any]:
            batch = self._require_batch(p.text("batch_id"))
            self._require_active(batch)
            reason = p.text("reason")
            actor = p.text("actor", required=False, default="qc")
            note = p.text("note", required=False, default="", max_length=500)
            self._set_batch_status(batch["id"], B_REJECTED, note or reason)
            self.store.execute(
                "UPDATE batches SET rejected_flag = 1 WHERE id = ?", (batch["id"],)
            )
            self.store.execute(
                "UPDATE retests SET status = ?, note = COALESCE(NULLIF(note, ''), ?) WHERE batch_id = ? AND status = ?",
                (RETEST_CLOSED, f"批次拒收：{reason}", batch["id"], RETEST_OPEN),
            )
            self.store.execute(
                "UPDATE concessions SET status = ?, decision_note = COALESCE(NULLIF(decision_note, ''), ?),"
                " decided_by = ?, decided_at = ? WHERE batch_id = ? AND status = ?",
                (CONCESSION_REJECTED, f"批次拒收：{reason}", actor, utc_now(), batch["id"], CONCESSION_PENDING),
            )
            return {"batch_id": batch["id"], "status": B_REJECTED}

        # ---------- 取样
        @register("sample.add_point")
        def _(p: Params) -> Mapping[str, Any]:
            batch = self._require_batch(p.text("batch_id"))
            self._require_active(batch)
            point_code = p.text("point_code")
            point_name = p.text("point_name", required=False, default=point_code)
            now = utc_now()
            sid = self._insert_sample(batch["id"], point_code, point_name, 900, required=0, planned_at=now)
            self._recompute_batch(batch["id"])
            return {"sample_id": sid, "point_code": point_code}

        @register("sample.collect")
        def _(p: Params) -> Mapping[str, Any]:
            sample = self._require_sample(p.text("sample_id"))
            batch = self._require_batch(sample["batch_id"])
            self._require_active(batch)
            if sample["status"] not in (SAMPLE_PLANNED,):
                raise StateTransitionError(
                    "该样品已经取样", details={"sample_no": sample["sample_no"], "status": sample["status"]}
                )
            actor = p.text("actor", required=False, default="sampler")
            now = utc_now()
            self.store.execute(
                "UPDATE samples SET status = ?, taken_at = ?, taken_by = ? WHERE id = ?",
                (SAMPLE_TAKEN, now, actor, sample["id"]),
            )
            self._recompute_batch(batch["id"])
            return {"sample_id": sample["id"], "status": SAMPLE_TAKEN}

        # ---------- 化验单
        @register("report.submit")
        def _(p: Params) -> Mapping[str, Any]:
            sample = self._require_sample(p.text("sample_id"))
            batch = self._require_batch(sample["batch_id"])
            self._require_active(batch)
            if sample["status"] == SAMPLE_PLANNED:
                raise StateTransitionError(
                    "样品尚未取样，不能录入化验单", details={"sample_no": sample["sample_no"]}
                )
            kind = p.text("kind", required=False, default="initial")
            if kind not in ("initial", "retest"):
                raise ValidationError("化验单类型只能是 initial 或 retest", details={"kind": kind})
            retest_id = p.optional_text("source_retest_id")
            if kind == "retest":
                if not retest_id:
                    raise ValidationError("复检化验单必须带来源复检单", details={"field": "source_retest_id"})
                retest = self._require_retest(retest_id)
                if retest["batch_id"] != batch["id"] or retest["new_sample_id"] != sample["id"]:
                    raise ValidationError("复检化验单与复检单/样品不匹配")
                if retest["status"] != RETEST_OPEN:
                    raise StateTransitionError("该复检单已关闭", details={"retest_no": retest["retest_no"]})
            analyst = p.text("analyst", required=False, default="")
            actor = p.text("actor", required=False, default=analyst or "analyst")
            note = p.text("note", required=False, default="", max_length=500)
            values = p.mapping("values", required=True)
            if not values:
                raise ValidationError("化验单必须至少录入一个检验项", details={"field": "values"})

            specs = {row["name"]: row for row in self.store.query_all(
                "SELECT * FROM test_specs WHERE material_id = ?", (batch["material_id"],)
            )}
            missing = sorted(set(specs) - set(values))
            if missing:
                raise ValidationError("检验项结果未录全", details={"missing": missing})
            unknown = sorted(set(values) - set(specs))
            if unknown:
                raise ValidationError("存在规格外的检验项", details={"unknown": unknown})

            rid = _new_id()
            report_no = self._next_doc_no("LAB")
            now = utc_now()
            overall = "pass"
            result_rows: list[tuple[Any, ...]] = []
            for name in sorted(specs):
                spec = specs[name]
                raw = values[name]
                try:
                    value = float(raw)
                except (TypeError, ValueError) as exc:
                    raise ValidationError(
                        f"检验项 {name} 的结果必须是数值", details={"value": repr(raw)}
                    ) from exc
                if value != value:
                    raise ValidationError(f"检验项 {name} 不能是 NaN")
                verdict = _result_verdict(value, spec["min_value"], spec["max_value"])
                if verdict == "fail":
                    overall = "fail"
                result_rows.append(
                    (
                        _new_id(), rid, name, spec["method"], value, spec["min_value"],
                        spec["max_value"], spec["unit"], verdict,
                    )
                )
            self.store.execute(
                "INSERT INTO reports(id, report_no, sample_id, batch_id, kind, source_retest_id,"
                " overall_verdict, status, analyst, analyzed_at, submitted_at, submitted_by, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rid, report_no, sample["id"], batch["id"], kind, retest_id,
                    overall, REPORT_PENDING, analyst, now, now, actor, note,
                ),
            )
            self.store.executemany(
                "INSERT INTO report_results(id, report_id, spec_name, method, value, min_value,"
                " max_value, unit, verdict) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                result_rows,
            )
            self.store.execute(
                "UPDATE samples SET status = ? WHERE id = ? AND status IN (?, ?)",
                (SAMPLE_PASS if overall == "pass" else SAMPLE_FAIL, sample["id"], SAMPLE_TAKEN, SAMPLE_TESTING),
            )
            attachments = p.mapping("attachments", required=False)
            stored: list[str] = []
            for key in sorted(attachments):
                item = attachments[key]
                if not isinstance(item, Mapping):
                    raise ValidationError(f"附件 {key} 必须是对象")
                filename = str(item.get("filename") or key)[:200]
                content_type = str(item.get("content_type") or "application/octet-stream")[:120]
                content = _decode_base64(str(item.get("content_base64") or ""))
                if len(content) > self.settings.max_attachment_bytes:
                    raise ValidationError(
                        "附件超过大小上限",
                        details={"filename": filename, "size": len(content),
                                 "max": self.settings.max_attachment_bytes},
                    )
                aid = _new_id()
                target = self.settings.attachment_dir / f"{aid}_{Path(filename).name}"
                self._atomic_write(target, content)
                self.store.execute(
                    "INSERT INTO attachments(id, report_id, filename, content_type, size_bytes, stored_path,"
                    " uploaded_by, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (aid, rid, filename, content_type, len(content), str(target), actor, now),
                )
                stored.append(filename)
            self._recompute_batch(batch["id"])
            return {
                "report_id": rid,
                "report_no": report_no,
                "verdict": overall,
                "status": REPORT_PENDING,
                "attachments": stored,
            }

        @register("report.list")
        def _(p: Params) -> Mapping[str, Any]:
            sql = (
                "SELECT r.*, s.sample_no, s.point_code, s.point_name, b.batch_no, m.code AS material_code"
                " FROM reports r"
                " JOIN samples s ON s.id = r.sample_id"
                " JOIN batches b ON b.id = r.batch_id"
                " JOIN materials m ON m.id = b.material_id"
            )
            params: list[Any] = []
            where: list[str] = []
            batch_id = p.optional_text("batch_id")
            if batch_id:
                where.append("r.batch_id = ?")
                params.append(batch_id)
            status = p.optional_text("status")
            if status:
                where.append("r.status = ?")
                params.append(status)
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY r.submitted_at DESC, r.report_no DESC LIMIT 500"
            return {"reports": rows_to_dicts(self.store.query_all(sql, params))}

        @register("report.get")
        def _(p: Params) -> Mapping[str, Any]:
            return self._report_view(p.text("report_id"))

        @register("report.review")
        def _(p: Params) -> Mapping[str, Any]:
            report = self._require_report(p.text("report_id"))
            batch = self._require_batch(report["batch_id"])
            self._require_active(batch)
            if report["status"] != REPORT_PENDING:
                raise StateTransitionError(
                    "化验单已审核过", details={"report_no": report["report_no"], "status": report["status"]}
                )
            decision = p.text("decision")
            if decision not in (REPORT_APPROVED, REPORT_REJECTED):
                raise ValidationError("审核结论只能是 approved 或 rejected", details={"decision": decision})
            actor = p.text("actor", required=False, default="qc-reviewer")
            note = p.text("note", required=False, default="", max_length=500)
            self.store.execute(
                "UPDATE reports SET status = ?, reviewed_at = ?, reviewed_by = ?, review_note = ? WHERE id = ?",
                (decision, utc_now(), actor, note, report["id"]),
            )
            if decision == REPORT_REJECTED:
                self.store.execute(
                    "UPDATE report_results SET verdict = ? WHERE report_id = ?",
                    (REPORT_REJECTED, report["id"]),
                )
            elif report["kind"] == "retest" and report["overall_verdict"] == "pass":
                # 复检通过：覆盖原样品上的不合格报告，并自动关闭复检单。
                retest = self.store.query_one(
                    "SELECT * FROM retests WHERE id = ?", (report["source_retest_id"],)
                )
                if retest is not None:
                    self.store.execute(
                        "UPDATE reports SET superseded_by = ?"
                        " WHERE sample_id = ? AND status = ? AND overall_verdict = 'fail'"
                        " AND superseded_by IS NULL",
                        (report["id"], retest["sample_id"], REPORT_APPROVED),
                    )
                    self.store.execute(
                        "UPDATE retests SET status = ?, closed_at = ? WHERE id = ? AND status = ?",
                        (RETEST_CLOSED, utc_now(), retest["id"], RETEST_OPEN),
                    )
            self._recompute_batch(batch["id"])
            return {"report_id": report["id"], "status": decision}

        # ---------- 复检
        @register("retest.create")
        def _(p: Params) -> Mapping[str, Any]:
            report = self._require_report(p.text("report_id"))
            if report["overall_verdict"] != "fail":
                raise ValidationError(
                    "只有不合格化验单可以发起复检",
                    details={"report_no": report["report_no"], "verdict": report["overall_verdict"]},
                )
            batch = self._require_batch(report["batch_id"])
            self._require_active(batch)
            sample = self._require_sample(report["sample_id"])
            reason = p.text("reason")
            actor = p.text("actor", required=False, default="qc")
            now = utc_now()
            retest_id = _new_id()
            retest_no = self._next_doc_no("RT")
            self.store.execute(
                "INSERT INTO retests(id, retest_no, batch_id, sample_id, reason, requested_by,"
                " requested_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (retest_id, retest_no, batch["id"], sample["id"], reason, actor, now, RETEST_OPEN),
            )
            new_sample_id = self._insert_sample(
                batch["id"],
                sample["point_code"],
                f"{sample['point_name']}（复检）",
                int(sample["seq"]) + 1,
                required=1,
                planned_at=now,
                origin_sample_id=sample["id"],
            )
            self.store.execute(
                "UPDATE retests SET new_sample_id = ? WHERE id = ?", (new_sample_id, retest_id)
            )
            self._recompute_batch(batch["id"])
            return {
                "retest_id": retest_id,
                "retest_no": retest_no,
                "new_sample_id": new_sample_id,
                "status": RETEST_OPEN,
            }

        @register("retest.close")
        def _(p: Params) -> Mapping[str, Any]:
            retest = self._require_retest(p.text("retest_id"))
            if retest["status"] != RETEST_OPEN:
                raise StateTransitionError("复检单已关闭", details={"retest_no": retest["retest_no"]})
            note = p.text("note", required=False, default="", max_length=500)
            self.store.execute(
                "UPDATE retests SET status = ?, closed_at = ?, note = ? WHERE id = ?",
                (RETEST_CLOSED, utc_now(), note, retest["id"]),
            )
            self._recompute_batch(retest["batch_id"])
            return {"retest_id": retest["id"], "status": RETEST_CLOSED}

        @register("retest.list")
        def _(p: Params) -> Mapping[str, Any]:
            batch_id = p.optional_text("batch_id")
            if batch_id:
                rows = self.store.query_all(
                    "SELECT * FROM retests WHERE batch_id = ? ORDER BY requested_at DESC", (batch_id,)
                )
            else:
                rows = self.store.query_all("SELECT * FROM retests ORDER BY requested_at DESC LIMIT 500")
            return {"retests": rows_to_dicts(rows)}

        # ---------- 让步
        @register("concession.apply")
        def _(p: Params) -> Mapping[str, Any]:
            batch = self._require_batch(p.text("batch_id"))
            self._require_active(batch)
            fails = self._failed_groups(batch["id"])
            if not fails:
                raise ValidationError("批次没有不合格结论，不能申请让步接收")
            reason = p.text("reason")
            disposition = p.text("disposition", required=False, default="让步接收")
            actor = p.text("actor", required=False, default="qc")
            open_row = self.store.query_one(
                "SELECT id FROM concessions WHERE batch_id = ? AND status = ?",
                (batch["id"], CONCESSION_PENDING),
            )
            if open_row:
                raise ConflictError("该批次已有进行中的让步申请")
            cid = _new_id()
            cno = self._next_doc_no("CN")
            self.store.execute(
                "INSERT INTO concessions(id, concession_no, batch_id, reason, disposition, applied_by,"
                " applied_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, cno, batch["id"], reason, disposition, actor, utc_now(), CONCESSION_PENDING),
            )
            self._recompute_batch(batch["id"])
            return {"concession_id": cid, "concession_no": cno, "status": CONCESSION_PENDING}

        @register("concession.decide")
        def _(p: Params) -> Mapping[str, Any]:
            concession = self._require_concession(p.text("concession_id"))
            if concession["status"] != CONCESSION_PENDING:
                raise StateTransitionError(
                    "让步申请已有结论", details={"concession_no": concession["concession_no"]}
                )
            batch = self._require_batch(concession["batch_id"])
            self._require_active(batch)
            decision = p.text("decision")
            if decision not in (CONCESSION_APPROVED, CONCESSION_REJECTED):
                raise ValidationError("审批结论只能是 approved 或 rejected", details={"decision": decision})
            actor = p.text("actor", required=False, default="quality-manager")
            note = p.text("note", required=False, default="", max_length=500)
            self.store.execute(
                "UPDATE concessions SET status = ?, decided_by = ?, decided_at = ?, decision_note = ? WHERE id = ?",
                (decision, actor, utc_now(), note, concession["id"]),
            )
            self._recompute_batch(batch["id"])
            return {"concession_id": concession["id"], "status": decision}

        @register("concession.list")
        def _(p: Params) -> Mapping[str, Any]:
            batch_id = p.optional_text("batch_id")
            if batch_id:
                rows = self.store.query_all(
                    "SELECT * FROM concessions WHERE batch_id = ? ORDER BY applied_at DESC", (batch_id,)
                )
            else:
                rows = self.store.query_all("SELECT * FROM concessions ORDER BY applied_at DESC LIMIT 500")
            return {"concessions": rows_to_dicts(rows)}

        # ---------- 放行
        @register("release.create")
        def _(p: Params) -> Mapping[str, Any]:
            batch = self._require_batch(p.text("batch_id"))
            actor = p.text("actor", required=False, default="warehouse")
            destination = p.text("destination", required=False, default="产线")
            note = p.text("note", required=False, default="", max_length=500)

            release_row = self.store.query_one(
                "SELECT id FROM releases WHERE batch_id = ?", (batch["id"],)
            )
            if release_row:
                raise ConflictError("该批次已放行，不能重复放行", details={"batch_no": batch["batch_no"]})
            if batch["rejected_flag"]:
                raise StateTransitionError("批次已拒收/报废，不能放行")

            groups = self._sample_groups(batch["id"])
            self._assert_release_ready(batch, groups)

            concession_row = self.store.query_one(
                "SELECT * FROM concessions WHERE batch_id = ? AND status = ? ORDER BY decided_at DESC LIMIT 1",
                (batch["id"], CONCESSION_APPROVED),
            )
            basis = "qualified"
            concession_id: str | None = None
            if any(group["verdict"] == "fail" for group in groups.values()):
                if concession_row is None:
                    raise StateTransitionError("批次仍有不合格项且无已批准的让步接收，禁止放行")
                basis = "concession"
                concession_id = concession_row["id"]

            report_ids: list[str] = []
            for code in sorted(groups):
                group = groups[code]
                rid = group.get("effective_report_id")
                if rid and rid not in report_ids:
                    report_ids.append(rid)
            if not report_ids:
                raise StateTransitionError("没有可用的化验单，禁止放行")

            rid_release = _new_id()
            release_no = self._next_doc_no("RL")
            now = utc_now()
            self.store.execute(
                "INSERT INTO releases(id, release_no, batch_id, basis, destination, released_by,"
                " released_at, note, concession_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rid_release, release_no, batch["id"], basis, destination, actor, now, note,
                    concession_id,
                ),
            )
            self.store.executemany(
                "INSERT INTO release_reports(release_id, report_id) VALUES (?, ?)",
                [(rid_release, report_id) for report_id in report_ids],
            )
            self._set_batch_status(batch["id"], B_RELEASED, note)
            return {
                "release_id": rid_release,
                "release_no": release_no,
                "batch_no": batch["batch_no"],
                "basis": basis,
                "report_ids": report_ids,
            }

        @register("release.list")
        def _(p: Params) -> Mapping[str, Any]:
            batch_id = p.optional_text("batch_id")
            if batch_id:
                rows = self.store.query_all(
                    "SELECT * FROM releases WHERE batch_id = ? ORDER BY released_at DESC", (batch_id,)
                )
            else:
                rows = self.store.query_all(
                    "SELECT r.*, b.batch_no, m.code AS material_code"
                    " FROM releases r JOIN batches b ON b.id = r.batch_id"
                    " JOIN materials m ON m.id = b.material_id"
                    " ORDER BY r.released_at DESC LIMIT 500"
                )
            return {"releases": rows_to_dicts(rows)}

        # ---------- 追溯与审计
        @register("batch.trace")
        def _(p: Params) -> Mapping[str, Any]:
            batch = self._require_batch(p.text("batch_id"))
            view = self._batch_view(batch["id"])
            events = self.store.query_all(
                "SELECT seq, at, actor, action, target, outcome, reason, detail_json"
                " FROM audit_events WHERE batch_id = ?"
                " ORDER BY seq ASC",
                (batch["id"],),
            )
            view["timeline"] = [self._audit_dict(row) for row in events]
            return view

        @register("audit.list")
        def _(p: Params) -> Mapping[str, Any]:
            limit = p.integer("limit", required=False, default=50, minimum=1,
                              maximum=self.settings.audit_page_limit)
            action = p.optional_text("action")
            actor = p.optional_text("actor")
            outcome = p.optional_text("outcome")
            sql = "SELECT seq, at, actor, action, target, outcome, reason, detail_json FROM audit_events"
            where: list[str] = []
            params: list[Any] = []
            if action:
                where.append("action = ?")
                params.append(action)
            if actor:
                where.append("actor = ?")
                params.append(actor)
            if outcome:
                where.append("outcome = ?")
                params.append(outcome)
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY seq DESC LIMIT ?"
            params.append(limit)
            rows = self.store.query_all(sql, params)
            return {"events": [self._audit_dict(row) for row in rows]}

    # ============================================================== 对外
    @property
    def actions(self) -> Mapping[str, ActionHandler]:
        return dict(self._actions)

    def describe_actions(self) -> list[Mapping[str, Any]]:
        return [
            {"action": name, "endpoint": "/api/qc/" + name.replace(".", "/")}
            for name in sorted(self._actions)
        ]

    # 只读动作：直接读库，不开写事务、不产生审计噪音；状态跃迁类动作才记审计。
    READ_ONLY_ACTIONS = frozenset(
        {
            "material.list",
            "batch.list",
            "batch.get",
            "batch.trace",
            "report.list",
            "report.get",
            "retest.list",
            "concession.list",
            "release.list",
            "audit.list",
        }
    )

    def invoke(self, action: str, params: Mapping[str, Any] | None = None, *, source: str = "api") -> Mapping[str, Any]:
        handler = self._actions.get(action)
        if handler is None:
            raise ValidationError("未知质控动作", details={"action": action, "known": sorted(self._actions)})
        parsed = params if isinstance(params, Params) else Params(params, source=source)
        if action in self.READ_ONLY_ACTIONS:
            return dict(handler(parsed))
        actor = parsed.optional_text("actor") or "qc"
        target = self._target_for(action, parsed)
        batch_id = self._batch_context(action, parsed)
        self.store.begin()
        try:
            result = handler(parsed)
            self.store.audit(
                actor=actor, action=action, target=target, batch_id=batch_id, outcome="ok",
                detail={"result": dict(result)},
            )
            self.store.commit()
        except Exception as exc:
            self.store.rollback()
            reason = getattr(exc, "message", str(exc))
            code = getattr(exc, "code", "internal-error")
            try:
                self.store.begin()
                self.store.audit(
                    actor=actor, action=action, target=target, batch_id=batch_id, outcome="rejected",
                    reason=f"{code}: {reason}", detail={"params": self._safe_params(parsed)},
                )
                self.store.commit()
            except Exception:  # pragma: no cover - 审计失败不应掩盖原错误
                self.store.rollback()
            raise
        return dict(result)

    def _batch_context(self, action: str, p: Params) -> str:
        direct = p.optional_text("batch_id")
        if direct:
            return direct
        sample_id = p.optional_text("sample_id")
        if sample_id:
            row = self.store.query_one("SELECT batch_id FROM samples WHERE id = ?", (sample_id,))
            if row:
                return str(row["batch_id"])
        report_id = p.optional_text("report_id")
        if report_id:
            row = self.store.query_one("SELECT batch_id FROM reports WHERE id = ?", (report_id,))
            if row:
                return str(row["batch_id"])
        retest_id = p.optional_text("retest_id")
        if retest_id:
            row = self.store.query_one("SELECT batch_id FROM retests WHERE id = ?", (retest_id,))
            if row:
                return str(row["batch_id"])
        concession_id = p.optional_text("concession_id")
        if concession_id:
            row = self.store.query_one("SELECT batch_id FROM concessions WHERE id = ?", (concession_id,))
            if row:
                return str(row["batch_id"])
        material_id = p.optional_text("material_id")
        if material_id and (action.startswith("material.") or action.startswith(("spec.", "plan."))):
            return material_id
        return ""

    # ============================================================== 内部工具
    def _target_for(self, action: str, p: Params) -> str:
        for name in ("batch_id", "report_id", "sample_id", "retest_id", "concession_id", "material_id"):
            value = p.optional_text(name)
            if value:
                return value
        return ""

    @staticmethod
    def _safe_params(p: Params) -> dict[str, Any]:
        raw = dict(p.raw)
        if "attachments" in raw:
            raw["attach"] = "<binary>"
            raw.pop("attachments", None)
        return raw

    @staticmethod
    def _atomic_write(target: Path, content: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(target.parent))
        try:
            with Path(tmp_name).open("wb") as handle:
                handle.write(content)
            shutil.move(tmp_name, target)
        finally:
            if Path(tmp_name).exists():
                Path(tmp_name).unlink(missing_ok=True)

    def _next_doc_no(self, prefix: str) -> str:
        """按天发号（LAB/RT/CN/RL），UPSERT 原子自增，形如 RL-20260919-0007。"""
        day = utc_now()[:10].replace("-", "")
        self.store.execute(
            "INSERT INTO doc_sequences(prefix, day, seq) VALUES (?, ?, 1)"
            " ON CONFLICT(prefix, day) DO UPDATE SET seq = seq + 1",
            (prefix, day),
        )
        row = self.store.query_one(
            "SELECT seq FROM doc_sequences WHERE prefix = ? AND day = ?", (prefix, day)
        )
        return f"{prefix}-{day}-{int(row['seq']):04d}"

    def _require_material(self, mid: str) -> Mapping[str, Any]:
        row = self.store.query_one("SELECT * FROM materials WHERE id = ?", (mid,))
        if row is None:
            raise NotFoundError("物料不存在", details={"material_id": mid})
        return row

    def _require_batch(self, bid: str) -> Mapping[str, Any]:
        row = self.store.query_one("SELECT * FROM batches WHERE id = ?", (bid,))
        if row is None:
            raise NotFoundError("批次不存在", details={"batch_id": bid})
        return row

    def _require_sample(self, sid: str) -> Mapping[str, Any]:
        row = self.store.query_one("SELECT * FROM samples WHERE id = ?", (sid,))
        if row is None:
            raise NotFoundError("样品不存在", details={"sample_id": sid})
        return row

    def _require_report(self, rid: str) -> Mapping[str, Any]:
        row = self.store.query_one("SELECT * FROM reports WHERE id = ?", (rid,))
        if row is None:
            raise NotFoundError("化验单不存在", details={"report_id": rid})
        return row

    def _require_retest(self, rid: str) -> Mapping[str, Any]:
        row = self.store.query_one("SELECT * FROM retests WHERE id = ?", (rid,))
        if row is None:
            raise NotFoundError("复检单不存在", details={"retest_id": rid})
        return row

    def _require_concession(self, cid: str) -> Mapping[str, Any]:
        row = self.store.query_one("SELECT * FROM concessions WHERE id = ?", (cid,))
        if row is None:
            raise NotFoundError("让步申请不存在", details={"concession_id": cid})
        return row

    @staticmethod
    def _require_active(batch: Mapping[str, Any]) -> None:
        if batch["status"] in TERMINAL_BATCH_STATES:
            raise StateTransitionError(
                "批次已终态，不能再操作",
                details={"batch_no": batch["batch_no"], "status": batch["status"]},
            )

    def _specs(self, material_id: str) -> list[dict[str, Any]]:
        return rows_to_dicts(
            self.store.query_all(
                "SELECT * FROM test_specs WHERE material_id = ? ORDER BY seq, name", (material_id,)
            )
        )

    def _plan_points(self, material_id: str) -> list[dict[str, Any]]:
        return rows_to_dicts(
            self.store.query_all(
                "SELECT * FROM sampling_plans WHERE material_id = ? ORDER BY seq, point_code",
                (material_id,),
            )
        )

    def _insert_sample(
        self,
        batch_id: str,
        point_code: str,
        point_name: str,
        seq: int,
        *,
        required: int,
        planned_at: str,
        origin_sample_id: str | None = None,
    ) -> str:
        sid = _new_id()
        sample_no = self._next_sample_no(batch_id)
        self.store.execute(
            "INSERT INTO samples(id, sample_no, batch_id, point_code, point_name, seq,"
            " origin_sample_id, required_flag, status, planned_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid, sample_no, batch_id, point_code, point_name, seq,
                origin_sample_id, required, SAMPLE_PLANNED, planned_at,
            ),
        )
        return sid

    def _next_sample_no(self, batch_id: str) -> str:
        batch = self.store.query_one("SELECT batch_no FROM batches WHERE id = ?", (batch_id,))
        prefix = (batch["batch_no"] if batch else "B")[:16]
        row = self.store.query_one(
            "SELECT COUNT(*) AS c FROM samples WHERE batch_id = ?", (batch_id,)
        )
        return f"S-{prefix}-{int(row['c']) + 1:02d}"

    # ----------------------------------------------------------- 判定引擎
    def _sample_groups(self, batch_id: str) -> dict[str, dict[str, Any]]:
        """按物理取样点聚合样品；复检样品归并到原取样点。

        每个点的有效结论 = 该点最新一张审核通过、未被覆盖的化验单。
        复检通过的报告标记为对原报告的覆盖（superseded_by 反向关系）。
        """
        samples = self.store.query_all(
            "SELECT * FROM samples WHERE batch_id = ? ORDER BY seq, planned_at, id", (batch_id,)
        )
        groups: dict[str, dict[str, Any]] = {}
        for sample in samples:
            origin_id = sample["origin_sample_id"]
            if origin_id:
                origin = self.store.query_one("SELECT point_code FROM samples WHERE id = ?", (origin_id,))
                key = origin["point_code"] if origin else sample["point_code"]
            else:
                key = sample["point_code"]
            group = groups.setdefault(
                key,
                {"point_code": key, "point_name": sample["point_name"].replace("（复检）", ""),
                 "required": bool(sample["required_flag"]) if not origin_id else None,
                 "samples": [], "verdict": "pending", "effective_report_id": None,
                 "has_pending_review": False},
            )
            if not origin_id:
                group["required"] = group["required"] or bool(sample["required_flag"])
            group["samples"].append(sample["id"])

        # 复检通过并审核通过的报告会覆盖原不合格报告（覆盖关系在审核环节落盘）。
        reports = self.store.query_all(
            "SELECT * FROM reports WHERE batch_id = ? ORDER BY submitted_at DESC, id DESC", (batch_id,)
        )
        latest_approved: dict[str, Any] = {}
        for report in reports:
            if report["status"] != REPORT_APPROVED:
                continue
            if report["superseded_by"]:
                continue
            sample = next(s for s in samples if s["id"] == report["sample_id"])
            origin_id = sample["origin_sample_id"]
            key_sample_id = origin_id or sample["id"]
            current = latest_approved.get(key_sample_id)
            if current is None:
                latest_approved[key_sample_id] = report

        pending_review = [r for r in reports if r["status"] == REPORT_PENDING]
        for code, group in groups.items():
            sample_ids = set(group["samples"])
            # 组内任一待审报告 → 判定悬而未决
            group["has_pending_review"] = any(r["sample_id"] in sample_ids for r in pending_review)
            origin_samples = [s for s in samples if s["id"] in sample_ids and not s["origin_sample_id"]]
            effective: Any = None
            for origin in origin_samples:
                report = latest_approved.get(origin["id"])
                if report is not None:
                    effective = report
                    break
            if effective is not None:
                group["verdict"] = effective["overall_verdict"]
                group["effective_report_id"] = effective["id"]
            else:
                statuses = [s["status"] for s in samples if s["id"] in sample_ids]
                if all(status == SAMPLE_PLANNED for status in statuses):
                    group["verdict"] = "awaiting_sampling"
                else:
                    group["verdict"] = "in_testing"
        return groups

    def _failed_groups(self, batch_id: str) -> list[str]:
        groups = self._sample_groups(batch_id)
        return [code for code, group in groups.items() if group["verdict"] == "fail"]

    def _recompute_batch(self, batch_id: str) -> str:
        """根据当前样品/化验单/复检/让步重算批次状态。单一判定口径。"""
        batch = self._require_batch(batch_id)
        if batch["status"] in TERMINAL_BATCH_STATES:
            return str(batch["status"])

        groups = self._sample_groups(batch_id)
        verdicts = {group["verdict"] for group in groups.values()}

        open_concession = self.store.query_one(
            "SELECT id FROM concessions WHERE batch_id = ? AND status = ?",
            (batch_id, CONCESSION_PENDING),
        )
        approved_concession = self.store.query_one(
            "SELECT id FROM concessions WHERE batch_id = ? AND status = ?"
            " ORDER BY decided_at DESC LIMIT 1",
            (batch_id, CONCESSION_APPROVED),
        )
        open_retest = self.store.query_one(
            "SELECT id FROM retests WHERE batch_id = ? AND status = ?",
            (batch_id, RETEST_OPEN),
        )

        if "awaiting_sampling" in verdicts and verdicts <= {"awaiting_sampling"}:
            new_status = B_PENDING_SAMPLING
        elif "fail" in verdicts:
            if open_concession:
                new_status = B_CONCESSION_PENDING
            elif approved_concession:
                new_status = B_CONCESSION_APPROVED
            elif open_retest:
                new_status = B_PENDING_RETEST
            else:
                new_status = B_QUARANTINED
        elif "pending" in verdicts or "in_testing" in verdicts:
            if any(group["has_pending_review"] for group in groups.values()):
                new_status = B_PENDING_REVIEW
            elif open_retest:
                new_status = B_PENDING_RETEST
            else:
                new_status = B_PENDING_RESULTS
        else:  # 全部 pass
            if approved_concession:
                new_status = B_CONCESSION_APPROVED
            else:
                new_status = B_PASS

        self._set_batch_status(batch_id, new_status)
        return new_status

    def _set_batch_status(self, batch_id: str, status: str, note: str = "") -> None:
        self.store.execute(
            "UPDATE batches SET status = ?, location = ?, updated_at = ?"
            + (", note = CASE WHEN ? <> '' THEN ? ELSE note END" if note else "")
            + " WHERE id = ?",
            (status, STATUS_ZONE[status], utc_now(), note, note, batch_id) if note
            else (status, STATUS_ZONE[status], utc_now(), batch_id),
        )

    def _assert_release_ready(self, batch: Mapping[str, Any], groups: Mapping[str, dict[str, Any]]) -> None:
        problems: list[str] = []
        if not groups:
            problems.append("批次下没有取样点")
        approved_concession = self.store.query_one(
            "SELECT id FROM concessions WHERE batch_id = ? AND status = ?"
            " ORDER BY decided_at DESC LIMIT 1",
            (batch["id"], CONCESSION_APPROVED),
        )
        for code, group in groups.items():
            verdict = group["verdict"]
            if verdict in ("awaiting_sampling", "in_testing", "pending"):
                problems.append(f"取样点 {code} 尚未出齐有效化验单")
            elif group["has_pending_review"]:
                problems.append(f"取样点 {code} 存在待审核化验单")
            elif verdict == "fail" and not approved_concession:
                problems.append(f"取样点 {code} 判定不合格，需复检通过或让步审批通过")
        pending_concession = self.store.query_one(
            "SELECT id FROM concessions WHERE batch_id = ? AND status = ?",
            (batch["id"], CONCESSION_PENDING),
        )
        if pending_concession:
            problems.append("让步接收申请还在审批中")
        open_retest = self.store.query_one(
            "SELECT id FROM retests WHERE batch_id = ? AND status = ?",
            (batch["id"], RETEST_OPEN),
        )
        if open_retest:
            problems.append("复检单尚未关闭")
        if problems:
            raise StateTransitionError(
                "批次不满足放行条件，已卡住",
                details={"batch_no": batch["batch_no"], "problems": problems},
            )

    # ----------------------------------------------------------- 视图
    def _batch_view(self, batch_id: str) -> dict[str, Any]:
        row = self.store.query_one(
            "SELECT b.*, m.code AS material_code, m.name AS material_name, m.unit AS unit"
            " FROM batches b JOIN materials m ON m.id = b.material_id WHERE b.id = ?",
            (batch_id,),
        )
        if row is None:
            raise NotFoundError("批次不存在", details={"batch_id": batch_id})
        view = row_to_dict(row)
        assert view is not None
        samples = rows_to_dicts(
            self.store.query_all(
                "SELECT * FROM samples WHERE batch_id = ? ORDER BY seq, planned_at", (batch_id,)
            )
        )
        sample_ids = [s["id"] for s in samples]
        reports: list[dict[str, Any]] = []
        if sample_ids:
            placeholders = ",".join("?" for _ in sample_ids)
            reports = rows_to_dicts(
                self.store.query_all(
                    f"SELECT * FROM reports WHERE sample_id IN ({placeholders}) ORDER BY submitted_at",
                    sample_ids,
                )
            )
        reports_by_id = {r["id"]: r for r in reports}
        for sample in samples:
            sample["reports"] = [
                {
                    "report_id": r["id"],
                    "report_no": r["report_no"],
                    "kind": r["kind"],
                    "verdict": r["overall_verdict"],
                    "status": r["status"],
                }
                for r in reports if r["sample_id"] == sample["id"]
            ]
        view["samples"] = samples
        groups = self._sample_groups(batch_id)
        view["groups"] = [groups[code] for code in sorted(groups)]
        view["retests"] = rows_to_dicts(
            self.store.query_all("SELECT * FROM retests WHERE batch_id = ? ORDER BY requested_at", (batch_id,))
        )
        view["concessions"] = rows_to_dicts(
            self.store.query_all("SELECT * FROM concessions WHERE batch_id = ? ORDER BY applied_at", (batch_id,))
        )
        release_rows = self.store.query_all(
            "SELECT * FROM releases WHERE batch_id = ? ORDER BY released_at", (batch_id,)
        )
        releases: list[dict[str, Any]] = []
        for release in release_rows:
            item = row_to_dict(release)
            assert item is not None
            linked = self.store.query_all(
                "SELECT rr.report_id, r.report_no, r.overall_verdict, r.status, s.point_code"
                " FROM release_reports rr JOIN reports r ON r.id = rr.report_id"
                " JOIN samples s ON s.id = r.sample_id WHERE rr.release_id = ? ORDER BY s.point_code",
                (release["id"],),
            )
            item["reports"] = rows_to_dicts(linked)
            releases.append(item)
        view["releases"] = releases
        view["release_ready"] = self._release_ready_summary(batch_id, groups)
        return view

    def _release_ready_summary(self, batch_id: str, groups: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
        batch = self._require_batch(batch_id)
        try:
            self._assert_release_ready(batch, groups)
            return {"ready": True, "problems": []}
        except StateTransitionError as exc:
            return {"ready": False, "problems": list(exc.details.get("problems", []))}

    def _report_view(self, report_id: str) -> dict[str, Any]:
        row = self.store.query_one(
            "SELECT r.*, s.sample_no, s.point_code, s.point_name, b.batch_no, b.material_id,"
            " m.code AS material_code, m.name AS material_name"
            " FROM reports r JOIN samples s ON s.id = r.sample_id"
            " JOIN batches b ON b.id = r.batch_id JOIN materials m ON m.id = b.material_id"
            " WHERE r.id = ?",
            (report_id,),
        )
        if row is None:
            raise NotFoundError("化验单不存在", details={"report_id": report_id})
        view = row_to_dict(row)
        assert view is not None
        view["results"] = rows_to_dicts(
            self.store.query_all("SELECT * FROM report_results WHERE report_id = ? ORDER BY spec_name", (report_id,))
        )
        view["attachments"] = rows_to_dicts(
            self.store.query_all(
                "SELECT id, filename, content_type, size_bytes, uploaded_by, uploaded_at, report_id"
                " FROM attachments WHERE report_id = ? ORDER BY uploaded_at",
                (report_id,),
            )
        )
        if row["source_retest_id"]:
            retest = self.store.query_one("SELECT * FROM retests WHERE id = ?", (row["source_retest_id"],))
            view["source_retest"] = row_to_dict(retest)
        return view

    @staticmethod
    def _audit_dict(row: Any) -> dict[str, Any]:
        import json as _json

        item = row_to_dict(row)
        assert item is not None
        raw = item.pop("detail_json", "{}")
        try:
            item["detail"] = _json.loads(raw or "{}")
        except ValueError:
            item["detail"] = {}
        return item


def build_service(settings: QcSettings | None = None) -> QualityService:
    settings = settings or QcSettings.from_env()
    return QualityService(settings)


__all__ = ["QualityService", "build_service", "STATUS_ZONE", "TERMINAL_BATCH_STATES"]
