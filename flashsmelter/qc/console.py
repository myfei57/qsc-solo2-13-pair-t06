"""QC 子系统的 HTTP 控制台。

与主控制台一样，HTTP 层不做业务判定，只把请求转成动作注册表里的调用；另外负责
两件表现层的事：化验单照片的字节读写、内置网页（``GET /qc/``）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, quote, urlparse

from ..errors import FlashSmelterError, NotFoundError, ValidationError
from .service import QualityService

LOGGER = logging.getLogger("flashsmelter.qc.console")

_PLACEHOLDER = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _read_resource() -> bytes:
    path = Path(__file__).with_name("web") / "index.html"
    return path.read_bytes()


class QcConsoleApp:
    def __init__(self, service: QualityService, *, page: bytes | None = None) -> None:
        self.service = service
        self.settings = service.settings
        self._page = page if page is not None else _read_resource()
        self._started = time.monotonic()
        self._routes: list[tuple[str, tuple[str, ...], Callable[..., Any], bool]] = []
        self._register()

    # --------------------------------------------------------------- 路由
    def _add(self, method: str, pattern: str, handler: Callable[..., Any], *, exact_segments: bool = False) -> None:
        segments = tuple(part for part in pattern.strip("/").split("/") if part)
        self._routes.append((method.upper(), segments, handler, exact_segments))

    def _register(self) -> None:
        self._add("GET", "/qc", self._page_handler, exact_segments=True)
        self._add("GET", "/qc/", self._page_handler, exact_segments=True)
        self._add("GET", "/api/qc/health", self._health, exact_segments=True)
        self._add("GET", "/api/qc/actions", self._actions, exact_segments=True)
        self._add("GET", "/api/qc/attachments/{attachment_id}", self._attachment, exact_segments=True)
        for name in self.service.actions:
            parts = name.split(".")
            pattern = "/api/qc/" + "/".join(parts)
            self._add("POST", pattern, self._make_action(name), exact_segments=True)
            if name in QualityService.READ_ONLY_ACTIONS:
                self._add("GET", pattern, self._make_action(name), exact_segments=True)

    def _resolve(self, method: str, path: str) -> tuple[Callable[..., Any], Mapping[str, str]]:
        segments = tuple(part for part in path.strip("/").split("/") if part)
        method = method.upper()
        allowed: set[str] = set()
        for route_method, route_segments, handler, _ in self._routes:
            if len(segments) != len(route_segments):
                continue
            captured: dict[str, str] = {}
            matched = True
            for expected, actual in zip(route_segments, segments):
                token = _PLACEHOLDER.match(expected)
                if token:
                    captured[token.group(1)] = actual
                elif expected != actual:
                    matched = False
                    break
            if not matched:
                continue
            if route_method == method:
                return handler, captured
            allowed.add(route_method)
        if allowed:
            raise FlashSmelterError(
                "该路径不支持此 HTTP 方法",
                code="method-not-allowed",
                status=405,
                details={"path": path, "method": method, "allowed": sorted(allowed)},
            )
        raise NotFoundError("接口不存在", details={"path": path})

    # --------------------------------------------------------------- 视图
    def _page_handler(self, _captured: Mapping[str, str], _params: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"__html__": self._page}

    def _health(self, _c: Mapping[str, str], _p: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "status": "ok",
            "service": "flashsmelter-qc",
            "uptime_seconds": round(time.monotonic() - self._started, 3),
        }

    def _actions(self, _c: Mapping[str, str], _p: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"actions": self.service.describe_actions()}

    def _attachment(self, captured: Mapping[str, str], _p: Mapping[str, Any]) -> Mapping[str, Any]:
        row = self.service.store.query_one(
            "SELECT * FROM attachments WHERE id = ?", (captured["attachment_id"],)
        )
        if row is None:
            raise NotFoundError("附件不存在", details={"attachment_id": captured["attachment_id"]})
        path = Path(row["stored_path"])
        if not path.exists():
            raise NotFoundError("附件文件缺失", details={"path": str(path)})
        return {
            "__bytes__": path.read_bytes(),
            "content_type": row["content_type"],
            "filename": row["filename"],
        }

    def _make_action(self, action: str) -> Callable[..., Any]:
        def handler(_captured: Mapping[str, str], params: Mapping[str, Any]) -> Mapping[str, Any]:
            payload = dict(params)
            # 详情类 GET 允许浏览器用通用 ?id=...，这里按动作归一成具名参数。
            if payload.get("id") not in (None, ""):
                entity = action.split(".")[0]
                target_param = {
                    "batch": "batch_id",
                    "report": "report_id",
                    "retest": "retest_id",
                    "concession": "concession_id",
                    "release": "release_id",
                    "material": "material_id",
                }.get(entity)
                if target_param and not payload.get(target_param):
                    payload[target_param] = payload["id"]
            result = self.service.invoke(action, payload, source=f"http:{action}")
            return {"action": action, "result": dict(result)}

        return handler

    # --------------------------------------------------------------- 分发
    def handle(
        self,
        method: str,
        path: str,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> "QcResponse":
        started = time.monotonic()
        combined: dict[str, Any] = {}
        if query:
            combined.update(query)
        if body:
            combined.update(body)
        try:
            handler, captured = self._resolve(method, path)
            payload = handler(captured, combined)
            response = QcResponse(payload=payload)
        except FlashSmelterError as exc:
            response = QcResponse(status=exc.status, payload=exc.with_detail("path", path).to_dict())
        except Exception:  # pragma: no cover
            LOGGER.exception("QC 控制台处理请求时发生未捕获异常", extra={"path": path})
            response = QcResponse(
                status=500,
                payload={"error": "internal-error", "message": "控制台内部错误", "status": 500,
                         "details": {"path": path}},
            )
        LOGGER.info("%s %s -> %s (%.1f ms)", method, path, response.status, (time.monotonic() - started) * 1000)
        return response


class QcResponse:
    def __init__(self, *, status: int = 200, payload: Mapping[str, Any] | None = None) -> None:
        self.status = status
        self.payload = dict(payload or {})

    def render(self) -> tuple[int, bytes, str, Mapping[str, str]]:
        if "__html__" in self.payload:
            body = bytes(self.payload["__html__"])
            return self.status, body, "text/html; charset=utf-8", {}
        if "__bytes__" in self.payload:
            body = bytes(self.payload["__bytes__"])
            content_type = str(self.payload.get("content_type") or "application/octet-stream")
            filename = str(self.payload.get("filename") or "attachment")
            ascii_name = filename.encode("ascii", "ignore").decode() or "attachment"
            quoted = quote(filename)
            headers = {
                "Content-Disposition": (
                    f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"
                )
            }
            return self.status, body, content_type, headers
        body = json.dumps(self.payload, ensure_ascii=False).encode("utf-8")
        return self.status, body, "application/json; charset=utf-8", {}


def build_handler(app: QcConsoleApp) -> type[BaseHTTPRequestHandler]:
    settings = app.settings

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "FlashSmelterQc/1.0"
        timeout = settings.request_timeout_seconds

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            LOGGER.debug("%s - %s", self.address_string(), format % args)

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            try:
                body = self._read_body()
            except FlashSmelterError as exc:
                self._write(QcResponse(status=exc.status, payload=exc.to_dict()))
                return
            response = app.handle(method, parsed.path, query=query, body=body)
            self._write(response)

        def _read_body(self) -> Mapping[str, Any]:
            length_header = self.headers.get("Content-Length")
            if length_header is None:
                return {}
            try:
                length = int(length_header)
            except ValueError as exc:
                raise ValidationError("Content-Length 不是整数", details={"value": length_header}) from exc
            if length <= 0:
                return {}
            if length > settings.max_body_bytes:
                raise ValidationError(
                    "请求体超过上限",
                    code="payload-too-large",
                    status=413,
                    details={"length": length, "max": settings.max_body_bytes},
                )
            raw = self.rfile.read(length)
            if not raw.strip():
                return {}
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是合法 JSON") from exc
            if not isinstance(decoded, dict):
                raise ValidationError("请求体必须是 JSON 对象", details={"type": type(decoded).__name__})
            return decoded

        def _write(self, response: QcResponse) -> None:
            status, body, content_type, extra_headers = response.render()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in extra_headers.items():
                self.send_header(key, value)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


class QcConsoleServer:
    def __init__(self, app: QcConsoleApp, *, host: str, port: int) -> None:
        self.app = app
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        if self._httpd is None:
            return self.host, self.port
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    def start(self) -> tuple[str, int]:
        self._httpd = ThreadingHTTPServer((self.host, self.port), build_handler(self.app))
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="qc-console", daemon=True)
        self._thread.start()
        return self.address

    def serve_forever(self) -> None:
        if self._httpd is None:
            self.start()
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:  # pragma: no cover
            LOGGER.info("收到中断信号，准备停止 QC 控制台")
        finally:
            self.stop()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


__all__ = ["QcConsoleApp", "QcConsoleServer", "QcResponse"]
