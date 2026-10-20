from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from dynamic_memory.contracts.models import render_visible_turn
from dynamic_memory.web.bindings import WebBindings
from dynamic_memory.web.demo import DemoService

ENTRY = Path(__file__).resolve().parents[2] / "streamlit_app.py"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.delenv("DYNAMIC_MEMORY_WEB_FACTORY", raising=False)
    result = AppTest.from_file(str(ENTRY)).run()
    assert not result.exception
    return result


def click(app, label):
    next(button for button in app.button if button.label == label).click().run()
    assert not app.exception
    return app


def test_initial_browse_is_visibly_synthetic(app):
    assert "Synthetic" in app.warning[0].value
    assert len([b for b in app.button if b.label == "查看详情"]) == 4
    assert any("时间未知" in m.value for m in app.markdown)
    assert not app.error


def test_details_and_caption_use_shared_renderer(app):
    [b for b in app.button if b.label == "查看详情"][2].click().run()
    click(app, "↗ session_3 / D3:1")
    turn = DemoService().turns[("session_3", "D3:1")]
    assert turn.text in [t.value for t in app.text]
    assert turn.image_caption in [t.value for t in app.text]
    assert render_visible_turn(turn) in [t.value for t in app.text]


def test_null_time_details(app):
    [b for b in app.button if b.label == "查看详情"][3].click().run()
    assert not app.exception
    assert app.table[0].value.iloc[7]["值"] == "未知"


@pytest.mark.parametrize("action", ["search", "kind", "page", "sample"])
def test_navigation_clears_stale_details_and_source(app, action):
    click(app, "查看详情")
    click(app, "↗ session_1 / D1:1")
    if action == "search":
        app.text_input(key="search").set_value("no matches").run()
    elif action == "kind":
        app.selectbox(key="kind").select("event").run()
    elif action == "page":
        app.selectbox(key="page").select(2).run()
    else:
        app.selectbox(key="sample").select("demo_empty").run()
    assert not app.exception
    assert "selected_source" not in app.session_state
    assert "selected_memory" not in app.session_state
    assert not app.table


def test_selecting_another_memory_clears_source(app):
    click(app, "查看详情")
    click(app, "↗ session_1 / D1:1")
    [b for b in app.button if b.label == "查看详情"][1].click().run()
    assert "selected_source" not in app.session_state


def test_factory_failure_never_falls_back_to_demo(monkeypatch):
    monkeypatch.setenv("DYNAMIC_MEMORY_WEB_FACTORY", "missing_web_factory:create")
    app = AppTest.from_file(str(ENTRY)).run()
    assert not app.exception
    assert app.error
    assert not app.warning
    assert not app.selectbox


def test_live_service_is_session_scoped_readonly_and_escapes_html(monkeypatch):
    service = DemoService()
    service.memories[0].content = '<script>alert("x")</script>'
    calls = []
    def factory():
        calls.append("create")
        return WebBindings(service)
    # No run_query method exists: all UI interactions must remain read-only.
    monkeypatch.setitem(sys.modules, "test_app_factory", SimpleNamespace(create=factory))
    monkeypatch.setenv("DYNAMIC_MEMORY_WEB_FACTORY", "test_app_factory:create")
    app = AppTest.from_file(str(ENTRY)).run()
    assert not app.warning
    cards = "".join(m.value for m in app.markdown)
    assert "&lt;script&gt;" in cards and "<script>" not in cards
    click(app, "查看详情")
    click(app, "↗ session_1 / D1:1")
    assert calls == ["create"]
    click(app, "刷新服务")
    assert calls == ["create", "create"]
    assert "selected_source" not in app.session_state


def test_source_error_is_visible_without_leaking_exception(app):
    click(app, "查看详情")
    def fail(*args):
        raise RuntimeError("secret-token-should-not-appear")
    app.session_state["bindings"].service.get_turn = fail
    click(app, "↗ session_1 / D1:1")
    assert "读取原文失败" in app.error[0].value
    assert "secret-token" not in app.error[0].value
    assert "selected_source" not in app.session_state


def test_updated_memory_sources_clear_old_source_before_lookup(app):
    click(app, "查看详情")
    click(app, "↗ session_1 / D1:1")
    service = app.session_state["bindings"].service
    service.memories[0].source_refs = service.memories[1].source_refs

    def unexpected_lookup(*args):
        raise AssertionError("Old source must not be fetched")

    service.get_turn = unexpected_lookup
    app.run()
    assert not app.exception
    assert not app.error
    assert "selected_source" not in app.session_state
    assert "I am researching adoption agencies." not in [t.value for t in app.text][1:]


@pytest.mark.parametrize("index, expected", [(0, "2023-03-12T00:00:00"), (3, "未知")])
def test_source_recorded_time_is_displayed_without_inference(app, index, expected):
    [b for b in app.button if b.label == "查看详情"][index].click().run()
    next(b for b in app.button if b.label.startswith("↗")).click().run()
    assert not app.exception
    assert f"记录时间：{expected}" in [c.value for c in app.caption]
