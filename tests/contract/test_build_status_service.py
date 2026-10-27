"""IC-012 additive schema 2.1 state combinations, synthetic only."""

import pytest
from pydantic import ValidationError

from dynamic_memory.contracts import BuildStatus


@pytest.mark.parametrize("dataset,memory,partial,build,code", [
    ("unloaded", "not_built", False, None, "DATA_NOT_LOADED"),
    ("load_error", "not_built", False, None, "DATA_LOAD_FAILED"),
    ("ready", "not_built", False, None, "BUILD_NOT_FOUND"),
    ("ready", "complete", False, "build-synthetic", None),
    ("ready", "partial", True, "build-synthetic", "ARTIFACT_INCOMPLETE"),
    ("ready", "failed", True, "build-synthetic", "MEMORY_BUILD_FAILED"),
    ("ready", "stale", False, "build-synthetic", "ARTIFACT_STALE"),
    ("ready", "corrupt", False, "build-synthetic", "ARTIFACT_CORRUPT"),
])
def test_valid_states(dataset, memory, partial, build, code):
    status = BuildStatus(dataset_state=dataset, memory_state=memory,
                         partial_build=partial, build_id=build, error_code=code)
    assert status.schema_version == "2.1"
    assert BuildStatus.model_validate_json(status.model_dump_json()) == status


@pytest.mark.parametrize("changes", [
    {"dataset_state": "missing"}, {"memory_state": "unknown"},
    {"partial_build": 0}, {"schema_version": "2.2"},
    {"build_id": " "}, {"error_code": "ARTIFACT_CORRUPT"},
    {"extra": "not allowed"},
])
def test_invalid_complete_and_unknown_states(changes):
    payload = dict(dataset_state="ready", memory_state="complete", partial_build=False,
                   build_id="build-synthetic", error_code=None)
    payload.update(changes)
    with pytest.raises(ValidationError):
        BuildStatus.model_validate(payload)


@pytest.mark.parametrize("payload", [
    dict(dataset_state="ready", memory_state="not_built", partial_build=False,
         build_id="old", error_code="BUILD_NOT_FOUND"),
    dict(dataset_state="ready", memory_state="partial", partial_build=False,
         build_id="b", error_code="ARTIFACT_INCOMPLETE"),
    dict(dataset_state="ready", memory_state="failed", partial_build=True,
         build_id=None, error_code="MEMORY_BUILD_FAILED"),
    dict(dataset_state="unloaded", memory_state="complete", partial_build=False,
         build_id="b", error_code=None),
    dict(dataset_state="load_error", memory_state="not_built", partial_build=False,
         build_id=None, error_code="DATA_NOT_LOADED"),
])
def test_inconsistent_combinations_fail_closed(payload):
    with pytest.raises(ValidationError):
        BuildStatus.model_validate(payload)
