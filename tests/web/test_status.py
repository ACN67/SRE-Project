"""Synthetic service failures exercise UI only, not build implementation."""
import logging

import pytest

from dynamic_memory.web import app as web_app
from dynamic_memory.web.demo import DemoService
from dynamic_memory.web.status import ERROR_MESSAGES, known_error_code
from test_empty_and_failure import launch, go


class ServiceFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__("private-token-and-source-text")


@pytest.mark.parametrize("code", list(ERROR_MESSAGES))
def test_known_failures_are_distinct_and_sanitized(monkeypatch, caplog, code):
    rendered = []
    monkeypatch.setattr(web_app.st, "error", rendered.append)
    with caplog.at_level(logging.ERROR):
        web_app.report_error("读取记忆列表", ServiceFailure(code))
    assert len(rendered) == 1
    assert ERROR_MESSAGES[code] in rendered[0]
    assert f"错误码：{code}" in rendered[0]
    assert "private-token" not in rendered[0] + caplog.text


@pytest.mark.parametrize("code", [None, "private-secret", ["DATA_NOT_LOADED"]])
def test_unknown_codes_are_not_exposed(code):
    assert known_error_code(ServiceFailure(code)) is None


def test_sample_load_failure_remains_refreshable(monkeypatch):
    service = DemoService()
    original = service.list_samples

    def fail():
        raise ServiceFailure("DATA_NOT_LOADED")

    service.list_samples = fail
    app = launch(monkeypatch, service)
    assert "数据未加载" in app.error[0].value
    assert not app.selectbox
    service.list_samples = original
    next(b for b in app.button if b.label == "刷新服务").click().run()
    go(app, "记忆浏览")
    assert not app.exception
    assert not app.error
    assert any(b.label == "查看详情" for b in app.button)


def test_failed_state_clears_selection_and_recovers_without_building(monkeypatch):
    service = DemoService()  # No build/index/query methods exist on this fixture.
    app = launch(monkeypatch, service)
    next(b for b in app.button if b.label == "查看详情").click().run()
    next(b for b in app.button if b.label.startswith("↗")).click().run()
    original = service.list_memories

    def fail(*args):
        raise ServiceFailure("ARTIFACT_STALE")

    service.list_memories = fail
    app.run()
    assert "selected_memory" not in app.session_state
    assert "selected_source" not in app.session_state
    assert not app.table
    assert "已过期" in app.error[0].value
    service.list_memories = original
    app.run()
    assert not app.exception
    assert not app.error
    assert "selected_source" not in app.session_state
    assert any(b.label == "查看详情" for b in app.button)
