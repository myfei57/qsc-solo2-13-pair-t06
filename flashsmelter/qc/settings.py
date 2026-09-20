"""QC 子系统运行期配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from ..errors import ConfigurationError, ValidationError

ENV_PREFIX = "FLASHSMELTER_QC_"


def _read_env(environ: Mapping[str, str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, caster in _ENV_FIELDS.items():
        raw = environ.get(ENV_PREFIX + name.upper())
        if raw is None or raw == "":
            continue
        try:
            values[name] = caster(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"环境变量 {ENV_PREFIX}{name.upper()} 取值无法解析",
                details={"value": raw, "expected": caster.__name__},
            ) from exc
    return values


_ENV_FIELDS: dict[str, Any] = {
    "host": str,
    "port": int,
    "max_body_bytes": int,
    "max_attachment_bytes": int,
    "audit_page_limit": int,
}


@dataclass(frozen=True, slots=True)
class QcSettings:
    """质控台配置。默认值面向厂内单机部署。"""

    root: Path = field(default_factory=lambda: Path("var/qc"))
    host: str = "127.0.0.1"
    port: int = 8090
    request_timeout_seconds: float = 10.0
    max_body_bytes: int = 8 * 1024 * 1024
    max_attachment_bytes: int = 6 * 1024 * 1024
    audit_page_limit: int = 500

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides: Any) -> "QcSettings":
        env = os.environ if environ is None else environ
        values = _read_env(env)
        root = env.get(ENV_PREFIX + "ROOT")
        if root:
            values["root"] = Path(root)
        values.update(overrides)
        settings = cls(**values)
        settings.validate()
        return settings

    def validate(self) -> None:
        # 0 保留给测试：让操作系统分配空闲端口。
        if not 0 <= self.port <= 65535:
            raise ValidationError("监听端口超出范围", details={"port": self.port})
        if self.max_body_bytes < 1024:
            raise ValidationError("请求体上限过小", details={"max_body_bytes": self.max_body_bytes})
        if self.max_attachment_bytes <= 0:
            raise ValidationError("附件上限必须为正", details={"max_attachment_bytes": self.max_attachment_bytes})
        if self.max_attachment_bytes > self.max_body_bytes:
            raise ValidationError(
                "附件上限不能超过请求体上限",
                details={"attachment": self.max_attachment_bytes, "body": self.max_body_bytes},
            )
        if self.audit_page_limit < 1:
            raise ValidationError("审计分页上限必须为正", details={"audit_page_limit": self.audit_page_limit})

    def with_root(self, root: Path | str) -> "QcSettings":
        updated = replace(self, root=Path(root))
        updated.validate()
        return updated

    @property
    def db_path(self) -> Path:
        return Path(self.root) / "qc.sqlite3"

    @property
    def attachment_dir(self) -> Path:
        return Path(self.root) / "attachments"

    def ensure_directories(self) -> Path:
        root = Path(self.root)
        root.mkdir(parents=True, exist_ok=True)
        self.attachment_dir.mkdir(parents=True, exist_ok=True)
        return root


__all__ = ["QcSettings", "ENV_PREFIX"]
