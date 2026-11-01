"""Member E, increment 1: browse memories and inspect their original sources."""
from html import escape
import logging
import os
from pathlib import Path
from uuid import uuid4

import streamlit as st

from dynamic_memory.contracts.models import render_visible_turn, SourceRef
from .bindings import load_bindings
from .bindings import InterfaceUnavailable
from .query import query_panel, clear_query
from .demo import demo_bindings
from .status import ERROR_MESSAGES, known_error_code

PAGE_SIZE = 4
LOG = logging.getLogger(__name__)


def clear_selection():
    for key in ("selected_memory", "selected_source"):
        st.session_state.pop(key, None)


def reset_filter():
    clear_selection()
    st.session_state["page"] = 1


def reset_sample():
    clear_query()
    reset_filter()
    st.session_state.pop("page_widgets", None)
    for key in ("search", "entity", "kind"):
        st.session_state.pop(key, None)


def report_error(context, error):
    incident = uuid4().hex[:10]
    LOG.error("Web %s incident=%s type=%s", context, incident, type(error).__name__)
    code = known_error_code(error)
    message = ERROR_MESSAGES[code] + f" 错误码：{code}。" if code else "请检查服务或数据契约。"
    st.error(f"{context}失败，{message}诊断号：{incident}")


def memory_card(memory):
    when = (memory.event_time.raw or "时间未知") if memory.event_time else "时间未知"
    st.markdown('<div class="memory-card"><div class="memory-card-meta"><span class="chip">'
                + escape(memory.fact_kind) + '</span><span class="small-label">' + escape(when)
                + '</span></div><div class="memory-card-title">' + escape(memory.entity or "未标注实体")
                + '</div><p>' + escape(memory.content) + '</p><div class="memory-card-footer"><span>'
                + str(len(memory.source_refs)) + ' 条原文来源</span><span>查看详情 ↗</span></div></div>',
                unsafe_allow_html=True)


def browse(bindings, sample, memories):
    left, middle, right = st.columns([2, 1, 1])
    search = left.text_input("搜索记忆内容", key="search", on_change=reset_filter,
                             placeholder="按内容、属性或值筛选")
    entities = sorted({m.entity for m in memories if m.entity is not None})
    entity = middle.selectbox("实体", [None, *entities], format_func=lambda x: "全部实体" if x is None else x,
                              key="entity", on_change=reset_filter)
    kind = right.selectbox("事实类型", ["全部", "state", "event", "unknown"], key="kind", on_change=reset_filter)
    needle = search.strip().casefold()
    filtered = [m for m in memories if (entity is None or m.entity == entity)
                and (kind == "全部" or m.fact_kind == kind)
                and needle in " ".join((m.content, m.attribute or "", m.value or "")).casefold()]
    st.caption(f"共 {len(memories)} 条记忆 · 筛选后 {len(filtered)} 条。页面筛选不调用检索或推理。")
    if not filtered:
        clear_selection()
        st.info("没有符合条件的记忆。" if memories else "当前样本尚无记忆。")
        return
    pages = (len(filtered) + PAGE_SIZE - 1) // PAGE_SIZE
    if st.session_state.get("page", 1) > pages:
        st.session_state["page"] = 1
    page = st.selectbox("页码", list(range(1, pages + 1)), key="page", on_change=clear_selection)
    visible = filtered[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    if st.session_state.get("selected_memory") not in {m.memory_id for m in visible}:
        clear_selection()
    for start in range(0, len(visible), 2):
        for column, memory in zip(st.columns(2), visible[start:start + 2]):
            with column, st.container(key=f"memory-tile-{memory.memory_id}"):
                memory_card(memory)
                # A native, focusable button covers the visual card. No JS or
                # user text is executed; the visual HTML remains escaped.
                st.button("查看详情", key=f"detail_{memory.memory_id}", on_click=open_memory,
                          args=(memory.memory_id,), use_container_width=True)
    with st.container(key="memory-pagination"):
        previous, summary, following = st.columns([1, 1, 1], vertical_alignment="center")
        previous.button("上一页", key="previous_page", disabled=page == 1,
                        on_click=change_page, args=(page - 1,), use_container_width=True)
        summary.markdown(f'<div class="pagination-summary">第 {page} / {pages} 页</div>', unsafe_allow_html=True)
        following.button("下一页", key="next_page", disabled=page == pages,
                         on_click=change_page, args=(page + 1,), use_container_width=True)


def change_page(page):
    clear_selection()
    st.session_state["page"] = page
    st.session_state.setdefault("page_widgets", {})["page"] = page


def open_memory(memory_id):
    remember_page_widgets()
    clear_selection()
    st.session_state["selected_memory"] = memory_id


def memory_detail(bindings, sample):
    st.button("← 返回记忆列表", on_click=return_to_list)
    selected = st.session_state.get("selected_memory")
    if selected:
        try:
            memory = bindings.get_memory(sample, selected)
        except Exception as error:
            clear_selection()
            report_error("读取记忆详情", error)
            return
        if st.session_state.get("selected_source") not in memory.source_refs:
            st.session_state.pop("selected_source", None)
        st.code(memory.memory_id, language=None)
        st.text(memory.content)
        st.table({"字段": ["实体", "属性", "值", "事实类型", "断言类型", "极性", "记录时间", "事件时间", "抽取版本"],
                  "值": [memory.entity or "未知", memory.attribute or "未知", memory.value or "未知",
                         memory.fact_kind, memory.assertion_type, memory.polarity,
                         str(memory.recorded_at) if memory.recorded_at else "未知",
                         memory.event_time.raw or "未知" if memory.event_time else "未知", memory.extractor_version]})
        with st.expander("完整公共契约对象 · schema 2.1"):
            st.json(memory.model_dump(mode="json"))
        st.subheader("原始对话")
        st.caption("点击下方来源，直接查看这条记忆所依据的原始发言。")
        for index, ref in enumerate(memory.source_refs):
            if st.button(f"↗ {ref.session_id} / {ref.dia_id}", key=f"source_{index}",
                         help="点击加载原文，内容直接显示在按钮下方"):
                st.session_state["selected_source"] = ref
        if st.session_state.get("selected_source") is not None:
            with st.container(border=True):
                show_source(bindings, sample)


def show_source(bindings, sample):
    ref = st.session_state.get("selected_source")
    if ref is None:
        return
    try:
        turn = bindings.get_turn(sample, ref)
    except Exception as error:
        st.session_state.pop("selected_source", None)
        report_error("读取原文", error)
        return
    st.caption(f"{turn.sample_id} / {turn.session_id} / {turn.dia_id} · {turn.speaker}")
    st.caption(f"记录时间：{turn.recorded_at.isoformat() if turn.recorded_at else '未知'}")
    st.text(turn.text)
    if turn.image_caption:
        st.caption(f"图片描述 · 来源：{turn.caption_origin}（独立于原始 text）")
        st.text(turn.image_caption)
    with st.expander("统一可见文本"):
        st.text(render_visible_turn(turn))


def independent_source(bindings, sample):
    st.caption("填写当前样本的轮次标识即可查看原文，无需先构建记忆。")
    with st.container(border=True):
        with st.form("source_lookup"):
            session = st.text_input("Session ID")
            dia = st.text_input("Dia ID")
            submitted = st.form_submit_button("回查原文")
        if submitted:
            try:
                if not session.strip() or not dia.strip():
                    st.warning("请输入 Session ID 和 Dia ID。")
                else:
                    turn = bindings.get_turn(sample, SourceRef(session_id=session.strip(), dia_id=dia.strip()))
                    st.text(turn.text)
                    if turn.image_caption:
                        st.caption(f"图片描述 · 来源：{turn.caption_origin}")
                        st.text(turn.image_caption)
                    st.text(render_visible_turn(turn))
            except Exception as error:
                report_error("回查原文", error)


def build_panel(bindings, sample, *, details=False):
    if not bindings.supports("get_build_status"):
        if details:
            st.info("当前服务尚未提供构建状态接口。")
        return None
    try:
        status = bindings.get_build_status(sample)
        labels = {"not_built": "未构建", "complete": "完整构建", "partial": "部分构建",
                  "failed": "构建失败", "stale": "产物过期", "corrupt": "产物损坏"}
        if details:
            left, right = st.columns(2)
            left.metric("数据状态", {"ready": "已加载", "unloaded": "未加载", "load_error": "加载失败"}[status.dataset_state])
            right.metric("记忆状态", labels[status.memory_state])
            if status.build_id:
                st.text(f"构建 ID：{status.build_id}")
            if status.error_code:
                st.warning(ERROR_MESSAGES.get(status.error_code, "服务报告未就绪状态。"))
        identity = (sample, status.dataset_state, status.memory_state, status.build_id, status.error_code)
        if st.session_state.get("build_identity") != identity:
            clear_selection()
            clear_query()
            st.session_state["build_identity"] = identity
        return status
    except InterfaceUnavailable:
        if details:
            st.info("构建状态公共接口尚未合并，暂不能展示构建状态。")
    except Exception as error:
        identity = (sample, "status_error", known_error_code(error), type(error).__name__)
        if st.session_state.get("build_identity") != identity:
            clear_selection()
            clear_query()
            st.session_state["build_identity"] = identity
        if details:
            report_error("读取构建状态", error)
    return None


PAGE_COPY = {
    "记忆浏览": ("让每一条记忆，都有据可循。", "浏览结构化记忆，查看事实细节，回到原始对话。"),
    "问答工作台": ("带着问题，寻找有据的回答。", "选择回答方法，提交问题，再沿着证据回到原文。"),
    "原文回查": ("回到对话发生的那一刻。", "用会话与轮次标识定位原始发言，区分原文与图片描述。"),
    "服务状态": ("了解数据当前的准备情况。", "查看数据与记忆构建状态；页面操作不会触发构建。"),
}
PAGE_WIDGETS = ("search", "entity", "kind", "page", "query_method", "query_question", "query_as_of")


def remember_page_widgets():
    saved = st.session_state.setdefault("page_widgets", {})
    saved.update({key: st.session_state[key] for key in PAGE_WIDGETS if key in st.session_state})


def navigate(section):
    remember_page_widgets()
    st.session_state["workspace_page"] = section


def return_to_list():
    clear_selection()
    for key in ("search", "entity", "kind", "page"):
        if key in st.session_state.get("page_widgets", {}):
            st.session_state[key] = st.session_state["page_widgets"][key]


def choose_sample():
    sample = st.session_state["sample_choice"]
    reset_sample()
    st.session_state["sample"] = sample


def refresh_service():
    reset_sample()
    for key in ("bindings", "sample", "sample_choice", "build_identity"):
        st.session_state.pop(key, None)


def home_cards():
    st.subheader("选择工作区")
    descriptions = {
        "记忆浏览": "浏览记忆、筛选事实，查看详情与原始来源。",
        "问答工作台": "选择回答方法，提交问题，检查答案与证据。",
        "原文回查": "通过会话与轮次标识，直接定位原始对话。",
        "服务状态": "了解数据、记忆构建状态与接口准备情况。",
    }
    for row in (("记忆浏览", "问答工作台"), ("原文回查", "服务状态")):
        for column, section in zip(st.columns(2), row):
            with column, st.container(border=True, key=f"home-card-{list(PAGE_COPY).index(section)}"):
                st.caption(f"0{list(PAGE_COPY).index(section) + 1} / MEMORY ATLAS")
                st.subheader(section)
                st.write(descriptions[section])
                st.button(f"进入{section} →", on_click=navigate, args=(section,), use_container_width=True)


def sidebar_navigation(section):
    with st.sidebar:
        st.markdown('<div class="sidebar-brand">Memory Atlas</div>', unsafe_allow_html=True)
        st.caption("记忆与证据工作台")
        with st.container(key="side-navigation"):
            for label, icon in (("首页", "home"), ("记忆浏览", "auto_stories"),
                                ("问答工作台", "forum"), ("原文回查", "manage_search"),
                                ("服务状态", "monitor_heart")):
                st.button(label, icon=f":material/{icon}:", key=f"nav_{label}",
                          type="primary" if section == label else "secondary",
                          use_container_width=True, on_click=navigate, args=(label,))
        st.divider()
        st.caption("样本与服务设置位于首页")


def main():
    st.set_page_config(page_title="Memory Atlas", page_icon="◈", layout="wide")
    css = Path(__file__).with_name("assets").joinpath("theme.css").read_text(encoding="utf-8")
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)
    factory = os.environ.get("DYNAMIC_MEMORY_WEB_FACTORY", "").strip()
    if st.session_state.get("factory") != factory:
        reset_sample()
        st.session_state.pop("bindings", None)
        st.session_state.pop("sample", None)
        st.session_state["factory"] = factory
    try:
        if "bindings" not in st.session_state:
            st.session_state["bindings"] = load_bindings(factory) if factory else demo_bindings()
        bindings = st.session_state["bindings"]
    except Exception as error:
        clear_selection()
        report_error("初始化服务", error)
        st.stop()
    section = st.session_state.get("workspace_page", "首页")
    sidebar_navigation(section)
    if section != "首页":
        st.button("← 返回首页", on_click=navigate, args=("首页",))
    title, description = PAGE_COPY.get(section, ("Memory Atlas · 记忆工作台", "从一条记忆出发，浏览事实、提出问题，沿证据回到对话。"))
    if section == "记忆浏览" and st.session_state.get("selected_memory"):
        title, description = "记忆详情", "查看事实字段与来源，或返回刚才的记忆列表。"
    st.markdown('<div class="brand">DYNAMIC MEMORY / MEMORY ATLAS</div>', unsafe_allow_html=True)
    st.title(title)
    st.write(description)
    if section == "首页":
        service_panel = st.container(border=True, key="home-service", width=900)
        with service_panel:
            left, sample_control, right = st.columns([2.2, 2, 1], vertical_alignment="center")
            left.caption("当前服务")
            left.text(bindings.label)
            right.button("刷新服务", on_click=refresh_service, help="重新初始化当前会话并清除旧结果")
    if bindings.synthetic:
        st.warning("Synthetic 合成演示数据：仅用于界面与接口验证，不代表真实样本、模型输出或实验结果。")
    elif section == "服务状态" and not bindings.supports("get_build_status"):
        st.caption("当前仅展示只读接口返回结果；构建状态接口尚未接入，不据此判断构建是否就绪。")
    try:
        samples = bindings.list_samples()
    except Exception as error:
        clear_selection()
        report_error("读取样本列表", error)
        st.stop()
    if not samples:
        clear_selection()
        st.info("服务尚未提供样本：样本列表读取成功，返回为空。")
        st.stop()
    if st.session_state.get("sample") not in samples:
        reset_sample()
        st.session_state["sample"] = samples[0]
    sample = st.session_state["sample"]
    if section == "首页":
        st.session_state["sample_choice"] = sample
        sample_control.selectbox("样本", samples, key="sample_choice", on_change=choose_sample)
        home_cards()
        return
    st.caption(f"当前样本：{sample}")
    for key, value in st.session_state.get("page_widgets", {}).items():
        if key not in st.session_state:
            st.session_state[key] = value
    status = build_panel(bindings, sample, details=section == "服务状态")
    if section == "问答工作台":
        query_panel(bindings, sample, report_error)
        return
    if section == "原文回查":
        independent_source(bindings, sample)
        return
    if section == "服务状态":
        st.subheader("接口连接")
        st.table({"能力": ["记忆浏览", "构建状态", "问答"],
                  "接口": ["已接入", "已提供" if bindings.supports("get_build_status") else "未提供",
                           "已提供" if bindings.supports("run_query") else "未提供"]})
        st.caption("接口已提供表示服务具有相应方法，实际可用性仍取决于数据、索引和业务组件。")
        return
    if st.session_state.get("selected_memory"):
        memory_detail(bindings, sample)
        return
    try:
        memories = bindings.list_memories(sample)
    except Exception as error:
        clear_selection()
        report_error("读取记忆列表", error)
    else:
        if status is not None and status.dataset_state == "ready" and status.memory_state == "complete":
            st.success("已就绪" if memories else "完整构建，零条 Memory。")
        browse(bindings, sample, memories)
