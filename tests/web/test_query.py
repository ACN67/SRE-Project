from test_empty_and_failure import go, switch_sample
from datetime import date

import pytest

from dynamic_memory.contracts.models import (
    AnswerResult, CandidateItem, CandidateSet, MemoryDecision, QueryAnalysis, QueryRequest, QueryResponse,
    ResolutionResult, SourceRef, TimeIssue,
)
from dynamic_memory.web.bindings import ContractError, WebBindings
from dynamic_memory.web.demo import DemoService
from test_empty_and_failure import launch as launch_page


def launch(monkeypatch, service):
    return launch_page(monkeypatch, service, page="问答工作台")


class QueryService(DemoService):
    def __init__(self, status="answered"):
        super().__init__()
        self.calls = []
        self.status = status

    def run_query(self, request, method="dynamic"):
        assert isinstance(request, QueryRequest)
        self.calls.append((request, method))
        analysis = QueryAnalysis(query_id=request.query_id, sample_id=request.sample_id,
                                 query_intent="general_fact", time_mode="latest_known")
        early = self.status in {"error", "time_uncertain"}
        if self.status == "time_uncertain":
            analysis.time_mode = "conflict"
            analysis.time_issue = TimeIssue(code="QUERY_TIME_CONFLICT", message="synthetic conflict",
                explicit_as_of="2023-01-01", question_time="2024-01-01")
        ref = SourceRef(session_id="session_1", dia_id="D1:1")
        if self.status == "error":
            from dynamic_memory.contracts.models import ErrorInfo
            answer = AnswerResult(status="error", error=ErrorInfo(code="COMPONENT_UNAVAILABLE", message="secret-provider-token"))
        else:
            answer = AnswerResult(status=self.status, answer="Synthetic answer" if self.status == "answered" else None,
                uncertainty="synthetic reason" if self.status != "answered" else None,
                context_source_refs=[ref] if method == "full_text" and not early else [],
                evidence_refs=[ref] if method == "full_text" and not early else [])
        retrieved = CandidateSet(query_id=request.query_id, sample_id=request.sample_id,
            base_top_k=1, max_candidates=1, retriever_fingerprint="synthetic") if method != "full_text" and not early else None
        resolution = ResolutionResult(query_id=request.query_id) if method == "dynamic" and not early else None
        if method != "full_text" and self.status == "answered":
            memory = self.memories[0]
            retrieved.items = [CandidateItem(memory_id=memory.memory_id, rank=1, score=1.0,
                source_refs=memory.source_refs, origin="semantic")]
            answer = AnswerResult(status="answered", answer="Synthetic answer",
                used_memory_ids=[memory.memory_id], context_source_refs=memory.source_refs,
                evidence_refs=memory.source_refs)
            if resolution is not None:
                resolution.decisions = [MemoryDecision(memory_id=memory.memory_id, action="RETAIN", reason="synthetic")]
                resolution.effective_memory_ids = [memory.memory_id]
        return QueryResponse(query_id=request.query_id, sample_id=request.sample_id, question=request.question,
            method=method, query_analysis=analysis, retrieved=retrieved, resolution=resolution, answer_result=answer)


def submit(app):
    app.text_area(key="query_question").set_value("synthetic question")
    next(b for b in app.button if b.label == "提交问题").click().run()
    assert not app.exception


@pytest.mark.parametrize("method", ["full_text", "vector_rag", "dynamic"])
def test_submit_once_and_navigation_never_requeries(monkeypatch, method):
    service = QueryService()
    app = launch(monkeypatch, service)
    app.selectbox(key="query_method").select(method).run()
    app.date_input(key="query_as_of").set_value(date(2023, 1, 1))
    submit(app)
    request = service.calls[0][0]
    assert request.query_id.startswith("interactive_")
    assert request.as_of == date(2023, 1, 1)
    app.run()
    go(app, "记忆浏览")
    app.selectbox(key="page").select(2).run()
    go(app, "问答工作台")
    assert len(service.calls) == 1
    if method == "full_text":
        next(b for b in app.button if b.label.startswith("答案证据 ↗")).click().run()
        assert len(service.calls) == 1
        assert service.memories[0].content in [t.value for t in app.text]
    switch_sample(app, "demo_empty")
    assert "query_response" not in app.session_state
    assert "query_source" not in app.session_state


@pytest.mark.parametrize("status,label", [("answered", "已回答"), ("no_evidence", "证据不足"),
    ("time_uncertain", "时间不确定"), ("unresolved_conflict", "存在未解决冲突"), ("error", "执行失败")])
def test_answer_statuses(monkeypatch, status, label):
    app = launch(monkeypatch, QueryService(status))
    submit(app)
    assert any(label in x.value for x in [*app.success, *app.warning, *app.error])
    assert "secret-provider-token" not in "".join(x.value for x in app.error)


def test_method_switch_clears_response_and_new_submission_has_new_id(monkeypatch):
    service = QueryService()
    app = launch(monkeypatch, service)
    submit(app)
    app.selectbox(key="query_method").select("vector_rag").run()
    assert "query_response" not in app.session_state
    submit(app)
    assert service.calls[0][0].query_id != service.calls[1][0].query_id


def test_identity_mismatch_rejected():
    service = QueryService()
    original = service.run_query
    def wrong(request, method):
        result = original(request, method)
        result.sample_id = "other"
        return result
    service.run_query = wrong
    with pytest.raises((ContractError, ValueError)):
        WebBindings(service).run_query(QueryRequest(query_id="q", sample_id="demo_caroline", question="q"), "full_text")


def test_source_lookup_works_when_memory_unavailable(monkeypatch):
    service = DemoService()
    def fail(*args):
        raise RuntimeError("unavailable")
    service.list_memories = fail
    app = launch_page(monkeypatch, service, page="原文回查")
    next(x for x in app.text_input if x.label == "Session ID").set_value("session_1")
    next(x for x in app.text_input if x.label == "Dia ID").set_value("D1:1")
    next(b for b in app.button if b.label == "回查原文").click().run()
    assert not app.exception
    assert service.memories[0].content in [x.value for x in app.text]


def test_full_text_can_submit_when_memory_unavailable(monkeypatch):
    service = QueryService()
    service.list_memories = lambda _: (_ for _ in ()).throw(RuntimeError("no build"))
    app = launch(monkeypatch, service)
    submit(app)
    assert any("已回答" in x.value for x in app.success)


@pytest.mark.parametrize("method", ["vector_rag", "dynamic"])
def test_empty_candidates_are_not_null(monkeypatch, method):
    app = launch(monkeypatch, QueryService("no_evidence"))
    app.selectbox(key="query_method").select(method).run()
    submit(app)
    assert any("候选集合为空" in x.value for x in app.info)
    assert app.session_state["query_response"].retrieved is not None


def test_reject_wrong_source_before_storing_query_result(monkeypatch):
    service = QueryService()
    service.get_turn = lambda *_: next(iter(service.turns.values())).model_copy(update={"sample_id": "other"})
    app = launch(monkeypatch, service)
    submit(app)
    assert "query_response" not in app.session_state
    assert any("执行问答失败" in x.value for x in app.error)
