from pathlib import Path
import sys
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

from dynamic_memory.web.bindings import WebBindings
from dynamic_memory.web.demo import DemoService

ENTRY = Path(__file__).resolve().parents[2] / "streamlit_app.py"


def launch(monkeypatch, service):
    monkeypatch.setitem(sys.modules, "empty_factory", SimpleNamespace(create=lambda: WebBindings(service)))
    monkeypatch.setenv("DYNAMIC_MEMORY_WEB_FACTORY", "empty_factory:create")
    return AppTest.from_file(str(ENTRY)).run()


def test_no_samples(monkeypatch):
    app = launch(monkeypatch, SimpleNamespace(list_samples=lambda: []))
    assert not app.exception
    assert any("尚未提供样本" in item.value for item in app.info)


def test_list_failure_is_not_empty_state(monkeypatch):
    def fail(sample):
        raise RuntimeError("private connection string")
    app = launch(monkeypatch, SimpleNamespace(list_samples=lambda: ["sample"], list_memories=fail))
    assert not app.exception
    assert "读取记忆列表失败" in app.error[0].value
    assert "private" not in app.error[0].value
    assert not app.info


def test_detail_failure_clears_previous_source(monkeypatch):
    service = DemoService()
    app = launch(monkeypatch, service)
    next(b for b in app.button if b.label == "查看详情").click().run()
    next(b for b in app.button if b.label.startswith("↗")).click().run()
    def fail(*args):
        raise KeyError("removed")
    service.get_memory = fail
    app.run()
    assert not app.exception
    assert "读取记忆详情失败" in app.error[0].value
    assert "selected_source" not in app.session_state


def test_changing_factory_clears_old_state(monkeypatch):
    service = DemoService()
    app = launch(monkeypatch, service)
    next(b for b in app.button if b.label == "查看详情").click().run()
    monkeypatch.setenv("DYNAMIC_MEMORY_WEB_FACTORY", "missing_factory:create")
    app.run()
    assert not app.exception
    assert app.error
    assert "selected_memory" not in app.session_state
    assert "bindings" not in app.session_state
