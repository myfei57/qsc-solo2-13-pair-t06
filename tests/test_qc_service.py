"""QC 取样—判定—隔离—复检/让步—放行状态机测试。"""

from __future__ import annotations

import base64
import unittest
from pathlib import Path

from flashsmelter.errors import (
    ConflictError,
    StateTransitionError,
    ValidationError,
)
from flashsmelter.qc.service import QualityService
from flashsmelter.qc.settings import QcSettings

from .helpers import make_root

CONC_SPECS = {"Cu品位(%)": 22.0, "水分(%)": 10.0, "As(%)": 0.2}


class QcCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_root("flashsmelter-qc-")
        self.service = QualityService(QcSettings(root=self.root))

    def call(self, action: str, **params):
        return self.service.invoke(action, params, source="test")

    # ---------------------------------------------------------- 夹具
    def make_material(self, code: str = "CU-CONC", *, points: int = 3):
        existing = self.call("material.list")["materials"]
        for material in existing:
            if material["code"] == code and len(material["sampling_plan"]) == points:
                return material["id"]
        m = self.call("material.create", code=code, name="铜精矿", unit="t", actor="t")
        mid = m["material_id"]
        self.call("spec.set", material_id=mid, name="Cu品位(%)", min_value=20.0, unit="%", actor="t")
        self.call("spec.set", material_id=mid, name="水分(%)", max_value=12.0, unit="%", actor="t")
        self.call("spec.set", material_id=mid, name="As(%)", max_value=0.5, unit="%", actor="t")
        names = ["车头", "车中", "车尾", "备用点"]
        for i in range(points):
            letter = "ABCD"[i]
            self.call(
                "plan.set_point", material_id=mid, point_code=letter,
                point_name=names[i], quantity=1, seq=i + 1, actor="t",
            )
        return mid

    def make_batch(self, mid: str | None = None, no: str = "B-1") -> str:
        mid = mid or self.make_material()
        return self.call("batch.create", material_id=mid, batch_no=no, supplier="矿点甲",
                         quantity=100.0, actor="t")["batch_id"]

    def samples_by_point(self, bid: str) -> dict[str, dict]:
        view = self.call("batch.get", batch_id=bid)
        return {s["point_code"]: s for s in view["samples"]}

    def submit_all(self, bid: str, values_by_point: dict[str, dict]):
        reports = []
        for code, values in values_by_point.items():
            sample = self.samples_by_point(bid)[code]
            self.call("sample.collect", sample_id=sample["id"], actor="取样工")
            r = self.call("report.submit", sample_id=sample["id"], values=values,
                          actor="化验员", analyst="化验员")
            reports.append(r)
        for r in self.call("report.list", batch_id=bid)["reports"]:
            self.call("report.review", report_id=r["id"], decision="approved", actor="主管")
        return reports

    def passing(self, *, water: float = 10.0, arsenic: float = 0.2) -> dict:
        return {"Cu品位(%)": 22.0, "水分(%)": water, "As(%)": arsenic}

    def batch_status(self, bid: str) -> str:
        return str(self.call("batch.get", batch_id=bid)["status"])


class MasterDataTest(QcCase):
    def test_spec_requires_bound_and_order(self) -> None:
        mid = self.call("material.create", code="X", name="物料X", actor="t")["material_id"]
        with self.assertRaises(ValidationError):
            self.call("spec.set", material_id=mid, name="无上下限项", actor="t")
        with self.assertRaises(ValidationError):
            self.call("spec.set", material_id=mid, name="倒置", min_value=10, max_value=1, actor="t")

    def test_batch_requires_sampling_plan(self) -> None:
        mid = self.call("material.create", code="EMPTY", name="无计划料", actor="t")["material_id"]
        with self.assertRaises(ValidationError):
            self.call("batch.create", material_id=mid, batch_no="B-X", actor="t")

    def test_batch_create_plans_samples(self) -> None:
        bid = self.make_batch(no="B-1")
        view = self.call("batch.get", batch_id=bid)
        self.assertEqual(3, len(view["samples"]))
        self.assertEqual([s["status"] for s in view["samples"]], ["planned"] * 3)
        self.assertEqual("pending_sampling", view["status"])

    def test_duplicate_codes_rejected(self) -> None:
        self.make_material()
        with self.assertRaises(ConflictError):
            self.call("material.create", code="CU-CONC", name="重复", actor="t")
        mid = self.call("material.list")["materials"][0]["id"]
        self.call("batch.create", material_id=mid, batch_no="DUP", actor="t")
        with self.assertRaises(ConflictError):
            self.call("batch.create", material_id=mid, batch_no="DUP", actor="t")


class VerdictAndQuarantineTest(QcCase):
    def test_all_pass_reaches_pass_state(self) -> None:
        bid = self.make_batch(no="B-OK")
        self.submit_all(bid, {c: self.passing() for c in "ABC"})
        self.assertEqual("pass", self.batch_status(bid))
        view = self.call("batch.get", batch_id=bid)
        self.assertTrue(view["release_ready"]["ready"])
        self.assertEqual([], view["release_ready"]["problems"])

    def test_any_fail_quarantines_and_blocks_release(self) -> None:
        bid = self.make_batch(no="B-BAD")
        values = {"A": self.passing(), "B": self.passing(water=13.5), "C": self.passing()}
        self.submit_all(bid, values)
        self.assertEqual("quarantined", self.batch_status(bid))
        with self.assertRaises(StateTransitionError) as ctx:
            self.call("release.create", batch_id=bid, actor="仓库")
        self.assertTrue(any("B" in p for p in ctx.exception.details["problems"]))
        rejected = self.service.invoke(
            "audit.list", {"action": "release.create", "outcome": "rejected"})["events"]
        self.assertTrue(rejected)

    def test_pending_review_blocks_release(self) -> None:
        bid = self.make_batch(no="B-REVIEW")
        samples = self.samples_by_point(bid)
        for code in "ABC":
            sid = samples[code]["id"]
            self.call("sample.collect", sample_id=sid, actor="取样工")
            self.call("report.submit", sample_id=sid, values=self.passing(), actor="化验员")
        # 一张都不审
        self.assertEqual("pending_review", self.batch_status(bid))
        with self.assertRaises(StateTransitionError):
            self.call("release.create", batch_id=bid, actor="仓库")

    def test_uncollected_sample_blocks_release(self) -> None:
        bid = self.make_batch(no="B-MISS")
        samples = self.samples_by_point(bid)
        for code in "AB":
            sid = samples[code]["id"]
            self.call("sample.collect", sample_id=sid, actor="取样工")
            r = self.call("report.submit", sample_id=sid, values=self.passing(), actor="化验员")
            self.call("report.review", report_id=r["report_id"], decision="approved", actor="主管")
        with self.assertRaises(StateTransitionError) as ctx:
            self.call("release.create", batch_id=bid, actor="仓库")
        self.assertIn("problems", ctx.exception.details)

    def test_report_must_cover_all_specs(self) -> None:
        bid = self.make_batch(no="B-PART")
        sid = self.samples_by_point(bid)["A"]["id"]
        self.call("sample.collect", sample_id=sid, actor="取样工")
        with self.assertRaises(ValidationError) as ctx:
            self.call("report.submit", sample_id=sid, values={"Cu品位(%)": 22}, actor="化验员")
        self.assertIn("水分(%)", ctx.exception.details["missing"])

    def test_cannot_submit_before_sampling(self) -> None:
        bid = self.make_batch(no="B-EARLY")
        sid = self.samples_by_point(bid)["A"]["id"]
        with self.assertRaises(StateTransitionError):
            self.call("report.submit", sample_id=sid, values=self.passing(), actor="化验员")


class RetestTest(QcCase):
    def _quarantined_batch(self, no: str = "B-RT"):
        bid = self.make_batch(no=no)
        reports = self.submit_all(
            bid, {"A": self.passing(), "B": self.passing(water=13.5), "C": self.passing()}
        )
        bad = next(r for r in self.call("report.list", batch_id=bid)["reports"]
                   if r["overall_verdict"] == "fail")
        return bid, bad["id"]

    def test_retest_pass_overrides_and_releases(self) -> None:
        bid, bad_report = self._quarantined_batch()
        retest = self.call("retest.create", report_id=bad_report, reason="留样复测", actor="主管")
        self.assertEqual("pending_retest", self.batch_status(bid))
        new_sid = retest["new_sample_id"]
        self.call("sample.collect", sample_id=new_sid, actor="取样工")
        self.call(
            "report.submit", sample_id=new_sid, kind="retest",
            source_retest_id=retest["retest_id"], values=self.passing(water=11.0),
            actor="化验员",
        )
        rt_report = next(r for r in self.call("report.list", batch_id=bid)["reports"]
                         if r["kind"] == "retest")
        self.call("report.review", report_id=rt_report["id"], decision="approved", actor="主管")
        self.assertEqual("pass", self.batch_status(bid))

        release = self.call("release.create", batch_id=bid, actor="仓库")
        self.assertEqual("qualified", release["basis"])
        locked = self.call("batch.trace", batch_id=bid)["releases"][0]["reports"]
        b_report = next(x for x in locked if x["point_code"] == "B")
        view = self.call("report.get", report_id=b_report["report_id"])
        self.assertEqual("retest", view["kind"])
        self.assertEqual("pass", view["overall_verdict"])

    def test_retest_requires_failing_report(self) -> None:
        bid = self.make_batch(no="B-RT2")
        reports = self.submit_all(bid, {c: self.passing() for c in "ABC"})
        good = self.call("report.list", batch_id=bid)["reports"][0]["id"]
        with self.assertRaises(ValidationError):
            self.call("retest.create", report_id=good, reason="不应允许", actor="主管")

    def test_retest_report_must_match_retest_order(self) -> None:
        bid, bad_report = self._quarantined_batch(no="B-RT3")
        retest = self.call("retest.create", report_id=bad_report, reason="x", actor="主管")
        # 另加一个不相干的临时取样点，用它冒充复检样应被拒绝
        extra = self.call("sample.add_point", batch_id=bid, point_code="X", point_name="加取", actor="t")
        self.call("sample.collect", sample_id=extra["sample_id"], actor="取样工")
        with self.assertRaises(ValidationError):
            self.call(
                "report.submit", sample_id=extra["sample_id"], kind="retest",
                source_retest_id=retest["retest_id"], values=self.passing(), actor="化验员",
            )


class ConcessionTest(QcCase):
    def _fail_batch(self, no: str = "B-CN"):
        bid = self.make_batch(no=no)
        self.submit_all(
            bid, {"A": self.passing(), "B": self.passing(arsenic=0.62), "C": self.passing()}
        )
        return bid

    def test_concession_lifecycle(self) -> None:
        bid = self._fail_batch()
        good = self.make_batch(no="B-CN-OK")
        self.submit_all(good, {c: self.passing() for c in "ABC"})
        with self.assertRaises(ValidationError):
            self.call("concession.apply", batch_id=good, reason="合格批次不能申请让步", actor="主管")

        self.call("concession.apply", batch_id=bid, reason="As略超内控，受控掺用",
                  disposition="限配比8%", actor="主管")
        self.assertEqual("concession_pending", self.batch_status(bid))
        with self.assertRaises(StateTransitionError):
            self.call("release.create", batch_id=bid, actor="仓库")
        with self.assertRaises(ConflictError):
            self.call("concession.apply", batch_id=bid, reason="重复申请", actor="主管")

        cid = self.call("concession.list", batch_id=bid)["concessions"][0]["id"]
        self.call("concession.decide", concession_id=cid, decision="approved",
                  note="同意", actor="质量经理")
        self.assertEqual("concession_approved", self.batch_status(bid))
        release = self.call("release.create", batch_id=bid, actor="仓库")
        self.assertEqual("concession", release["basis"])

    def test_concession_rejected_stays_quarantined(self) -> None:
        bid = self._fail_batch(no="B-CN2")
        self.call("concession.apply", batch_id=bid, reason="r", actor="主管")
        cid = self.call("concession.list", batch_id=bid)["concessions"][0]["id"]
        self.call("concession.decide", concession_id=cid, decision="rejected",
                  note="不同意", actor="质量经理")
        self.assertEqual("quarantined", self.batch_status(bid))
        with self.assertRaises(StateTransitionError):
            self.call("release.create", batch_id=bid, actor="仓库")


class ReleaseAndTerminalTest(QcCase):
    def test_release_once_and_terminal(self) -> None:
        bid = self.make_batch(no="B-REL")
        self.submit_all(bid, {c: self.passing() for c in "ABC"})
        release = self.call("release.create", batch_id=bid, destination="产线", actor="仓库")
        self.assertTrue(release["release_no"].startswith("RL-"))
        with self.assertRaises(ConflictError):
            self.call("release.create", batch_id=bid, actor="仓库")
        # 终态批次不能再取样/录报告
        sid = self.samples_by_point(bid)["A"]["id"]
        with self.assertRaises(StateTransitionError):
            self.call("batch.reject", batch_id=bid, reason="晚了", actor="t")

    def test_batch_reject_closes_open_records(self) -> None:
        bid = self.make_batch(no="B-REJ")
        self.submit_all(
            bid, {"A": self.passing(), "B": self.passing(water=13.5), "C": self.passing()}
        )
        self.call("concession.apply", batch_id=bid, reason="等审批", actor="主管")
        self.call("batch.reject", batch_id=bid, reason="供方责任，退货", actor="主管")
        self.assertEqual("rejected", self.batch_status(bid))
        with self.assertRaises(StateTransitionError):
            self.call("release.create", batch_id=bid, actor="仓库")
        cn = self.call("concession.list", batch_id=bid)["concessions"][0]
        self.assertEqual("rejected", cn["status"])


class TraceAndAttachmentTest(QcCase):
    def test_trace_covers_sample_and_report_actions(self) -> None:
        bid = self.make_batch(no="B-TRACE")
        self.submit_all(bid, {c: self.passing() for c in "ABC"})
        timeline = self.call("batch.trace", batch_id=bid)["timeline"]
        actions = {e["action"] for e in timeline}
        self.assertIn("sample.collect", actions)
        self.assertIn("report.submit", actions)
        self.assertIn("report.review", actions)
        self.assertTrue(all(e["outcome"] == "ok" for e in timeline))

    def test_attachment_roundtrip(self) -> None:
        bid = self.make_batch(no="B-ATT")
        sid = self.samples_by_point(bid)["A"]["id"]
        self.call("sample.collect", sample_id=sid, actor="t")
        payload = bytes(range(256)) * 2
        r = self.call(
            "report.submit", sample_id=sid, values=self.passing(), actor="t",
            attachments={"p": {
                "filename": "化验单.png",
                "content_type": "image/png",
                "content_base64": base64.b64encode(payload).decode(),
            }},
        )
        view = self.call("report.get", report_id=r["report_id"])
        self.assertEqual(1, len(view["attachments"]))
        path = Path(self.root) / "attachments"
        files = list(path.iterdir())
        self.assertEqual(payload, files[0].read_bytes())

    def test_oversized_attachment_rejected(self) -> None:
        settings = QcSettings(root=self.root, max_body_bytes=4096, max_attachment_bytes=1024)
        service = QualityService(settings)
        mid = service.invoke(
            "material.create", {"code": "SMALL", "name": "小", "actor": "t"})["material_id"]
        service.invoke("spec.set", {"material_id": mid, "name": "x", "max_value": 1.0, "actor": "t"})
        service.invoke("plan.set_point", {"material_id": mid, "point_code": "A",
                                          "point_name": "a", "quantity": 1, "actor": "t"})
        bid = service.invoke("batch.create", {"material_id": mid, "batch_no": "S1", "actor": "t"})["batch_id"]
        sid = service.invoke("batch.get", {"batch_id": bid})["samples"][0]["id"]
        service.invoke("sample.collect", {"sample_id": sid, "actor": "t"})
        big = base64.b64encode(b"x" * 2000).decode()
        with self.assertRaises(ValidationError):
            service.invoke("report.submit", {"sample_id": sid, "values": {"x": 0.5}, "actor": "t",
                                             "attachments": {"p": {"filename": "f",
                                                                  "content_base64": big}}})


class PersistenceTest(QcCase):
    def test_state_survives_reopen(self) -> None:
        bid = self.make_batch(no="B-PERSIST")
        self.submit_all(
            bid, {"A": self.passing(), "B": self.passing(water=13.5), "C": self.passing()}
        )
        reopened = QualityService(QcSettings(root=self.root))
        self.assertEqual("quarantined", reopened.invoke("batch.get", {"batch_id": bid})["status"])
        with self.assertRaises(StateTransitionError):
            reopened.invoke("release.create", {"batch_id": bid, "actor": "t"})


if __name__ == "__main__":
    unittest.main()
