"""Query UI consumes A's service; no query analysis, retrieval or generation."""
from time import perf_counter
from uuid import uuid4

import streamlit as st

from dynamic_memory.contracts.models import QueryRequest


def clear_query():
    st.session_state.pop("query_response", None)
    st.session_state.pop("query_source", None)
    st.session_state.pop("query_elapsed_seconds", None)
    st.session_state.pop("query_context_choice", None)


def used_memory_panel(bindings, sample, used_memory_ids):
    st.write("实际用于生成答案的记忆")
    if not used_memory_ids:
        st.caption("本次回答没有使用结构化 Memory；Full Text 直接使用原始对话。")
        return
    st.caption("以下是提交给答案模型的记忆上下文；并非答案逐句引用的判定。")
    for position, memory_id in enumerate(dict.fromkeys(used_memory_ids), 1):
        try:
            memory = bindings.get_memory(sample, memory_id)
        except Exception:
            st.warning(f"第 {position} 条已使用记忆暂时无法读取。")
            continue
        with st.container(border=True):
            st.markdown(
                f"**{position}. {memory.entity or '未标注实体'}"
                f" · {memory.attribute or memory.fact_kind}**"
            )
            st.code(memory.memory_id, language=None)
            st.text(memory.content)
            st.caption(
                f"事实类型：{memory.fact_kind} · "
                f"断言：{memory.assertion_type} · 极性：{memory.polarity} · "
                f"记录时间：{memory.recorded_at or '未知'}"
            )


def source_reference_panel(answer):
    """Keep long Full Text contexts inspectable without rendering every turn."""
    refs = answer.context_source_refs
    evidence = {(ref.session_id, ref.dia_id) for ref in answer.evidence_refs}
    if len(refs) > 20:
        st.caption(f"本次使用 {len(refs)} 条原始上下文，其中 {len(evidence)} 条标记为答案证据。")
        with st.expander("按需回查原始上下文"):
            choice = st.selectbox(
                "选择来源轮次",
                range(len(refs)),
                format_func=lambda index: (
                    f"{'答案证据' if (refs[index].session_id, refs[index].dia_id) in evidence else '实际上下文'}"
                    f" · {refs[index].session_id} / {refs[index].dia_id}"
                ),
                key="query_context_choice",
            )
            if st.button("查看选中原文", key="query_context_open"):
                st.session_state["query_source"] = refs[choice]
        return
    for index, ref in enumerate(refs):
        label = "答案证据" if (ref.session_id, ref.dia_id) in evidence else "实际上下文"
        if st.button(f"{label} ↗ {ref.session_id} / {ref.dia_id}", key=f"query_ref_{index}"):
            st.session_state["query_source"] = ref


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
    method_labels = {
        "full_text": "Full Text：发送完整可见对话",
        "vector_rag": "Vector RAG：本地检索候选后生成答案",
        "dynamic": "Dynamic：本地检索和状态筛选后生成答案",
    }
    st.caption(method_labels[method])
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
                started = perf_counter()
                with st.spinner(f"{method_labels[method]}，等待 DeepSeek 生成最终答案…"):
                    st.session_state["query_response"] = bindings.run_query(request, method)
                st.session_state["query_elapsed_seconds"] = perf_counter() - started
            except Exception as error:
                report_error("执行问答", error)
    response = st.session_state.get("query_response")
    if response is None:
        return
    if response.sample_id != sample or response.method != method:
        clear_query()
        return
    st.caption(f"本次结果 · {response.query_id}")
    elapsed = st.session_state.get("query_elapsed_seconds")
    if elapsed is not None:
        st.caption(f"本次端到端耗时：{elapsed:.2f} 秒")
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
        used_ids = set(answer.used_memory_ids)
        st.write("候选记忆（服务返回顺序）")
        st.table([
            {
                **item.model_dump(mode="json"),
                "used_in_answer": item.memory_id in used_ids,
            }
            for item in response.retrieved.items
        ])
    if response.resolution is not None:
        with st.expander("状态解析（服务返回）"):
            st.json(response.resolution.model_dump(mode="json"))
    used_memory_panel(bindings, sample, answer.used_memory_ids)
    # Only context sources can be selected; evidence is a validated subset.
    source_reference_panel(answer)
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
