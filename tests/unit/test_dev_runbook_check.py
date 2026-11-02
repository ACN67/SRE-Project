"""Isolated synthetic tests for the offline development runbook."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_dev_runbook.py"
spec = importlib.util.spec_from_file_location("check_dev_runbook", SCRIPT)
runbook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runbook)


@pytest.fixture
def fixture_root(tmp_path, monkeypatch):
    folder = tmp_path / "configs"
    folder.mkdir()
    for name in runbook.CONFIG_NAMES:
        (folder / f"{name}.yaml").write_text("nested: [ok, {child: value}]\n", encoding="utf-8")
    monkeypatch.setattr(runbook, "load_settings", lambda _: object())
    monkeypatch.setattr(runbook, "_git_sha", lambda _: "a" * 40)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "DO_NOT_PRINT_THIS_SECRET")
    return tmp_path


def test_six_configs_and_exact_raw_hashes(fixture_root):
    report = runbook.build_manifest(fixture_root)
    assert len(report["configs"]) == 6
    for config in report["configs"]:
        raw = (fixture_root / config["path"]).read_bytes()
        assert config["sha256"] == hashlib.sha256(raw).hexdigest()
        assert config["result"] == "passed"
    assert report["overall_status"] == "PASS"
    assert report["passed_checks"] == len(report["checks"])
    assert report["failed_checks"] == 0
    assert report["git_sha"] == "a" * 40
    assert report["python_version"] == runbook.sys.version.split()[0]
    assert report["cross_branch"]["status"] == "deferred"


@pytest.mark.parametrize("contents,result,paths", [
    (None, "missing", []),
    ("broken: [\n", "parse_error", []),
    ("- item\n", "structure_error", []),
    ("outer: [{child: null}]\n", "passed", ["$.outer[0].child"]),
])
def test_bad_config_states(fixture_root, contents, result, paths):
    path = fixture_root / "configs" / "models.yaml"
    if contents is None:
        path.unlink()
    else:
        path.write_text(contents, encoding="utf-8")
    config = runbook.check_config(fixture_root, "models")
    assert config["result"] == result
    assert config["null_paths"] == paths
    assert (config["sha256"] is None) == (contents is None)
    assert runbook.build_manifest(fixture_root)["overall_status"] == (
        "PASS" if result == "passed" else "FAIL")


def test_key_absent_continues_other_checks_and_returns_zero(fixture_root, monkeypatch, capsys):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert runbook.main(fixture_root) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["api_key"] == "absent"
    assert report["real_call_readiness"]["status"] == "not_ready"
    assert report["source_repository"]["synthetic_repository_check"] == "passed"
    assert report["dynamic_memory_service"]["synthetic_service_check"] == "passed"
    assert report["failed_checks"] == 0
    assert report["offline_health"] == "PASS"
    assert report["formal_dev_readiness"] == "NOT_READY"


@pytest.mark.parametrize("value,present", [(None, False), ("", False), ("  \t", False),
                                            ("DO_NOT_PRINT_THIS_SECRET", True)])
def test_key_presence_only_and_formal_readiness(fixture_root, monkeypatch, value, present, capsys):
    if value is None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    else:
        monkeypatch.setenv("DEEPSEEK_API_KEY", value)
    assert runbook.main(fixture_root) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["api_key"] == ("present" if present else "absent")
    assert report["offline_health"] == "PASS"
    assert report["formal_dev_readiness"] == "NOT_READY"
    assert "DO_NOT_PRINT_THIS_SECRET" not in output


def test_real_config_null_is_offline_healthy(tmp_path, monkeypatch, capsys):
    repository = SCRIPT.parents[1]
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    for name in runbook.CONFIG_NAMES:
        (config_dir / f"{name}.yaml").write_bytes((repository / "configs" / f"{name}.yaml").read_bytes())
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(runbook, "_git_sha", lambda _: "a" * 40)
    assert runbook.main(tmp_path) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["offline_health"] == "PASS"
    assert report["formal_dev_readiness"] == "NOT_READY"
    assert report["source_repository"]["synthetic_repository_check"] == "passed"


def test_real_config_type_error_is_offline_failure(tmp_path, monkeypatch, capsys):
    repository = SCRIPT.parents[1]
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    for name in runbook.CONFIG_NAMES:
        (config_dir / f"{name}.yaml").write_bytes(
            (repository / "configs" / f"{name}.yaml").read_bytes())
    model_path = config_dir / "models.yaml"
    model_path.write_text(model_path.read_text(encoding="utf-8").replace(
        "extraction_max_input_tokens: 4096", "extraction_max_input_tokens: invalid"),
        encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(runbook, "_git_sha", lambda _: "a" * 40)
    assert runbook.main(tmp_path) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["offline_health"] == "FAIL"
    assert report["formal_dev_readiness"] == "NOT_READY"
    assert next(check for check in report["checks"] if check["name"] == "typed_settings")["status"] == "FAIL"


def test_key_value_never_emitted_and_json_canonical(fixture_root, capsys):
    assert runbook.main(fixture_root) == 0
    first = capsys.readouterr().out
    assert runbook.main(fixture_root) == 0
    assert capsys.readouterr().out == first
    assert "DO_NOT_PRINT_THIS_SECRET" not in first
    assert first == json.dumps(json.loads(first), ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False) + "\n"


def test_real_source_repository_synthetic_contract():
    result = runbook.check_source_repository()
    assert result["synthetic_repository_check"] == "passed"
    assert result["real_dataset_check"] == "not_performed"
    assert result["turn_count"] == 1


def test_source_repository_failure_isolated(fixture_root, monkeypatch):
    monkeypatch.setattr(runbook, "check_source_repository", lambda: (_ for _ in ()).throw(ValueError("secret")))
    report = runbook.build_manifest(fixture_root)
    assert report["source_repository"]["synthetic_repository_check"] == "failed"
    assert report["dynamic_memory_service"]["synthetic_service_check"] == "passed"
    assert "secret" not in json.dumps(report)
    assert report["overall_status"] == "FAIL"


def test_source_repository_constructor_failure(fixture_root, monkeypatch):
    monkeypatch.setattr(runbook, "SourceRepository", lambda _bundle: (_ for _ in ()).throw(ValueError("secret")))
    report = runbook.build_manifest(fixture_root)
    assert report["source_repository"]["synthetic_repository_check"] == "failed"
    assert "secret" not in json.dumps(report)
    assert report["api_key"] == "present"


def test_typed_settings_failure(fixture_root, monkeypatch):
    monkeypatch.setattr(runbook, "load_settings", lambda _: (_ for _ in ()).throw(ValueError("secret")))
    report = runbook.build_manifest(fixture_root)
    assert next(check for check in report["checks"] if check["name"] == "typed_settings")["status"] == "FAIL"
    assert report["failed_checks"] == 1
    assert "secret" not in json.dumps(report)


def test_real_dynamic_service_valid_contract():
    result = runbook.check_dynamic_memory_service()
    assert result["synthetic_service_check"] == "passed"
    assert result["build_status"]["memory_state"] == "not_built"
    assert result["build_status"]["error_code"] == "BUILD_NOT_FOUND"


def test_dynamic_service_invalid_state_isolated(fixture_root, monkeypatch):
    class BrokenService:
        def __init__(self, **_kwargs):
            pass

        def get_build_status(self, _sample):
            return "invalid"

    monkeypatch.setattr(runbook, "DynamicMemoryService", BrokenService)
    report = runbook.build_manifest(fixture_root)
    assert report["dynamic_memory_service"]["synthetic_service_check"] == "failed"
    assert report["source_repository"]["synthetic_repository_check"] == "passed"
    assert report["failed_checks"] == 1


def test_program_error_is_distinct(fixture_root, monkeypatch, capsys):
    monkeypatch.setattr(runbook, "_git_sha", lambda _: (_ for _ in ()).throw(OSError("secret")))
    assert runbook.main(fixture_root) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["overall_status"] == "ERROR"
    assert "secret" not in json.dumps(report)
