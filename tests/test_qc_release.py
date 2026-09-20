"""化验室取样、判定与批次放行链。"""

from __future__ import annotations

import unittest

from flashsmelter.application import Application
from flashsmelter.errors import GuardViolation, NotFoundError, StateTransitionError, ValidationError

from .helpers import feed_heat, make_app, make_root, settle_pool, start_furnace

LIMITS = {"cu": {"min": 20.0, "max": 30.0}, "h2o": {"max": 0.5}}


def set_spec(app, material="conc-a", limits=None):
    return app.qc.set_spec("lab", material=material, limits=limits or LIMITS)


def plan_batch(app, batch_id="B-1", material="conc-a", points="P1,P2", per_point=1):
    set_spec(app, material)
    return app.qc.plan(
        "sampler",
        batch_id=batch_id,
        material=material,
        points=points,
        per_point=per_point,
        quantity_tons=60.0,
        source="仓库-3",
    )


def collect_all(app, batch_id, points=("P1", "P2")):
    for point in points:
        app.qc.collect("sampler", batch_id=batch_id, point=point)


def report(app, batch_id, point, report_id, values):
    return app.qc.report("lab", batch_id=batch_id, point=point, report_id=report_id, values=values)


def judge_slot(app, batch_id, point, report_id, values):
    app.qc.collect("sampler", batch_id=batch_id, point=point)
    return report(app, batch_id, point, report_id, values)


class QcReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()

    def test_plan_requires_registered_spec(self) -> None:
        with self.assertRaises(NotFoundError):
            self.app.qc.plan("sampler", batch_id="B-1", material="mystery", points="P1")

    def test_plan_builds_slots_from_points_and_frequency(self) -> None:
        view = plan_batch(self.app, points="P1,P2,P3", per_point=2)
        self.assertEqual("pending", view["disposition"])
        self.assertEqual(6, len(view["slots"]))
        self.assertEqual(["P1", "P2", "P3"], [row["point"] for row in view["points"]])
        with self.assertRaises(GuardViolation):
            self.app.qc.plan("sampler", batch_id="B-1", material="conc-a", points="P1")

    def test_spec_limits_are_validated(self) -> None:
        with self.assertRaises(ValidationError):
            set_spec(self.app, limits={"cu": {"min": 30.0, "max": 20.0}})
        with self.assertRaises(ValidationError):
            set_spec(self.app, limits={"cu": {}})

    def test_collect_records_order_and_collector(self) -> None:
        plan_batch(self.app)
        self.app.qc.collect("sampler-a", batch_id="B-1", point="P1")
        self.app.clock.advance(30)
        view = self.app.qc.collect("sampler-b", batch_id="B-1", point="P2")
        slots = {slot["point"]: slot for slot in view["slots"]}
        self.assertEqual("collected", slots["P1"]["status"])
        self.assertEqual("sampler-a", slots["P1"]["collector"])
        self.assertLess(slots["P1"]["collected_at"], slots["P2"]["collected_at"])
        with self.assertRaises(GuardViolation):
            self.app.qc.collect("sampler", batch_id="B-1", point="P9")

    def test_report_requires_collected_sample(self) -> None:
        plan_batch(self.app)
        with self.assertRaises(GuardViolation) as blocked:
            report(self.app, "B-1", "P1", "R-1", {"cu": 25.0, "h2o": 0.2})
        self.assertIn("先登记取样", str(blocked.exception))

    def test_release_blocked_until_all_points_judged(self) -> None:
        plan_batch(self.app)
        judge_slot(self.app, "B-1", "P1", "R-1", {"cu": 25.0, "h2o": 0.2})
        with self.assertRaises(GuardViolation):
            self.app.qc.release("qc-lead", batch_id="B-1")

    def test_passing_results_release_with_report_trace(self) -> None:
        plan_batch(self.app)
        judge_slot(self.app, "B-1", "P1", "R-1", {"cu": 25.0, "h2o": 0.2})
        judge_slot(self.app, "B-1", "P2", "R-2", {"cu": 26.0, "h2o": 0.1})
        view = self.app.qc.release("qc-lead", batch_id="B-1")
        self.assertEqual("released", view["disposition"])
        self.assertEqual("normal", view["release"]["type"])
        self.assertEqual(["R-1", "R-2"], view["release"]["report_ids"])
        # 放行以后能追到是哪份化验单
        proof = self.app.qc.require_released("B-1")
        self.assertEqual(["R-1", "R-2"], proof["report_ids"])
        inspect = self.app.qc.inspect_batch("B-1")
        self.assertEqual({"R-1", "R-2"}, {item["report_id"] for item in inspect["reports"]})
        self.assertEqual("pass", inspect["reports"][0]["verdict"])
        self.assertEqual(1, len(inspect["releases"]))
        self.assertEqual("normal", inspect["releases"][0]["type"])

    def test_failing_result_holds_batch_and_blocks_release(self) -> None:
        plan_batch(self.app)
        collect_all(self.app, "B-1")
        result = report(self.app, "B-1", "P1", "R-1", {"cu": 18.0, "h2o": 0.2})
        self.assertEqual("fail", result["verdict"])
        self.assertEqual("held", result["disposition"])
        self.assertEqual("below-min", result["deviations"][0]["reason"])
        # 冻结不卡已取样品的化验录入，卡的是放行与取新样
        result2 = report(self.app, "B-1", "P2", "R-2", {"cu": 25.0, "h2o": 0.2})
        self.assertEqual("pass", result2["verdict"])
        with self.assertRaises(GuardViolation):
            self.app.qc.release("qc-lead", batch_id="B-1")
        with self.assertRaises(GuardViolation):
            self.app.qc.require_released("B-1")
        with self.assertRaises(StateTransitionError):
            self.app.qc.collect("sampler", batch_id="B-1", point="P1")

    def test_missing_analyte_fails_judgment(self) -> None:
        plan_batch(self.app)
        result = judge_slot(self.app, "B-1", "P1", "R-1", {"cu": 25.0})
        self.assertEqual("fail", result["verdict"])
        self.assertEqual("missing", result["deviations"][0]["reason"])
        self.assertEqual("h2o", result["deviations"][0]["analyte"])

    def test_duplicate_report_id_rejected(self) -> None:
        plan_batch(self.app)
        judge_slot(self.app, "B-1", "P1", "R-1", {"cu": 25.0, "h2o": 0.2})
        self.app.qc.collect("sampler", batch_id="B-1", point="P2")
        with self.assertRaises(GuardViolation):
            report(self.app, "B-1", "P2", "R-1", {"cu": 26.0, "h2o": 0.2})

    def test_retest_opens_new_round_and_release_uses_latest(self) -> None:
        plan_batch(self.app)
        collect_all(self.app, "B-1")
        report(self.app, "B-1", "P1", "R-1", {"cu": 18.0, "h2o": 0.2})
        report(self.app, "B-1", "P2", "R-2", {"cu": 25.0, "h2o": 0.2})
        view = self.app.qc.retest("qc-lead", batch_id="B-1")
        self.assertEqual("retesting", view["disposition"])
        self.assertEqual(2, view["round"])
        # 复检只给不合格点开新样
        points = {row["point"]: row for row in view["points"]}
        self.assertEqual(2, points["P1"]["round"])
        self.assertEqual(1, points["P2"]["round"])
        result = judge_slot(self.app, "B-1", "P1", "R-3", {"cu": 24.0, "h2o": 0.2})
        self.assertEqual("pass", result["verdict"])
        view = self.app.qc.release("qc-lead", batch_id="B-1")
        self.assertEqual("released", view["disposition"])
        self.assertEqual(["R-2", "R-3"], sorted(view["release"]["report_ids"]))

    def test_untested_points_continue_on_own_round_after_retest(self) -> None:
        # P1 不合格开复检后，P2 还没取的样应能在自己原来的轮次上继续
        plan_batch(self.app)
        judge_slot(self.app, "B-1", "P1", "R-1", {"cu": 18.0, "h2o": 0.2})
        self.app.qc.retest("qc-lead", batch_id="B-1")
        judge_slot(self.app, "B-1", "P1", "R-2", {"cu": 24.0, "h2o": 0.2})
        result = judge_slot(self.app, "B-1", "P2", "R-3", {"cu": 25.0, "h2o": 0.2})
        self.assertEqual("pass", result["verdict"])
        self.assertEqual(1, result["round"])
        view = self.app.qc.release("qc-lead", batch_id="B-1")
        self.assertEqual("released", view["disposition"])

    def test_retest_rounds_are_capped(self) -> None:
        app = make_app(qc_max_rounds=2)
        plan_batch(app, points="P1")
        judge_slot(app, "B-1", "P1", "R-1", {"cu": 18.0, "h2o": 0.2})
        app.qc.retest("qc-lead", batch_id="B-1")
        judge_slot(app, "B-1", "P1", "R-2", {"cu": 17.0, "h2o": 0.2})
        with self.assertRaises(GuardViolation):
            app.qc.retest("qc-lead", batch_id="B-1")

    def test_concession_releases_held_batch_with_record(self) -> None:
        plan_batch(self.app)
        collect_all(self.app, "B-1")
        report(self.app, "B-1", "P1", "R-1", {"cu": 18.0, "h2o": 0.2})
        report(self.app, "B-1", "P2", "R-2", {"cu": 25.0, "h2o": 0.2})
        view = self.app.qc.concession(
            "qc-lead", batch_id="B-1", approver="chief-engineer", reason="铜品位略低，本批搭配高品位矿使用"
        )
        self.assertEqual("released", view["disposition"])
        release = view["release"]
        self.assertEqual("concession", release["type"])
        self.assertEqual("chief-engineer", release["approver"])
        self.assertEqual("R-1", release["deviations"][0]["report_id"])
        inspect = self.app.qc.inspect_batch("B-1")
        self.assertEqual("concession", inspect["releases"][0]["type"])
        # 让步记录里能追到不合格的那份化验单
        self.assertIn("R-1", inspect["releases"][0]["report_ids"])

    def test_reject_is_terminal(self) -> None:
        plan_batch(self.app)
        judge_slot(self.app, "B-1", "P1", "R-1", {"cu": 18.0, "h2o": 0.2})
        view = self.app.qc.reject("qc-lead", batch_id="B-1", reason="品位过低，退货")
        self.assertEqual("rejected", view["disposition"])
        with self.assertRaises(StateTransitionError):
            self.app.qc.release("qc-lead", batch_id="B-1")
        with self.assertRaises(StateTransitionError):
            self.app.qc.retest("qc-lead", batch_id="B-1")
        with self.assertRaises(StateTransitionError):
            report(self.app, "B-1", "P2", "R-2", {"cu": 25.0, "h2o": 0.2})

    def test_batch_state_survives_restart(self) -> None:
        root = make_root()
        app = make_app(root=root)
        plan_batch(app)
        judge_slot(app, "B-1", "P1", "R-1", {"cu": 18.0, "h2o": 0.2})
        rebuilt = Application(app.settings)
        self.assertEqual("held", rebuilt.qc.disposition("B-1")["disposition"])
        with self.assertRaises(GuardViolation):
            rebuilt.qc.require_released("B-1")
        inspect = rebuilt.qc.inspect_batch("B-1")
        self.assertEqual("R-1", inspect["reports"][0]["report_id"])

    def test_converter_charge_blocked_until_qc_release(self) -> None:
        start_furnace(self.app)
        feed_heat(self.app, "H-1")
        self.app.furnace.tap("tester", heat_id="H-1", ladle_id="L-1", slag_tons=8.0, matte_tons=40.0)
        plan_batch(self.app, batch_id="L-1", points="P1")
        judge_slot(self.app, "L-1", "P1", "R-1", {"cu": 18.0, "h2o": 0.2})
        with self.assertRaises(GuardViolation):
            self.app.conv.charge("ops", ladle_id="L-1")
        self.app.qc.concession("qc-lead", batch_id="L-1", approver="chief", reason="让步接收，转炉搭配处理")
        status = self.app.conv.charge("ops", ladle_id="L-1")
        self.assertEqual("charging", status["state"])

    def test_unregistered_ladle_follows_original_flow(self) -> None:
        start_furnace(self.app)
        feed_heat(self.app, "H-1")
        self.app.furnace.tap("tester", heat_id="H-1", ladle_id="L-9", slag_tons=8.0, matte_tons=40.0)
        status = self.app.conv.charge("ops", ladle_id="L-9")
        self.assertEqual("charging", status["state"])

    def test_conc_inject_requires_released_batch(self) -> None:
        start_furnace(self.app)
        settle_pool(self.app)
        if self.app.burner.state == "stable":
            self.app.burner.attest("control-system")
        self.app.conc.arm("ops", heat_id="H-1")
        plan_batch(self.app, batch_id="B-7", points="P1")
        judge_slot(self.app, "B-7", "P1", "R-1", {"cu": 18.0, "h2o": 0.2})
        with self.assertRaises(GuardViolation):
            self.app.conc.inject("ops", rate_tph=50.0, tons=10.0, qc_batch_id="B-7")
        self.app.qc.concession("qc-lead", batch_id="B-7", approver="chief", reason="让步使用")
        status = self.app.conc.inject("ops", rate_tph=50.0, tons=10.0, qc_batch_id="B-7")
        self.assertEqual("injecting", status["state"])

    def test_actions_registered_and_invokable(self) -> None:
        self.app.invoke("qc.set_spec", {"material": "conc-a", "limits": LIMITS})
        view = self.app.invoke("qc.plan", {"batch_id": "B-1", "material": "conc-a", "points": "P1,P2"})
        self.assertEqual("pending", view["disposition"])
        names = {item["action"] for item in self.app.describe_actions()}
        for expected in (
            "qc.set_spec",
            "qc.plan",
            "qc.collect",
            "qc.report",
            "qc.release",
            "qc.retest",
            "qc.concession",
            "qc.reject",
            "qc.inspect",
        ):
            self.assertIn(expected, names)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
