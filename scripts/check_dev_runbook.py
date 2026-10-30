"""Offline development-run preflight. No dataset, model, or network access."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dynamic_memory.contracts import (  # noqa: E402
    BuildStatus, ConversationSample, ConversationSession, DatasetBundle, SourceRef, SourceTurn,
)
from dynamic_memory.data.repository import SourceRepository  # noqa: E402
from dynamic_memory.pipeline.settings import load_settings  # noqa: E402
from dynamic_memory.services import DynamicMemoryService  # noqa: E402

CONFIG_NAMES = ("models", "data_split", "evaluation", "extraction", "retrieval", "reasoning")
SAMPLE_ID = "synthetic_dev_runbook"


def _check(name: str, expected: str, actual: str, error: str | None = None) -> dict:
    status = "SKIP" if actual == expected == "skipped" else "PASS" if actual == expected else "FAIL"
    result = {"name": name, "expected": expected, "actual": actual, "status": status}
    if error is not None:
        result["error"] = error
    return result


def _null_paths(value: object, prefix: str = "$") -> list[str]:
    if value is None:
        return [prefix]
    if isinstance(value, dict):
        return [path for key, child in value.items()
                for path in _null_paths(child, f"{prefix}.{key}")]
    if isinstance(value, list):
        return [path for index, child in enumerate(value)
                for path in _null_paths(child, f"{prefix}[{index}]")]
    return []


def check_config(root: Path, name: str) -> dict:
    relative = f"configs/{name}.yaml"
    path = root / relative
    result = {"path": relative, "sha256": None, "null_paths": [], "result": "missing"}
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return result
    except OSError as exc:
        result["result"] = "unreadable"
        result["error"] = f"read failed ({type(exc).__name__})"
        return result
    result["sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        document = yaml.safe_load(raw.decode("utf-8"))
    except UnicodeError:
        result["result"] = "parse_error"
        result["error"] = "invalid UTF-8"
        return result
    except yaml.YAMLError:
        result["result"] = "parse_error"
        result["error"] = "invalid YAML"
        return result
    if not isinstance(document, dict):
        result["result"] = "structure_error"
        result["error"] = "YAML root must be a mapping"
        return result
    result["null_paths"] = _null_paths(document)
    if result["null_paths"]:
        result["result"] = "not_frozen"
        result["error"] = "null fields remain"
    else:
        result["result"] = "passed"
    return result


def check_source_repository() -> dict:
    turn = SourceTurn(sample_id=SAMPLE_ID, session_id="synthetic_session", dia_id="synthetic_turn",
                      speaker="Synthetic speaker", text="Synthetic offline turn")
    sample = ConversationSample(sample_id=SAMPLE_ID, source_order_1_based=1,
                                sessions=[ConversationSession(session_id="synthetic_session", turns=[turn])])
    bundle = DatasetBundle(snapshot_sha256="a" * 64, samples=[sample],
                           split_by_sample_id={SAMPLE_ID: "main_dev"})
    repository = SourceRepository(bundle)
    found_sample = repository.get_sample(SAMPLE_ID)
    found_turn = repository.get_turn(SAMPLE_ID, SourceRef(session_id="synthetic_session",
                                                         dia_id="synthetic_turn"))
    assert repository.list_samples() == [SAMPLE_ID]
    assert repository.snapshot_sha256 == bundle.snapshot_sha256
    assert found_sample == sample and found_turn == turn
    assert ConversationSample.model_validate_json(found_sample.model_dump_json()) == sample
    assert SourceTurn.model_validate_json(found_turn.model_dump_json()) == turn
    return {"synthetic_repository_check": "passed", "real_dataset_check": "not_performed",
            "sample_id": SAMPLE_ID, "session_count": len(found_sample.sessions), "turn_count": 1}


def check_dynamic_memory_service() -> dict:
    # The injected provider is an offline diagnostic, never a published build claim.
    repository = SourceRepository(DatasetBundle(snapshot_sha256="a" * 64, samples=[
        ConversationSample(sample_id=SAMPLE_ID, source_order_1_based=1,
                           sessions=[ConversationSession(session_id="synthetic_session", turns=[
                               SourceTurn(sample_id=SAMPLE_ID, session_id="synthetic_session",
                                          dia_id="synthetic_turn", speaker="Synthetic speaker",
                                          text="Synthetic offline turn")])])],
        split_by_sample_id={SAMPLE_ID: "main_dev"}))
    expected = BuildStatus(dataset_state="ready", memory_state="not_built",
                           partial_build=False, build_id=None, error_code="BUILD_NOT_FOUND")
    service = DynamicMemoryService(sources=repository, build_status=lambda _sample: expected)
    actual = service.get_build_status(SAMPLE_ID)
    assert type(actual) is BuildStatus
    assert actual == expected
    assert BuildStatus.model_validate_json(actual.model_dump_json()) == actual
    return {"synthetic_service_check": "passed", "sample_id": SAMPLE_ID,
            "build_status": actual.model_dump(mode="json")}


def _git_sha(root: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root,
                                   stderr=subprocess.DEVNULL, text=True).strip()


def build_manifest(root: Path = ROOT) -> dict:
    checks = []
    configs = [check_config(root, name) for name in CONFIG_NAMES]
    for config in configs:
        checks.append(_check(config["path"], "passed", config["result"], config.get("error")))
    if all(config["result"] == "passed" for config in configs):
        try:
            load_settings(root / "configs")
        except Exception as exc:
            checks.append(_check("typed_settings", "passed", "validation_error",
                                 f"typed settings validation failed ({type(exc).__name__})"))
        else:
            checks.append(_check("typed_settings", "passed", "passed"))
    else:
        checks.append(_check("typed_settings", "skipped", "skipped"))
    api_key = "present" if "DEEPSEEK_API_KEY" in os.environ else "absent"
    checks.append(_check("api_key_presence", "present", api_key,
                         "DEEPSEEK_API_KEY is absent" if api_key == "absent" else None))
    try:
        source = check_source_repository()
        checks.append(_check("source_repository", "passed", "passed"))
    except Exception as exc:
        source = {"synthetic_repository_check": "failed", "real_dataset_check": "not_performed"}
        checks.append(_check("source_repository", "passed", "failed",
                             f"synthetic repository check failed ({type(exc).__name__})"))
    try:
        service = check_dynamic_memory_service()
        checks.append(_check("dynamic_memory_service", "passed", "passed"))
    except Exception as exc:
        service = {"synthetic_service_check": "failed"}
        checks.append(_check("dynamic_memory_service", "passed", "failed",
                             f"synthetic service check failed ({type(exc).__name__})"))
    failed = sum(item["status"] == "FAIL" for item in checks)
    return {"format_version": 1, "git_sha": _git_sha(root), "python_version": sys.version.split()[0],
            "configs": configs, "checks": checks, "api_key": api_key,
            "source_repository": source, "dynamic_memory_service": service,
            "real_call_readiness": {"status": "not_ready", "reason":
                "presence does not verify key validity, frozen configuration, or a real call"},
            "cross_branch": {"status": "deferred", "reason": "B/C/D/E 接口未冻结，不在本轮"},
            "passed_checks": sum(item["status"] == "PASS" for item in checks),
            "failed_checks": failed, "overall_status": "FAIL" if failed else "PASS"}


def main(root: Path = ROOT) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        manifest = build_manifest(root)
        code = 1 if manifest["failed_checks"] else 0
    except Exception as exc:
        manifest = {"format_version": 1, "overall_status": "ERROR", "error":
                    f"runbook execution failed ({type(exc).__name__})"}
        code = 2
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
