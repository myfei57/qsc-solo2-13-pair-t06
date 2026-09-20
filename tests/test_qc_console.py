"""QC HTTP 控制台端到端测试。"""

from __future__ import annotations

import base64
import json
import unittest
import urllib.request
import urllib.error

from flashsmelter.qc.console import QcConsoleApp, QcConsoleServer
from flashsmelter.qc.service import QualityService
from flashsmelter.qc.settings import QcSettings

from .helpers import make_root


class QcHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_root("flashsmelter-qc-http-")
        self.service = QualityService(QcSettings(root=self.root, port=0))
        self.app = QcConsoleApp(self.service)
        self.server = QcConsoleServer(self.app, host="127.0.0.1", port=0)
        self.host, self.port = self.server.start()

    def tearDown(self) -> None:
        self.server.stop()

    def _url(self, path: str) -> str:
        return f"http://{self.host}:{self.port}{path}"

    def _post(self, path: str, payload: dict):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self._url(path), data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def _get(self, path: str):
        with urllib.request.urlopen(self._url(path), timeout=5) as resp:
            return resp.status, resp.headers, resp.read()

    def _seed(self) -> str:
        mid = self.service.invoke("material.create", {"code": "M1", "name": "物料", "actor": "t"})["material_id"]
        self.service.invoke("spec.set", {"material_id": mid, "name": "纯度", "min_value": 90.0, "actor": "t"})
        self.service.invoke(
            "plan.set_point",
            {"material_id": mid, "point_code": "A", "point_name": "车头", "quantity": 1, "actor": "t"},
        )
        bid = self.service.invoke("batch.create", {"material_id": mid, "batch_no": "HB-1", "actor": "t"})["batch_id"]
        sid = self.service.invoke("batch.get", {"batch_id": bid})["samples"][0]["id"]
        self.service.invoke("sample.collect", {"sample_id": sid, "actor": "t"})
        return bid, sid

    def test_health_and_page(self) -> None:
        status, _, body = self._get("/api/qc/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", json.loads(body)["status"])
        status, headers, body = self._get("/qc/")
        self.assertEqual(200, status)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("取样与放行质控台".encode(), body)

    def test_blocked_release_returns_409_json(self) -> None:
        bid, _ = self._seed()  # 还没有化验单
        status, payload = self._post("/api/qc/release/create", {"batch_id": bid, "actor": "t"})
        self.assertEqual(409, status)
        self.assertEqual("state-transition-rejected", payload["error"])
        self.assertTrue(payload["details"]["problems"])

    def test_attachment_with_chinese_filename(self) -> None:
        _bid, sid = self._seed()
        payload = b"\x89PNG\r\n\x1a\n" + b"x" * 100
        status, out = self._post("/api/qc/report/submit", {
            "sample_id": sid,
            "values": {"纯度": 95.0},
            "actor": "t",
            "attachments": {"p": {
                "filename": "化验单照片.png",
                "content_type": "image/png",
                "content_base64": base64.b64encode(payload).decode(),
            }},
        })
        self.assertEqual(200, status, out)
        report = self.service.invoke("report.get", {"report_id": out["result"]["report_id"]})
        aid = report["attachments"][0]["id"]
        status, headers, body = self._get(f"/api/qc/attachments/{aid}")
        self.assertEqual(200, status)
        self.assertEqual(payload, body)
        self.assertIn("image/png", headers["Content-Type"])
        self.assertIn("filename*=UTF-8''", headers["Content-Disposition"])

    def test_unknown_route_404(self) -> None:
        try:
            self._get("/api/qc/nope/nope")
            self.fail("expected 404")
        except urllib.error.HTTPError as exc:
            self.assertEqual(404, exc.code)

    def test_write_action_rejects_get(self) -> None:
        try:
            self._get("/api/qc/batch/create")
            self.fail("expected 405")
        except urllib.error.HTTPError as exc:
            self.assertEqual(405, exc.code)
            self.assertIn("POST", exc.read().decode())

    def test_read_action_allows_get(self) -> None:
        status, _, body = self._get("/api/qc/material/list")
        self.assertEqual(200, status)
        self.assertIn("materials", json.loads(body)["result"])


if __name__ == "__main__":
    unittest.main()
