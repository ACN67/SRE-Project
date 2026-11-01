from test_empty_and_failure import go, switch_sample
from datetime import date

from test_empty_and_failure import launch
from test_query import QueryService, submit


def test_home_has_service_controls_and_four_cards_without_loading_memories(monkeypatch):
    service = QueryService()
    def unexpected(*args):
        raise AssertionError("Home must not load memories")
    service.list_memories = unexpected
    app = launch(monkeypatch, service, page="首页")
    assert not app.exception
    assert not app.error
    assert not app.radio
    assert app.selectbox(key="sample_choice").value == "demo_caroline"
    assert len([button for button in app.button if button.label.startswith("进入")]) == 4
    assert any(button.label == "刷新服务" for button in app.button)
    go(app, "问答工作台")
    assert not app.error
    assert not any(item.key == "sample_choice" for item in app.selectbox)
    go(app, "首页")
    assert app.selectbox(key="sample_choice").value == "demo_caroline"


def test_pages_show_only_their_own_controls(monkeypatch):
    app = launch(monkeypatch, QueryService())
    assert app.text_input(key="search")
    assert not app.text_area
    go(app, "问答工作台")
    assert not app.text_input
    assert app.text_area(key="query_question")
    go(app, "原文回查")
    assert not app.text_area
    assert {item.label for item in app.text_input} == {"Session ID", "Dia ID"}
    go(app, "服务状态")
    assert not app.text_input
    assert not app.text_area
    assert not app.exception


def test_sidebar_and_home_cards_share_navigation_without_requery(monkeypatch):
    service = QueryService()
    app = launch(monkeypatch, service, page="首页")
    assert {button.label for button in app.sidebar.button} == {"首页", "记忆浏览", "问答工作台", "原文回查", "服务状态"}
    app.button(key="nav_问答工作台").click().run()
    submit(app)
    query_id = app.session_state["query_response"].query_id
    app.button(key="nav_首页").click().run()
    assert any(button.label == "进入问答工作台 →" for button in app.button)
    next(button for button in app.button if button.label == "进入问答工作台 →").click().run()
    assert not app.exception
    assert app.session_state["query_response"].query_id == query_id
    assert len(service.calls) == 1
    assert app.button(key="nav_问答工作台").proto.type == "primary"


def test_navigation_preserves_query_draft_method_and_response(monkeypatch):
    service = QueryService()
    app = launch(monkeypatch, service, page="问答工作台")
    app.selectbox(key="query_method").select("vector_rag").run()
    app.date_input(key="query_as_of").set_value(date(2023, 1, 1))
    submit(app)
    query_id = app.session_state["query_response"].query_id
    go(app, "记忆浏览")
    app.text_input(key="search").set_value("agency").run()
    go(app, "服务状态")
    go(app, "问答工作台")
    assert not app.exception
    assert app.selectbox(key="query_method").value == "vector_rag"
    assert app.date_input(key="query_as_of").value == date(2023, 1, 1)
    assert app.text_area(key="query_question").value == "synthetic question"
    assert app.session_state["query_response"].query_id == query_id
    assert len(service.calls) == 1
    go(app, "记忆浏览")
    assert app.text_input(key="search").value == "agency"


def test_other_pages_do_not_read_memory_list(monkeypatch):
    service = QueryService()
    calls = []
    original = service.list_memories
    def counted(sample):
        calls.append(sample)
        return original(sample)
    service.list_memories = counted
    app = launch(monkeypatch, service)
    count = len(calls)
    for page in ("问答工作台", "原文回查", "服务状态"):
        go(app, page)
    assert not app.exception
    assert len(calls) == count


def test_memory_detail_replaces_list_and_returns_to_same_filter_and_page(monkeypatch):
    service = QueryService()
    app = launch(monkeypatch, service)
    app.text_input(key="search").set_value("Studio").run()
    next(b for b in app.button if b.label == "查看详情").click().run()
    assert not app.exception
    assert not app.text_input
    assert not any(b.label == "查看详情" for b in app.button)
    assert app.table
    next(b for b in app.button if b.label == "← 返回记忆列表").click().run()
    assert app.text_input(key="search").value == "Studio"
    app.text_input(key="search").set_value("").run()
    app.selectbox(key="page").select(2).run()
    next(b for b in app.button if b.label == "查看详情").click().run()
    next(b for b in app.button if b.label == "← 返回记忆列表").click().run()
    assert not app.exception
    assert app.selectbox(key="page").value == 2
    assert not app.table
    assert service.calls == []


def test_bottom_pagination_syncs_with_selector_and_preserves_filter(monkeypatch):
    service = QueryService()
    app = launch(monkeypatch, service)
    assert app.button(key="previous_page").disabled
    assert not app.button(key="next_page").disabled
    first_page_ids = {button.key for button in app.button if button.key and button.key.startswith("detail_")}
    app.button(key="next_page").click().run()
    assert not app.exception
    assert app.selectbox(key="page").value == 2
    assert app.button(key="next_page").disabled
    assert not app.button(key="previous_page").disabled
    assert not first_page_ids.intersection(button.key for button in app.button if button.key and button.key.startswith("detail_"))
    next(b for b in app.button if b.label == "查看详情").click().run()
    next(b for b in app.button if b.label == "← 返回记忆列表").click().run()
    assert app.selectbox(key="page").value == 2
    app.button(key="previous_page").click().run()
    assert app.selectbox(key="page").value == 1
    app.selectbox(key="kind").select("state").run()
    app.button(key="next_page").click().run()
    assert app.selectbox(key="kind").value == "state"
    assert app.selectbox(key="page").value == 2
    app.text_input(key="search").set_value("larger apartment").run()
    assert app.selectbox(key="page").value == 1
    assert app.button(key="previous_page").disabled and app.button(key="next_page").disabled
    assert service.calls == []
