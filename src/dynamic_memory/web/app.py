"""Member E, increment 1: browse memories and inspect their original sources."""
from html import escape
import logging
import os
from pathlib import Path
from uuid import uuid4

import streamlit as st

from dynamic_memory.contracts.models import render_visible_turn
from .bindings import load_bindings
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
    reset_filter()
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
    st.markdown('<div class="memory-card"><span class="chip">' + escape(memory.fact_kind)
                + '</span> <span class="small-label">' + escape(when) + '</span><p>'
                + escape(memory.content) + '</p></div>', unsafe_allow_html=True)


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
    for memory in visible:
        memory_card(memory)
        if st.button("查看详情", key=f"detail_{memory.memory_id}"):
            clear_selection()
            st.session_state["selected_memory"] = memory.memory_id
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
        st.subheader("记忆详情")
        st.code(memory.memory_id, language=None)
        st.text(memory.content)
        st.table({"字段": ["实体", "属性", "值", "事实类型", "断言类型", "极性", "记录时间", "事件时间", "抽取版本"],
                  "值": [memory.entity or "未知", memory.attribute or "未知", memory.value or "未知",
                         memory.fact_kind, memory.assertion_type, memory.polarity,
                         str(memory.recorded_at) if memory.recorded_at else "未知",
                         memory.event_time.raw or "未知" if memory.event_time else "未知", memory.extractor_version]})
        with st.expander("完整公共契约对象 · schema 2.1"):
            st.json(memory.model_dump(mode="json"))
        for index, ref in enumerate(memory.source_refs):
            if st.button(f"↗ {ref.session_id} / {ref.dia_id}", key=f"source_{index}"):
                st.session_state["selected_source"] = ref
        st.markdown("[前往原文查看器 ↓](#source-inspector)")


def show_source(bindings, sample):
    st.subheader("原文查看器", anchor="source-inspector")
    ref = st.session_state.get("selected_source")
    if ref is None:
        st.info("在记忆详情中选择来源，查看对应的原始对话。")
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


def main():
    st.set_page_config(page_title="Memory Atlas · 记忆浏览", page_icon="◈", layout="wide")
    css = Path(__file__).with_name("assets").joinpath("theme.css").read_text(encoding="utf-8")
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)
    st.caption("DYNAMIC MEMORY / MEMORY ATLAS")
    st.title("让每一条记忆，都有据可循。")
    st.write("浏览结构化记忆，查看事实细节，回到原始对话。")
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
    with st.sidebar:
        st.header("Memory Atlas")
        st.text(bindings.label)
        st.caption("E · 第一期 / 只读浏览")
        if st.button("刷新服务", help="重新初始化当前会话的服务并清除已选记忆"):
            for key in ("bindings", "sample"):
                st.session_state.pop(key, None)
            reset_sample()
            st.rerun()
    if bindings.synthetic:
        st.warning("Synthetic 合成演示数据：仅用于界面与接口验证，不代表真实样本、模型输出或实验结果。")
    else:
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
    sample = st.sidebar.selectbox("样本", samples, key="sample", on_change=reset_sample)
    try:
        memories = bindings.list_memories(sample)
    except Exception as error:
        clear_selection()
        report_error("读取记忆列表", error)
        st.stop()
    browse(bindings, sample, memories)
    st.divider()
    show_source(bindings, sample)
