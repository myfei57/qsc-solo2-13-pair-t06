"""化验室取样与批次放行组件。

群里发照片传结果的管法，问题在于「谁先谁后、哪批能用」全靠人记，不合格的料
混进炉子里才回头翻记录。本组件把这条链固化下来：

* 按批次登记取样计划（取样点 × 每点频次），取样先后落盘有据；
* 化验单先落盘再判定，对照物料规格自动给出合格/不合格；
* 任一不合格即冻结批次：冻结状态禁止放行、禁止取新样，已取的样照常录结果；
* 复检开新一轮取样（轮次有上限），让步接收必须留审批人与原因；
* 放行记录串起整链：批次 → 判定所用化验单号 → 化验单内容，随时可回查。

喷吹（``conc.inject``）与转炉入炉（``conv.charge``）通过 ``ReleasePort`` 查
放行状态：已登记的批次未放行就卡住；未登记的批次走原流程，老产线行为不变。
"""

from __future__ import annotations

import re
import threading
from typing import Any, Iterable, Mapping, Sequence

from ..component import Component, ensure_actor
from ..errors import GuardViolation, NotFoundError, StateTransitionError, ValidationError
from ..runtime import RuntimeContext

DISPOSITIONS = ("pending", "held", "retesting", "released", "rejected")

TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "pending": ("held", "released", "rejected"),
    "held": ("retesting", "released", "rejected"),
    "retesting": ("held", "released", "rejected"),
    "released": (),
    "rejected": (),
}

REPORT_STREAM = "qc/reports"
RELEASE_STREAM = "qc/releases"

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_MAX_LABEL_LENGTH = 40


def _require_token(value: str, label: str) -> str:
    """批次号、物料代码、化验单号要进落盘键，必须满足键段规则。"""

    text = (value or "").strip()
    if not _TOKEN_PATTERN.match(text):
        raise ValidationError(
            f"{label}必须以字母或数字开头，只能含字母、数字、-、_、.",
            details={"label": label, "value": repr(value)},
        )
    return text


def _positive_int(value: Any, label: str) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label}必须是正整数", details={"value": repr(value)}) from exc
    if number != number or number < 1 or abs(number - round(number)) > 1e-9:
        raise ValidationError(f"{label}必须是正整数", details={"value": repr(value)})
    return int(round(number))


def _normalize_points(points: str | Sequence[str]) -> list[str]:
    raw: Iterable[Any] = points.split(",") if isinstance(points, str) else points
    cleaned: list[str] = []
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        if len(text) > _MAX_LABEL_LENGTH:
            raise ValidationError("取样点名称超长", details={"point": text, "max": _MAX_LABEL_LENGTH})
        if text in cleaned:
            raise ValidationError("取样点重复", details={"point": text})
        cleaned.append(text)
    if not cleaned:
        raise ValidationError("取样计划至少需要一个取样点")
    return cleaned


def _as_float(value: Any, label: str, *, required: bool) -> float | None:
    if value is None:
        if required:
            raise ValidationError(f"{label}必须给出数值")
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{label}必须是数值", details={"value": repr(value)})
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label}必须是数值", details={"value": repr(value)}) from exc
    if number != number:
        raise ValidationError(f"{label}不能是 NaN")
    return number


def _normalize_limits(raw: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    if not raw:
        raise ValidationError("判定规格至少需要一个判定项")
    limits: dict[str, dict[str, float]] = {}
    for key, bound in raw.items():
        analyte = str(key).strip()
        if not analyte:
            raise ValidationError("判定项名称不能为空")
        if len(analyte) > _MAX_LABEL_LENGTH:
            raise ValidationError("判定项名称超长", details={"analyte": analyte})
        if not isinstance(bound, Mapping):
            raise ValidationError("判定限值必须是 {min,max} 映射", details={"analyte": analyte})
        low = _as_float(bound.get("min"), f"{analyte}.min", required=False)
        high = _as_float(bound.get("max"), f"{analyte}.max", required=False)
        if low is None and high is None:
            raise ValidationError("判定项至少要给出下限或上限", details={"analyte": analyte})
        if low is not None and high is not None and low > high:
            raise ValidationError(
                "判定下限不能大于上限", details={"analyte": analyte, "min": low, "max": high}
            )
        entry: dict[str, float] = {}
        if low is not None:
            entry["min"] = low
        if high is not None:
            entry["max"] = high
        limits[analyte] = entry
    return limits


def _normalize_values(raw: Mapping[str, Any]) -> dict[str, float]:
    if not raw:
        raise ValidationError("化验结果至少要包含一个测定值")
    values: dict[str, float] = {}
    for key, item in raw.items():
        analyte = str(key).strip()
        if not analyte:
            raise ValidationError("测定项名称不能为空")
        values[analyte] = float(_as_float(item, analyte, required=True))
    return values


def _judge(limits: Mapping[str, Mapping[str, float]], values: Mapping[str, float]) -> tuple[str, list[dict[str, Any]]]:
    """对照规格逐项判定；任一缺项或越限即不合格。"""

    deviations: list[dict[str, Any]] = []
    for analyte, bound in limits.items():
        if analyte not in values:
            deviations.append({"analyte": analyte, "reason": "missing", **dict(bound)})
            continue
        value = values[analyte]
        low = bound.get("min")
        high = bound.get("max")
        if low is not None and value < low:
            deviations.append({"analyte": analyte, "reason": "below-min", "value": value, **dict(bound)})
        elif high is not None and value > high:
            deviations.append({"analyte": analyte, "reason": "above-max", "value": value, **dict(bound)})
    return ("fail" if deviations else "pass"), deviations


class QcLab(Component):
    """化验室：批次取样计划、化验单判定与放行冻结。"""

    name = "qc"

    def __init__(self, ctx: RuntimeContext) -> None:
        super().__init__(ctx)
        # 批次状态全部落在批次文档里，组件本身无内存状态；锁只保证「读-改-写」不被并发请求撕裂。
        self._lock = threading.RLock()
        self._refresh_gauges()

    # ------------------------------------------------------------------ 动作
    def set_spec(
        self,
        actor: str,
        *,
        material: str,
        limits: Mapping[str, Any],
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "set_spec",
            f"qc/spec/{material}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            code = _require_token(material, "物料代码")
            normalized = _normalize_limits(limits)
            payload = {
                "material": code,
                "limits": normalized,
                "updated_at": self.clock.timestamp_iso(),
                "actor": actor,
            }
            record = self.store.commit_intent(self.key("spec", code), payload)
            trace.attach(record).note("analytes", sorted(normalized))
            return {
                "material": code,
                "limits": normalized,
                "version": record.version,
                "updated_at": payload["updated_at"],
            }

    def plan(
        self,
        actor: str,
        *,
        batch_id: str,
        material: str,
        points: str | Sequence[str],
        per_point: int = 1,
        quantity_tons: float | None = None,
        source: str | None = None,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self._lock, self.action(
            "plan",
            f"qc/{batch_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            batch_code = _require_token(batch_id, "批次号")
            material_code = _require_token(material, "物料代码")
            point_list = _normalize_points(points)
            per = _positive_int(per_point, "每点频次")
            if self.store.get(self.key("spec", material_code)) is None:
                raise NotFoundError(
                    "物料规格未登记，先维护判定规格再登记批次", details={"material": material_code}
                )
            if self.store.exists(self.key("batch", batch_code)):
                raise GuardViolation("批次已登记，禁止重复建档", details={"batch_id": batch_code})
            now = self.clock.timestamp_iso()
            slots = [
                self._new_slot(point, seq, 1)
                for point in point_list
                for seq in range(1, per + 1)
            ]
            batch = {
                "batch_id": batch_code,
                "material": material_code,
                "source": source or None,
                "quantity_tons": None if quantity_tons is None else round(float(quantity_tons), 3),
                "disposition": "pending",
                "round": 1,
                "plan": {"points": point_list, "per_point": per},
                "slots": slots,
                "report_ids": [],
                "release": None,
                "rejection": None,
                "history": [
                    {"from": None, "to": "pending", "actor": actor, "reason": "登记批次与取样计划", "at": now}
                ],
                "registered_at": now,
                "registered_by": actor,
                "updated_at": now,
            }
            record = self._save_batch(batch)
            self._refresh_gauges()
            trace.attach(record).note("points", point_list).note("samples_planned", len(slots))
            return self._view(batch)

    def collect(
        self,
        actor: str,
        *,
        batch_id: str,
        point: str,
        seq: float | None = None,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self._lock, self.action(
            "collect",
            f"qc/{batch_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            batch = self._require_batch(batch_id)
            if batch["disposition"] not in ("pending", "retesting"):
                raise StateTransitionError(
                    "当前处置状态不允许取样登记（冻结批次须先开复检）",
                    details={"disposition": batch["disposition"]},
                )
            slot = self._find_slot(batch, point, seq, want="pending")
            now = self.clock.timestamp_iso()
            slot["status"] = "collected"
            slot["collected_at"] = now
            slot["collector"] = actor
            batch["updated_at"] = now
            record = self._save_batch(batch)
            trace.attach(record).note("point", slot["point"]).note("seq", slot["seq"]).note(
                "round", slot["round"]
            )
            return self._view(batch)

    def report(
        self,
        actor: str,
        *,
        batch_id: str,
        point: str,
        report_id: str,
        values: Mapping[str, Any],
        seq: float | None = None,
        lab: str | None = None,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self._lock, self.action(
            "report",
            f"qc/{batch_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            batch = self._require_batch(batch_id)
            if batch["disposition"] in ("released", "rejected"):
                raise StateTransitionError(
                    "批次已结案，禁止补录化验结果", details={"disposition": batch["disposition"]}
                )
            report_code = _require_token(report_id, "化验单号")
            if self.store.exists(self.key("report", report_code)):
                raise GuardViolation("化验单号已使用，禁止重复录入", details={"report_id": report_code})
            spec_record = self.store.get(self.key("spec", batch["material"]))
            if spec_record is None:
                raise NotFoundError("物料规格未登记", details={"material": batch["material"]})
            limits = _normalize_limits(spec_record.payload.get("limits", {}))
            measured = _normalize_values(values)
            slot = self._find_slot(batch, point, seq, want="collected")
            verdict, deviations = _judge(limits, measured)
            now = self.clock.timestamp_iso()
            report_payload = {
                "report_id": report_code,
                "batch_id": batch["batch_id"],
                "material": batch["material"],
                "point": slot["point"],
                "seq": slot["seq"],
                "round": slot["round"],
                "values": measured,
                "verdict": verdict,
                "deviations": deviations,
                "spec_version": spec_record.version,
                "lab": lab or actor,
                "actor": actor,
                "judged_at": now,
            }
            # 化验单先落盘、再判定批次：回读不一致就绝不改批次状态。
            self.store.commit_intent(self.key("report", report_code), report_payload)
            entry = self.store.append(REPORT_STREAM, report_payload)
            slot["status"] = "judged"
            slot["report_id"] = report_code
            slot["verdict"] = verdict
            slot["judged_at"] = now
            batch["report_ids"].append(report_code)
            if verdict == "fail" and batch["disposition"] != "held":
                self._move(
                    batch,
                    "held",
                    actor,
                    f"第 {slot['round']} 轮 {slot['point']}#{slot['seq']} 判定不合格，批次冻结",
                )
            batch["updated_at"] = now
            record = self._save_batch(batch)
            self._refresh_gauges()
            trace.attach(record).note("report_id", report_code).note("verdict", verdict).note(
                "report_seq", entry.seq
            )
            return {
                "batch_id": batch["batch_id"],
                "report_id": report_code,
                "point": slot["point"],
                "seq": slot["seq"],
                "round": slot["round"],
                "verdict": verdict,
                "deviations": deviations,
                "disposition": batch["disposition"],
                "spec_version": spec_record.version,
            }

    def release(
        self,
        actor: str,
        *,
        batch_id: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self._lock, self.action(
            "release",
            f"qc/{batch_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            batch = self._require_batch(batch_id)
            if batch["disposition"] in ("released", "rejected"):
                raise StateTransitionError("批次已结案", details={"disposition": batch["disposition"]})
            not_passed = [row for row in self._point_summary(batch) if row["status"] != "pass"]
            if not_passed:
                raise GuardViolation(
                    "尚有取样点未全部合格，不能放行",
                    details={"points": not_passed, "disposition": batch["disposition"]},
                )
            report_ids = [slot["report_id"] for slot in self._latest_slots(batch) if slot["report_id"]]
            now = self.clock.timestamp_iso()
            batch["release"] = {
                "type": "normal",
                "actor": actor,
                "at": now,
                "round": batch["round"],
                "report_ids": report_ids,
            }
            self._move(batch, "released", actor, "全部取样点判定合格，放行")
            batch["updated_at"] = now
            record = self._save_batch(batch)
            entry = self.store.append(
                RELEASE_STREAM,
                {
                    "batch_id": batch["batch_id"],
                    "type": "normal",
                    "actor": actor,
                    "report_ids": report_ids,
                    "at": now,
                },
            )
            self._refresh_gauges()
            trace.attach(record).note("report_ids", report_ids).note("release_seq", entry.seq)
            return self._view(batch)

    def retest(
        self,
        actor: str,
        *,
        batch_id: str,
        points: str | Sequence[str] | None = None,
        per_point: float | None = None,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self._lock, self.action(
            "retest",
            f"qc/{batch_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            batch = self._require_batch(batch_id)
            if batch["disposition"] != "held":
                raise StateTransitionError(
                    "仅冻结批次可以开复检", details={"disposition": batch["disposition"]}
                )
            max_rounds = int(self.settings.qc_max_rounds)
            if batch["round"] >= max_rounds:
                raise GuardViolation(
                    "复检轮次已达上限，请走让步接收或拒收",
                    details={"round": batch["round"], "max_rounds": max_rounds},
                )
            failed_points = [row["point"] for row in self._point_summary(batch) if row["status"] == "fail"]
            targets = _normalize_points(points) if points else failed_points
            if not targets:
                raise GuardViolation("没有需要复检的取样点")
            unknown = [point for point in targets if point not in batch["plan"]["points"]]
            if unknown:
                raise GuardViolation(
                    "复检取样点不在原计划中",
                    details={"unknown": unknown, "planned": list(batch["plan"]["points"])},
                )
            per = _positive_int(per_point, "每点频次") if per_point is not None else int(batch["plan"]["per_point"])
            round_no = batch["round"] + 1
            for point in targets:
                base = sum(1 for slot in batch["slots"] if slot["point"] == point)
                for offset in range(1, per + 1):
                    batch["slots"].append(self._new_slot(point, base + offset, round_no))
            batch["round"] = round_no
            self._move(batch, "retesting", actor, f"开第 {round_no} 轮复检：{'、'.join(targets)}")
            batch["updated_at"] = self.clock.timestamp_iso()
            record = self._save_batch(batch)
            trace.attach(record).note("round", round_no).note("points", targets)
            return self._view(batch)

    def concession(
        self,
        actor: str,
        *,
        batch_id: str,
        approver: str,
        reason: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self._lock, self.action(
            "concession",
            f"qc/{batch_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            batch = self._require_batch(batch_id)
            if batch["disposition"] != "held":
                raise StateTransitionError(
                    "仅冻结批次可以让步接收", details={"disposition": batch["disposition"]}
                )
            approver_text = (approver or "").strip()
            reason_text = (reason or "").strip()
            if not approver_text:
                raise ValidationError("让步接收必须给出审批人")
            if not reason_text:
                raise ValidationError("让步接收必须给出原因")
            deviations = self._current_deviations(batch)
            if not deviations:
                raise GuardViolation("批次没有不合格项，无需让步，请直接放行")
            report_ids = [slot["report_id"] for slot in self._latest_slots(batch) if slot["report_id"]]
            now = self.clock.timestamp_iso()
            batch["release"] = {
                "type": "concession",
                "actor": actor,
                "approver": approver_text,
                "reason": reason_text,
                "at": now,
                "round": batch["round"],
                "report_ids": report_ids,
                "deviations": deviations,
            }
            self._move(batch, "released", actor, f"让步接收：{reason_text}")
            batch["updated_at"] = now
            record = self._save_batch(batch)
            entry = self.store.append(
                RELEASE_STREAM,
                {
                    "batch_id": batch["batch_id"],
                    "type": "concession",
                    "actor": actor,
                    "approver": approver_text,
                    "reason": reason_text,
                    "deviations": deviations,
                    "report_ids": report_ids,
                    "at": now,
                },
            )
            self._refresh_gauges()
            trace.attach(record).note("approver", approver_text).note("release_seq", entry.seq)
            return self._view(batch)

    def reject(
        self,
        actor: str,
        *,
        batch_id: str,
        reason: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self._lock, self.action(
            "reject",
            f"qc/{batch_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            batch = self._require_batch(batch_id)
            if batch["disposition"] in ("released", "rejected"):
                raise StateTransitionError("批次已结案", details={"disposition": batch["disposition"]})
            reason_text = (reason or "").strip()
            if not reason_text:
                raise ValidationError("拒收必须给出原因")
            now = self.clock.timestamp_iso()
            batch["rejection"] = {"actor": actor, "reason": reason_text, "at": now}
            self._move(batch, "rejected", actor, f"拒收：{reason_text}")
            batch["updated_at"] = now
            record = self._save_batch(batch)
            entry = self.store.append(
                RELEASE_STREAM,
                {
                    "batch_id": batch["batch_id"],
                    "type": "reject",
                    "actor": actor,
                    "reason": reason_text,
                    "at": now,
                },
            )
            self._refresh_gauges()
            trace.attach(record).note("release_seq", entry.seq)
            return self._view(batch)

    # ------------------------------------------------------------------ 放行车道
    def disposition(self, batch_id: str) -> Mapping[str, Any] | None:
        """供投料侧查询：批次未登记时返回 ``None``，由调用方决定口径。"""

        batch = self._load_batch(batch_id)
        if batch is None:
            return None
        return {
            "batch_id": batch["batch_id"],
            "material": batch["material"],
            "disposition": batch["disposition"],
            "round": batch["round"],
        }

    def require_released(self, batch_id: str) -> Mapping[str, Any]:
        """投料前硬校验：未放行即拒绝，并给出当前处置状态。"""

        batch = self._load_batch(batch_id)
        if batch is None:
            raise NotFoundError("批次未登记，无法确认放行状态", details={"batch_id": batch_id})
        if batch["disposition"] != "released":
            raise GuardViolation(
                "批次未放行，禁止投入使用",
                details={"batch_id": batch_id, "disposition": batch["disposition"]},
            )
        release = batch["release"] or {}
        return {
            "batch_id": batch["batch_id"],
            "release_type": release.get("type"),
            "released_at": release.get("at"),
            "released_by": release.get("actor"),
            "report_ids": list(release.get("report_ids", [])),
        }

    # ------------------------------------------------------------------ 查询
    def inspect_batch(self, batch_id: str) -> Mapping[str, Any]:
        """批次全链档案：计划、样品、化验单与放行/让步/拒收记录。"""

        batch = self._require_batch(batch_id)
        reports = []
        for report_id in batch["report_ids"]:
            record = self.store.get(self.key("report", report_id))
            if record is not None:
                reports.append(dict(record.payload))
        releases = [
            dict(entry.payload)
            for entry in self.store.read_stream(RELEASE_STREAM, limit=1000)
            if entry.payload.get("batch_id") == batch["batch_id"]
        ]
        return {"batch": self._view(batch), "reports": reports, "releases": releases}

    def spec(self, material: str) -> Mapping[str, Any] | None:
        text = (material or "").strip()
        if not _TOKEN_PATTERN.match(text):
            return None
        record = self.store.get(self.key("spec", text))
        if record is None:
            return None
        return {
            "material": record.payload.get("material"),
            "limits": dict(record.payload.get("limits", {})),
            "version": record.version,
            "updated_at": record.payload.get("updated_at"),
        }

    def batches(self, *, disposition: str | None = None, limit: int = 50) -> list[Mapping[str, Any]]:
        if limit < 1:
            raise ValidationError("limit 必须为正", details={"limit": limit})
        items: list[dict[str, Any]] = []
        for key in self.store.list_keys(self.key("batch")):
            record = self.store.get(key)
            if record is None:
                continue
            payload = record.payload
            if disposition is not None and payload.get("disposition") != disposition:
                continue
            items.append(
                {
                    "batch_id": payload.get("batch_id"),
                    "material": payload.get("material"),
                    "disposition": payload.get("disposition"),
                    "round": payload.get("round"),
                    "quantity_tons": payload.get("quantity_tons"),
                    "registered_at": payload.get("registered_at"),
                    "updated_at": payload.get("updated_at"),
                }
            )
        return items[-limit:]

    def status(self) -> Mapping[str, Any]:
        batches = self.batches(limit=500)
        by_disposition = {name: 0 for name in DISPOSITIONS}
        held: list[str] = []
        for item in batches:
            disposition = str(item["disposition"])
            by_disposition[disposition] = by_disposition.get(disposition, 0) + 1
            if disposition == "held":
                held.append(str(item["batch_id"]))
        specs: dict[str, Any] = {}
        for key in self.store.list_keys(self.key("spec")):
            record = self.store.get(key)
            if record is None:
                continue
            material = str(record.payload.get("material", ""))
            specs[material] = {"version": record.version, "updated_at": record.payload.get("updated_at")}
        return {
            "state": "holding" if held else "ready",
            "batches_total": len(batches),
            "by_disposition": by_disposition,
            "held_batches": held,
            "specs": specs,
            "max_rounds": int(self.settings.qc_max_rounds),
            "recent_reports": [dict(entry.payload) for entry in self.store.read_stream(REPORT_STREAM, limit=5)],
            "recent_releases": [dict(entry.payload) for entry in self.store.read_stream(RELEASE_STREAM, limit=5)],
        }

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _new_slot(point: str, seq: int, round_no: int) -> dict[str, Any]:
        return {
            "point": point,
            "seq": seq,
            "round": round_no,
            "status": "pending",
            "collected_at": None,
            "collector": None,
            "report_id": None,
            "verdict": None,
            "judged_at": None,
        }

    def _load_batch(self, batch_id: str) -> dict[str, Any] | None:
        text = (batch_id or "").strip()
        if not _TOKEN_PATTERN.match(text):
            return None
        record = self.store.get(self.key("batch", text))
        return None if record is None else dict(record.payload)

    def _require_batch(self, batch_id: str) -> dict[str, Any]:
        text = (batch_id or "").strip()
        if not text:
            raise ValidationError("必须给出批次号")
        batch = self._load_batch(text)
        if batch is None:
            raise NotFoundError("批次未登记", details={"batch_id": text})
        return batch

    def _save_batch(self, batch: Mapping[str, Any]) -> Any:
        return self.store.commit_intent(self.key("batch", str(batch["batch_id"])), batch)

    def _move(self, batch: dict[str, Any], target: str, actor: str, reason: str) -> None:
        current = batch["disposition"]
        if target not in TRANSITIONS.get(current, ()):
            raise StateTransitionError(
                "批次处置状态跃迁被拒绝",
                details={
                    "batch_id": batch["batch_id"],
                    "from": current,
                    "to": target,
                    "allowed": list(TRANSITIONS.get(current, ())),
                },
            )
        batch["disposition"] = target
        batch["history"].append(
            {
                "from": current,
                "to": target,
                "actor": actor,
                "reason": reason,
                "at": self.clock.timestamp_iso(),
            }
        )

    def _find_slot(self, batch: dict[str, Any], point: str, seq: float | None, *, want: str) -> dict[str, Any]:
        point_text = (point or "").strip()
        if not point_text:
            raise ValidationError("必须给出取样点")
        planned = [slot for slot in batch["slots"] if slot["point"] == point_text]
        if not planned:
            raise GuardViolation(
                "取样点不在本批次取样计划中",
                details={"point": point_text, "planned": list(batch["plan"]["points"])},
            )
        # 每个取样点以自己的最新一轮为准：复检只给不合格点开新轮次，
        # 其余点仍在原来的轮次上继续取样、录结果。
        latest = max(slot["round"] for slot in planned)
        planned = [slot for slot in planned if slot["round"] == latest]
        if seq is not None:
            number = _positive_int(seq, "样品序号")
            for slot in planned:
                if slot["seq"] == number:
                    self._require_slot_status(slot, want)
                    return slot
            raise NotFoundError(
                "样品序号不存在",
                details={"point": point_text, "seq": number, "round": latest},
            )
        for slot in sorted(planned, key=lambda item: item["seq"]):
            if slot["status"] == want:
                return slot
        if want == "collected":
            raise GuardViolation(
                "先登记取样，再录化验结果",
                details={"point": point_text, "round": latest},
            )
        raise GuardViolation(
            "该取样点本轮样品已全部取完",
            details={"point": point_text, "round": latest},
        )

    @staticmethod
    def _require_slot_status(slot: Mapping[str, Any], want: str) -> None:
        if slot["status"] == want:
            return
        if want == "pending":
            raise GuardViolation(
                "样品已取或已判定，如需复测请开复检",
                details={"point": slot["point"], "seq": slot["seq"], "status": slot["status"]},
            )
        if slot["status"] == "pending":
            raise GuardViolation(
                "先登记取样，再录化验结果",
                details={"point": slot["point"], "seq": slot["seq"]},
            )
        raise GuardViolation(
            "该样品已有判定结果，如需复测请开复检",
            details={"point": slot["point"], "seq": slot["seq"], "report_id": slot["report_id"]},
        )

    def _point_rows(self, batch: Mapping[str, Any]) -> list[tuple[str, int, list[Mapping[str, Any]]]]:
        """每个取样点取最新一轮的样品行：判定与放行都以最新一轮为准。"""

        rows = []
        for point in batch["plan"]["points"]:
            slots = [slot for slot in batch["slots"] if slot["point"] == point]
            latest = max(slot["round"] for slot in slots)
            rows.append((point, latest, [slot for slot in slots if slot["round"] == latest]))
        return rows

    def _latest_slots(self, batch: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        slots: list[Mapping[str, Any]] = []
        for _point, _round, current in self._point_rows(batch):
            slots.extend(current)
        return slots

    def _point_summary(self, batch: Mapping[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for point, round_no, slots in self._point_rows(batch):
            judged = [slot for slot in slots if slot["status"] == "judged"]
            failed = [slot for slot in judged if slot["verdict"] == "fail"]
            if failed:
                status = "fail"
            elif len(judged) == len(slots):
                status = "pass"
            else:
                status = "pending"
            rows.append(
                {
                    "point": point,
                    "round": round_no,
                    "samples": len(slots),
                    "collected": sum(1 for slot in slots if slot["status"] != "pending"),
                    "judged": len(judged),
                    "failed": len(failed),
                    "status": status,
                }
            )
        return rows

    def _current_deviations(self, batch: Mapping[str, Any]) -> list[dict[str, Any]]:
        deviations: list[dict[str, Any]] = []
        for slot in self._latest_slots(batch):
            if slot["verdict"] != "fail" or not slot["report_id"]:
                continue
            record = self.store.get(self.key("report", str(slot["report_id"])))
            if record is None:
                continue
            for item in record.payload.get("deviations", []):
                deviations.append(
                    {
                        "point": slot["point"],
                        "seq": slot["seq"],
                        "report_id": slot["report_id"],
                        **dict(item),
                    }
                )
        return deviations

    def _view(self, batch: Mapping[str, Any]) -> dict[str, Any]:
        view = dict(batch)
        view["points"] = self._point_summary(batch)
        return view

    def _refresh_gauges(self) -> None:
        counts = {name: 0 for name in DISPOSITIONS}
        for key in self.store.list_keys(self.key("batch")):
            record = self.store.get(key)
            if record is None:
                continue
            disposition = str(record.payload.get("disposition", ""))
            if disposition in counts:
                counts[disposition] += 1
        for name, count in counts.items():
            self.metrics.observe(f"qc.batches_{name}", float(count))


__all__ = ["QcLab", "DISPOSITIONS", "TRANSITIONS", "REPORT_STREAM", "RELEASE_STREAM"]
