"""Run on A's public types after merge; never duplicate an unavailable DTO."""
import pytest

from dynamic_memory.web.bindings import WebBindings
from dynamic_memory.web.demo import DemoService
from test_empty_and_failure import launch


@pytest.fixture
def status_type():
    return pytest.importorskip("dynamic_memory.contracts.service_status").BuildStatus


@pytest.mark.parametrize("state,code,label", [
    ("not_built", "BUILD_NOT_FOUND", "未构建"),
    ("partial", "ARTIFACT_INCOMPLETE", "部分构建"),
    ("failed", "MEMORY_BUILD_FAILED", "构建失败"),
    ("stale", "ARTIFACT_STALE", "产物过期"),
    ("corrupt", "ARTIFACT_CORRUPT", "产物损坏"),
    ("complete", None, "完整构建"),
])
def test_public_status_panel_and_no_fake_empty(monkeypatch, status_type, state, code, label):
    service = DemoService()
    status = status_type(dataset_state="ready", memory_state=state,
        partial_build=state in {"partial", "failed"},
        build_id=None if state == "not_built" else "synthetic-build", error_code=code)
    service.get_build_status = lambda sample: status
    if state != "complete":
        from test_status import ServiceFailure
        def unavailable(sample):
            raise ServiceFailure(code)
        service.list_memories = unavailable
    app = launch(monkeypatch, service)
    assert not app.exception
    assert any(label in item.value for item in app.markdown)
    assert not any("完整构建，零条" in item.value for item in app.success)


def test_public_status_complete_empty_and_build_change_clears_query(monkeypatch, status_type):
    from test_query import QueryService, submit
    service = QueryService()
    status = status_type(dataset_state="ready", memory_state="complete", partial_build=False, build_id="synthetic-1")
    service.get_build_status = lambda sample: status
    app = launch(monkeypatch, service)
    submit(app)
    status = status_type(dataset_state="ready", memory_state="complete", partial_build=False, build_id="synthetic-2")
    app.run()
    assert "query_response" not in app.session_state
    assert len(service.calls) == 1
    app.selectbox(key="sample").select("demo_empty").run()
    assert any("完整构建，零条 Memory" in item.value for item in app.success)


def test_real_a_service_status_and_source_without_memory(status_type):
    service_module = pytest.importorskip("dynamic_memory.services")
    from dynamic_memory.contracts import ConversationSample, ConversationSession, DatasetBundle
    from dynamic_memory.data.repository import SourceRepository
    source = DemoService().turns[("session_1", "D1:1")]
    bundle = DatasetBundle(snapshot_sha256="a" * 64,
        samples=[ConversationSample(sample_id=source.sample_id, source_order_1_based=1,
            sessions=[ConversationSession(session_id=source.session_id, turns=[source])])],
        split_by_sample_id={source.sample_id: "main_dev"})
    status = status_type(dataset_state="ready", memory_state="not_built", partial_build=False, error_code="BUILD_NOT_FOUND")
    service = service_module.DynamicMemoryService(sources=SourceRepository(bundle), build_status=lambda _: status)
    bindings = WebBindings(service)
    assert bindings.get_build_status(source.sample_id) == status
    from dynamic_memory.contracts import SourceRef, QueryRequest
    assert bindings.get_turn(source.sample_id, SourceRef(session_id=source.session_id, dia_id=source.dia_id)) == source
    with pytest.raises(service_module.ServiceError) as error:
        bindings.list_memories(source.sample_id)
    assert error.value.code == "BUILD_NOT_FOUND"
    response = bindings.run_query(QueryRequest(query_id="interactive_synthetic", sample_id=source.sample_id, question="synthetic"), "full_text")
    assert response.answer_result.status == "error"
    assert response.answer_result.error.code == "COMPONENT_UNAVAILABLE"
