"""A-01 tests use only repository YAML copies and temporary offline fixtures."""

from pathlib import Path

import pytest
import yaml

from dynamic_memory.pipeline.settings import SettingsError, load_settings


CONFIGS = Path(__file__).resolve().parents[2] / "configs"
NAMES = ("models", "data_split", "evaluation", "extraction", "retrieval", "reasoning")


@pytest.fixture
def config_dir(tmp_path):
    for name in NAMES:
        (tmp_path / f"{name}.yaml").write_bytes((CONFIGS / f"{name}.yaml").read_bytes())
    return tmp_path


def change(directory, name, key, value):
    path = directory / f"{name}.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document[key] = value
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")


def test_repository_defaults_and_pending_values():
    settings = load_settings()
    assert settings.models.default_backend == "mock"
    assert settings.models.api_enabled_by_default is False
    assert settings.models.model_id is None
    assert settings.retrieval.embedding_model is None
    assert settings.data_split.snapshot_sha256 == "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"
    assert settings.data_split.sample_ids == [
        "conv-26", "conv-30", "conv-41", "conv-42", "conv-43",
        "conv-44", "conv-47", "conv-48", "conv-49", "conv-50",
    ]
    assert settings.data_split.groups.main_dev.sample_ids == settings.data_split.sample_ids[:4]
    assert settings.data_split.groups.extra_dev.sample_ids == settings.data_split.sample_ids[4:8]
    assert settings.data_split.groups.test.sample_ids == settings.data_split.sample_ids[8:]
    assert settings.evaluation.qa_scoring == "pending_official_or_course_frozen_version"
    assert settings.retrieval.base_top_k == 8
    assert settings.retrieval.max_candidates == 12
    assert settings.retrieval.recall_at_k == [5, 10]
    assert settings.evaluation.recall_at_k == [5, 10]
    assert settings.models.qa_max_input_tokens == 32768
    assert settings.models.qa_max_output_tokens == 256
    assert settings.models.request_timeout_seconds == 120
    assert settings.models.max_retries == 2
    assert settings.models.max_concurrent_api_requests == 2
    assert settings.data_split.groups.main_dev.source_order_1_based == [1, 2, 3, 4]
    assert settings.data_split.groups.extra_dev.source_order_1_based == [5, 6, 7, 8]
    assert settings.data_split.groups.test.source_order_1_based == [9, 10]


def test_load_outside_repository_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert load_settings().models.default_backend == "mock"


def test_explicit_config_injection_and_no_api_activation(config_dir, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-do-not-use")
    change(config_dir, "retrieval", "base_top_k", 7)
    settings = load_settings(config_dir)
    assert settings.retrieval.base_top_k == 7  # loaded, not replaced by a hardcoded 8
    assert settings.models.default_backend == "mock"
    assert settings.models.api_enabled_by_default is False
    change(config_dir, "models", "api_enabled_by_default", True)
    with pytest.raises(SettingsError, match="models.yaml.*api_enabled_by_default=false"):
        load_settings(config_dir)


def test_missing_invalid_yaml_and_non_mapping_root(config_dir):
    (config_dir / "reasoning.yaml").unlink()
    with pytest.raises(SettingsError, match="reasoning.yaml.*not found"):
        load_settings(config_dir)
    (config_dir / "reasoning.yaml").write_text("query_intents: [unfinished", encoding="utf-8")
    with pytest.raises(SettingsError, match="reasoning.yaml.*invalid YAML"):
        load_settings(config_dir)
    (config_dir / "reasoning.yaml").write_text("- list root\n", encoding="utf-8")
    with pytest.raises(SettingsError, match="reasoning.yaml.*Mapping"):
        load_settings(config_dir)


@pytest.mark.parametrize(
    ("name", "key", "value", "field"),
    [
        ("models", "thinking", "unsupported", "thinking"),
        ("models", "qa_max_input_tokens", 0, "qa_max_input_tokens"),
        ("models", "max_concurrent_api_requests", -1, "max_concurrent_api_requests"),
        ("models", "request_timeout_seconds", 0, "request_timeout_seconds"),
        ("models", "max_retries", -1, "max_retries"),
        ("models", "qa_max_output_tokens", "256", "qa_max_output_tokens"),
        ("retrieval", "base_top_k", 0, "base_top_k"),
        ("retrieval", "recall_at_k", [5, 0], "recall_at_k"),
        ("evaluation", "main_methods", ["full_text", "bogus", "dynamic"], "main_methods"),
        ("extraction", "window_overlap_turns", -1, "window_overlap_turns"),
        ("reasoning", "decision_actions", ["RETAIN", "DROP"], "decision_actions"),
    ],
)
def test_invalid_field_has_file_and_field(config_dir, name, key, value, field):
    change(config_dir, name, key, value)
    with pytest.raises(SettingsError) as error:
        load_settings(config_dir)
    assert f"{name}.yaml" in str(error.value)
    assert field in str(error.value)


def test_cross_field_budget_and_extra_key(config_dir):
    change(config_dir, "retrieval", "max_candidates", 7)
    with pytest.raises(SettingsError, match="retrieval.yaml.*max_candidates"):
        load_settings(config_dir)
    change(config_dir, "retrieval", "max_candidates", 12)
    change(config_dir, "models", "unexpected", 42)
    with pytest.raises(SettingsError, match="models.yaml.*unexpected"):
        load_settings(config_dir)


def test_shared_recall_budget(config_dir):
    change(config_dir, "evaluation", "recall_at_k", [5, 9])
    with pytest.raises(SettingsError, match="evaluation.yaml recall_at_k must match retrieval.yaml recall_at_k"):
        load_settings(config_dir)


def test_invalid_yaml_does_not_echo_content(config_dir):
    (config_dir / "models.yaml").write_text("DEEPSEEK_API_KEY: [synthetic-secret", encoding="utf-8")
    with pytest.raises(SettingsError) as error:
        load_settings(config_dir)
    assert "synthetic-secret" not in str(error.value)
