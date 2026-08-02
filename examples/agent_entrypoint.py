"""Minimal provider-neutral image entrypoint.

Replace ``run_agent`` with a framework or model adapter.  Keep the file
contract and task/result identity bindings unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any


WORKSPACE = Path("/workspace")
TASK_PATH = WORKSPACE / "task_packet.json"
RESULT_PATH = WORKSPACE / "result_packet.json"


def canonical_sha256(value: dict[str, Any]) -> str:
    content = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def run_agent(task: dict[str, Any]) -> str:
    """Call the selected Agent/provider here and return a concise summary."""

    return f"Role {task['role_id']} accepted the provider-neutral task contract."


def main() -> int:
    task = json.loads(TASK_PATH.read_text(encoding="utf-8"))
    if not isinstance(task, dict):
        raise ValueError("task_packet.json must contain an object")
    summary = run_agent(task)
    manifest_sha256 = os.environ.get("AGENT_ARTIFACT_MANIFEST_SHA256")
    if not manifest_sha256 or len(manifest_sha256) != 64:
        raise RuntimeError(
            "AGENT_ARTIFACT_MANIFEST_SHA256 must bind the produced artifact manifest"
        )
    result = {
        "schema_version": "1.0.0",
        "packet_id": f"result-{task['packet_id']}",
        "task_id": task["task_id"],
        "run_id": task["run_id"],
        "attempt_id": task["attempt_id"],
        "role_id": task["role_id"],
        "task_packet_sha256": canonical_sha256(task),
        "status": "pass",
        "summary": summary,
        "artifact_manifest": {
            "artifact_id": "artifact-manifest",
            "manifest_sha256": manifest_sha256,
            "location": f"/runs/{task['task_id']}/artifact-manifest.json",
        },
        "claims_and_sources": [],
        "assumptions": [],
        "validation_performed": [],
        "metrics": {},
        "known_failures": [],
        "uncertainties": [],
        "provenance": {
            "prompt_sha256": task["prompt"]["sha256"],
            "container_image_digest": os.environ.get(
                "AGENT_IMAGE_DIGEST", "sha256:" + "0" * 64
            ),
            "input_manifest_sha256s": [
                item["manifest_sha256"] for item in task["input_artifacts"]
            ],
            "random_seeds": [],
        },
        "completed_at": "1970-01-01T00:00:00Z",
    }
    temporary = RESULT_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(RESULT_PATH)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"agent failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

