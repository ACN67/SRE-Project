"""Query UI consumes A's service; no query analysis, retrieval or generation."""
from uuid import uuid4

import streamlit as st

from dynamic_memory.contracts.models import QueryRequest


def clear_query():
    st.session_state.pop("query_response", None)
    st.session_state.pop("query_source", None)


def query_panel(bindings, sample, report_error):
    st.subheader("问答")
    if not bindings.supports("run_query"):
        clear_query()
        st.info("当前服务未提供问答接口，接入统一服务后可提问。")
        return
    methods = ["full_text", "vector_rag"]
    if bindings.dynamic_available:
        methods.append("dynamic")
    else:
        st.info("Dynamic 需要 D 状态解析模块；当前离线集成仅提供 Full Text 与 Vector RAG。")
    method = st.selectbox("问答方法", methods,
                          key="query_method", on_change=clear_query)
    with st.form("query_form"):
        question = st.text_area("问题", key="query_question")
        as_of = st.date_input("截至日期（可选）", value=None, key="query_as_of")
        submitted = st.form_submit_button("提交问题")
    if submitted:
        clear_query()
        if not question.strip():
            st.warning("请输入问题。")
        else:
            try:
                request = QueryRequest(query_id=f"interactive_{uuid4()}", sample_id=sample,
                                       question=question, as_of=as_of)
                with st.spinner("正在查询…"):
                    st.session_state["query_response"] = bindings.run_query(request, method)
            except Exception as error:
                report_error("执行问答", error)
    response = st.session_state.get("query_response")
    if response is None:
        return
    if response.sample_id != sample or response.method != method:
        clear_query()
        return
    st.caption(f"本次结果 · {response.query_id}")
    st.text(f"提交的问题：{response.question}")
    answer = response.answer_result
    labels = {"answered": "已回答", "no_evidence": "证据不足", "time_uncertain": "时间不确定",
              "unresolved_conflict": "存在未解决冲突", "error": "执行失败"}
    if answer.status == "answered":
        st.success(labels[answer.status])
    elif answer.status == "error":
        from .status import ERROR_MESSAGES
        st.error("执行失败：" + ERROR_MESSAGES.get(answer.error.code, "请检查服务端诊断记录。"))
    else:
        st.warning(labels[answer.status])
    if answer.answer is not None:
        st.text(answer.answer)
    if answer.uncertainty:
        st.text(answer.uncertainty)
    if response.query_analysis is not None:
        with st.expander("查询分析（服务返回）"):
            st.json(response.query_analysis.model_dump(mode="json"))
    if response.retrieved is None:
        st.caption("本次没有候选集合（Full Text 或提前退出）。")
    elif not response.retrieved.items:
        st.info("检索完成，候选集合为空。")
    else:
        st.write("候选记忆（服务返回顺序）")
        st.table([item.model_dump(mode="json") for item in response.retrieved.items])
    if response.resolution is not None:
        with st.expander("状态解析（服务返回）"):
            st.json(response.resolution.model_dump(mode="json"))
    st.write("实际使用的记忆 ID")
    st.text(", ".join(answer.used_memory_ids) or "无")
    # Only context sources can be selected; evidence is a validated subset.
    evidence = {(r.session_id, r.dia_id) for r in answer.evidence_refs}
    for index, ref in enumerate(answer.context_source_refs):
        label = "答案证据" if (ref.session_id, ref.dia_id) in evidence else "实际上下文"
        if st.button(f"{label} ↗ {ref.session_id} / {ref.dia_id}", key=f"query_ref_{index}"):
            st.session_state["query_source"] = ref
    ref = st.session_state.get("query_source")
    if ref is not None:
        try:
            from dynamic_memory.contracts.models import render_visible_turn
            turn = bindings.get_turn(sample, ref)
            st.text(turn.text)
            if turn.image_caption:
                st.caption(f"图片描述 · 来源：{turn.caption_origin}")
                st.text(turn.image_caption)
            with st.expander("问答来源统一可见文本"):
                st.text(render_visible_turn(turn))
        except Exception as error:
            st.session_state.pop("query_source", None)
            report_error("读取问答来源", error)
