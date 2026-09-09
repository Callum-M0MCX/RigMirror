"""Portable, data-only diagnostic reports for RigMirror testers."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import platform
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Optional


REPORT_FORMAT = "RigMirror Test Report"
REPORT_SCHEMA_VERSION = 1


def write_test_report(
    base_directory: str | Path,
    *,
    category: str,
    endpoint: str,
    port: str,
    baud: int,
    outcome: str,
    detail: str,
    traffic: Iterable[str] = (),
    driver: Optional[dict[str, Any]] = None,
    driver_filename: str = "",
    results: Optional[list[str]] = None,
    connection_settings: Optional[dict[str, Any]] = None,
) -> Path:
    """Write one human-readable JSON .rmreport containing the exact driver used."""
    timestamp = datetime.now().astimezone().replace(microsecond=0)
    metadata = (driver or {}).get("metadata", {})
    radio_name = str(metadata.get("model") or "serial-port")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", radio_name).strip("-") or "radio"
    safe_outcome = re.sub(r"[^A-Za-z0-9]+", "-", outcome.upper()).strip("-") or "REPORT"
    reports_directory = Path(base_directory) / "reports"
    reports_directory.mkdir(parents=True, exist_ok=True)
    target = reports_directory / (
        f"{safe_name}_{timestamp.strftime('%Y%m%d_%H%M%S')}_{safe_outcome}.rmreport"
    )
    payload = {
        "format": REPORT_FORMAT,
        "schema_version": REPORT_SCHEMA_VERSION,
        "created_local": timestamp.isoformat(),
        "created_utc": timestamp.astimezone(timezone.utc).isoformat(),
        "cat_traffic_time_basis": "local time with numeric UTC offset",
        "rigmirror_version": "0.3.003",
        "category": category,
        "endpoint": endpoint,
        "outcome": outcome.upper(),
        "detail": detail,
        "serial": {"port": port, "baud": int(baud), "data_format": "8N1"},
        "connection_settings": connection_settings or {},
        "host": {
            "system": platform.system(),
            "release": platform.release(),
            "python": platform.python_version(),
            "executable_type": "frozen" if getattr(sys, "frozen", False) else "python",
        },
        "driver_filename": driver_filename,
        "driver_revision": metadata.get("driver_revision"),
        "driver": driver,
        "results": list(results or []),
        "cat_traffic": [str(line).rstrip("\n") for line in traffic],
    }
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target
