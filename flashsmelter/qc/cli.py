"""QC 质控台命令行入口：serve 启动控制台，seed 灌入演示数据。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from ..errors import FlashSmelterError
from .console import QcConsoleApp, QcConsoleServer
from .service import QualityService
from .settings import QcSettings


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _build_service(args: argparse.Namespace) -> QualityService:
    settings = QcSettings.from_env(None)
    if getattr(args, "root", None):
        settings = settings.with_root(Path(args.root))
    return QualityService(settings)


def _cmd_serve(args: argparse.Namespace) -> int:
    settings = QcSettings.from_env(None)
    if args.root:
        settings = settings.with_root(Path(args.root))
    settings = replace(settings, host=args.host, port=args.port)
    settings.validate()
    service = QualityService(settings)
    console = QcConsoleApp(service)
    server = QcConsoleServer(console, host=settings.host, port=settings.port)
    host, port = server.start()
    print(f"QC 质控台已启动：http://{host}:{port}/qc/（Ctrl+C 停止）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def _cmd_seed(args: argparse.Namespace) -> int:
    service = _build_service(args)

    def call(action: str, **params: Any) -> dict[str, Any]:
        return dict(service.invoke(action, params, source=f"seed:{action}"))

    # 物料 1：铜精矿（品位 + 水分 + 有害元素）
    if not service.store.query_one("SELECT id FROM materials WHERE code = ?", ("CU-CONC",)):
        conc = call("material.create", code="CU-CONC", name="铜精矿", unit="t", actor="system")
        mid = conc["material_id"]
        call("spec.set", material_id=mid, name="Cu品位(%)", method="GB/T 3884.1", min_value=20.0, unit="%", seq=1, actor="system")
        call("spec.set", material_id=mid, name="水分(%)", method="GB/T 14263", max_value=12.0, unit="%", seq=2, actor="system")
        call("spec.set", material_id=mid, name="As(%)", method="GB/T 3884.18", max_value=0.50, unit="%", seq=3, actor="system")
        call("plan.set_point", material_id=mid, point_code="A", point_name="车头", quantity=1, frequency="每批", seq=1, actor="system")
        call("plan.set_point", material_id=mid, point_code="B", point_name="车中", quantity=1, frequency="每批", seq=2, actor="system")
        call("plan.set_point", material_id=mid, point_code="C", point_name="车尾", quantity=1, frequency="每批", seq=3, actor="system")

    # 物料 2：工业硫酸
    if not service.store.query_one("SELECT id FROM materials WHERE code = ?", ("H2SO4",)):
        acid = call("material.create", code="H2SO4", name="工业硫酸(98%)", unit="t", actor="system")
        aid = acid["material_id"]
        call("spec.set", material_id=aid, name="H2SO4(%)", method="GB/T 534", min_value=98.0, unit="%", seq=1, actor="system")
        call("spec.set", material_id=aid, name="灰分(%)", method="GB/T 534", max_value=0.03, unit="%", seq=2, actor="system")
        call("plan.set_point", material_id=aid, point_code="T", point_name="槽车上部", quantity=1, frequency="每批", seq=1, actor="system")
        call("plan.set_point", material_id=aid, point_code="B", point_name="槽车下部", quantity=1, frequency="每批", seq=2, actor="system")

    # 批次 1：一批水分超标的铜精矿（演示自动隔离 → 复检 → 复检合格 → 放行）
    if not service.store.query_one("SELECT id FROM batches WHERE batch_no = ?", ("CC-20260918-07",)):
        batch = call("batch.create", material_id=service.store.query_one(
            "SELECT id FROM materials WHERE code = 'CU-CONC'")["id"],
            batch_no="CC-20260918-07", supplier="宏达矿业", quantity=312.5,
            note="9月18日进厂汽车料 12 车", actor="仓库-李明")
        bid = batch["batch_id"]
        view = service.invoke("batch.get", {"batch_id": bid})
        samples = {s["point_code"]: s for s in view["samples"]}

        # A、C 点合格
        for code, cu, h2o, arsenic in (("A", 22.4, 10.1, 0.18), ("C", 21.8, 10.6, 0.22)):
            sid = samples[code]["id"]
            call("sample.collect", sample_id=sid, actor="取样工-王强")
            call("report.submit", sample_id=sid, actor="化验-赵静", analyst="化验-赵静",
                 values={"Cu品位(%)": cu, "水分(%)": h2o, "As(%)": arsenic})
        # B 点水分不合格（13.5 > 12）
        sid_b = samples["B"]["id"]
        call("sample.collect", sample_id=sid_b, actor="取样工-王强")
        bad = call("report.submit", sample_id=sid_b, actor="化验-赵静", analyst="化验-赵静",
                   values={"Cu品位(%)": 21.1, "水分(%)": 13.5, "As(%)": 0.31})
        bad_report = bad["report_id"]

        # 审核：合格报告审过；不合格报告也审过（确认隔离成立）
        for report in service.invoke("report.list", {"batch_id": bid})["reports"]:
            call("report.review", report_id=report["id"], decision="approved",
                 note="结果复核无误", actor="质量主管-陈敏")

        # 发起复检（留样 4h 烘干复测）
        retest = call("retest.create", report_id=bad_report,
                      reason="B点水分超标，供方要求留样烘干复测", actor="质量主管-陈敏")
        new_sid = retest["new_sample_id"]
        call("sample.collect", sample_id=new_sid, actor="取样工-王强")
        call("report.submit", sample_id=new_sid, kind="retest",
             source_retest_id=retest["retest_id"],
             actor="化验-赵静", analyst="化验-赵静",
             values={"Cu品位(%)": 21.3, "水分(%)": 11.2, "As(%)": 0.30})
        rt_reports = service.invoke("report.list", {"batch_id": bid})["reports"]
        rt_report = next(r for r in rt_reports if r["kind"] == "retest")
        call("report.review", report_id=rt_report["id"], decision="approved",
             note="复测水分 11.2%，符合要求，覆盖初检", actor="质量主管-陈敏")

    # 批次 2：As 超标，让步审批中（演示让步流程与卡住不放）
    if not service.store.query_one("SELECT id FROM batches WHERE batch_no = ?", ("CC-20260919-02",)):
        batch = call("batch.create", material_id=service.store.query_one(
            "SELECT id FROM materials WHERE code = 'CU-CONC'")["id"],
            batch_no="CC-20260919-02", supplier="宏达矿业", quantity=280.0, actor="仓库-李明")
        bid = batch["batch_id"]
        view = service.invoke("batch.get", {"batch_id": bid})
        samples = {s["point_code"]: s for s in view["samples"]}
        fail_report_id = None
        for code, cu, h2o, arsenic in (("A", 21.0, 9.8, 0.41), ("B", 20.6, 10.4, 0.62), ("C", 21.5, 9.5, 0.38)):
            sid = samples[code]["id"]
            call("sample.collect", sample_id=sid, actor="取样工-王强")
            r = call("report.submit", sample_id=sid, actor="化验-赵静", analyst="化验-赵静",
                     values={"Cu品位(%)": cu, "水分(%)": h2o, "As(%)": arsenic})
            if arsenic > 0.5:
                fail_report_id = r["report_id"]
        for report in service.invoke("report.list", {"batch_id": bid})["reports"]:
            call("report.review", report_id=report["id"], decision="approved",
                 note="B点As超标，其余正常" if report["id"] == fail_report_id else "复核无误",
                 actor="质量主管-陈敏")
        call("concession.apply", batch_id=bid,
             reason="B点As 0.62%略超0.50%内控限，低于0.80%国标限值；该批占配料比例≤8%，配料后As可受控",
             disposition="让步接收，限配比掺用", actor="质量主管-陈敏")
        # 保持 pending，方便演示审批页；实际由质量经理决定

    # 批次 3：全部合格、已放行（演示放行单锁定化验单）
    if not service.store.query_one("SELECT id FROM batches WHERE batch_no = ?", ("SA-20260917-03",)):
        batch = call("batch.create", material_id=service.store.query_one(
            "SELECT id FROM materials WHERE code = 'H2SO4'")["id"],
            batch_no="SA-20260917-03", supplier="瑞磷化工", quantity=29.6, actor="仓库-李明")
        bid = batch["batch_id"]
        view = service.invoke("batch.get", {"batch_id": bid})
        for sample in view["samples"]:
            call("sample.collect", sample_id=sample["id"], actor="取样工-王强")
            call("report.submit", sample_id=sample["id"], actor="化验-赵静", analyst="化验-赵静",
                 values={"H2SO4(%)": 98.4, "灰分(%)": 0.018})
        for report in service.invoke("report.list", {"batch_id": bid})["reports"]:
            call("report.review", report_id=report["id"], decision="approved",
                 note="复核无误", actor="质量主管-陈敏")
        call("release.create", batch_id=bid, destination="硫酸库→制酸车间",
             actor="仓库-周涛", note="正常放行")

    print("演示数据已就绪：")
    print("  CC-20260918-07  铜精矿（B点水分超标→复检合格，当前应可放行）")
    print("  CC-20260919-02  铜精矿（As超标→让步申请审批中，放行被卡住）")
    print("  SA-20260917-03  工业硫酸（合格已放行，可看放行单与化验单关联）")
    return 0


def _cmd_call(args: argparse.Namespace) -> int:
    service = _build_service(args)
    try:
        params = json.loads(args.params_json) if args.params_json else {}
        if not isinstance(params, dict):
            raise ValidationErrorCli("参数必须是 JSON 对象")
        result = service.invoke(args.action, params, source=f"cli:{args.action}")
    except FlashSmelterError as exc:
        _print(exc.to_dict())
        return 1
    _print({"action": args.action, "result": dict(result)})
    return 0


class ValidationErrorCli(FlashSmelterError):
    code = "validation-error"
    status = 400


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flashsmelter-qc", description="化验取样与放行质控台")
    parser.add_argument("--root", help="QC 状态根目录（默认 var/qc/）")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动 QC 网页控制台")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8090)
    serve.set_defaults(func=_cmd_serve)

    seed = sub.add_parser("seed", help="写入演示批次（物料/规格/取样点/批次/化验单/复检/让步/放行）")
    seed.set_defaults(func=_cmd_seed)

    call = sub.add_parser("call", help="直接调用一个质控动作（调试用）")
    call.add_argument("action", help="动作名，如 batch.create")
    call.add_argument("--params-json", help="JSON 对象参数")
    call.set_defaults(func=_cmd_call)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    try:
        return int(args.func(args))
    except FlashSmelterError as exc:
        _print(exc.to_dict())
        return 1


__all__ = ["main", "build_parser"]
