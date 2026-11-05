"""Long-history synthetic source -> strict B -> C -> A -> evaluator -> Web."""

import json
from pathlib import Path

from streamlit.testing.v1 import AppTest

from dynamic_memory.contracts import QueryRequest
from dynamic_memory.data.loader import load_conversations
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.data.snapshot import freeze_split_config, inspect_snapshot
from dynamic_memory.integration.artifacts import ArtifactPorts
from dynamic_memory.integration.offline import (
    FixedEncoder, _builder, _index_settings, create_system, prepare_artifacts,
    prepare_indexes, prepare_source,
)
from dynamic_memory.pipeline.settings import load_settings
from dynamic_memory.retrieval import IndexManager, VectorRetriever, analyze_query
from evaluation.predictions import verify_predictions
from evaluation.question_manifest import QuestionManifest, load_question_manifest
from evaluation.runners.non_d_offline import run_non_d_offline


def test_twenty_memories_expand_and_survive_all_abce_consumers(tmp_path, monkeypatch):
    root = tmp_path / "long_history"
    prepare_source(root)
    source = root / "synthetic_locomo.json"
    samples = json.loads(source.read_text(encoding="utf-8"))
    samples[0]["conversation"] = {
        f"session_{i}": [{"dia_id": "d1", "speaker": "Ada",
                          "text": f"Ada kept notebook edition {i} in {2000 + i}."}]
        for i in range(1, 21)
    }
    samples[0]["qa"] = [
        {"question": "What does Ada keep now?", "answer": "Synthetic answer", "category": 1},
        {"question": "What did Ada keep in 2005?", "answer": "Synthetic answer", "category": 1},
    ]
    source.write_text(json.dumps(samples, sort_keys=True), encoding="utf-8")
    split = root / "split.yaml"
    split.write_text(
        "snapshot_sha256: null\nsample_ids: null\ngroups:\n"
        "  main_dev: {source_order_1_based: [1, 2, 3, 4], sample_ids: null}\n"
        "  extra_dev: {source_order_1_based: [5, 6, 7, 8], sample_ids: null}\n"
        "  test: {source_order_1_based: [9, 10], sample_ids: null}\n", encoding="utf-8")
    freeze_split_config(inspect_snapshot(source), split, apply=True)
    bundle = load_conversations(str(source), str(split))
    repository = SourceRepository(bundle)
    sample = repository.get_sample("synthetic-0")
    report = _builder(repository, load_settings(), sample).build_memories_with_report(sample)
    assert report.build_status == "complete" and report.successful_windows == 20
    assert len(report.memories) == 20
    prepare_artifacts(root, ("synthetic-0",))
    prepare_indexes(root, ("synthetic-0",))
    artifact = ArtifactPorts(root / "artifacts", bundle, load_settings()).load("synthetic-0")
    assert len(artifact.memories) == 20
    assert all(repository.get_turn("synthetic-0", ref).text == memory.content
               for memory in artifact.memories for ref in memory.source_refs)
    settings = _index_settings(load_settings())
    index = IndexManager(FixedEncoder()).load_index(root / "indexes" / "synthetic-0.json", artifact, settings)
    service, orchestrator = create_system(root)
    assert service.get_build_status("synthetic-0").memory_state == "complete"
    current = QueryRequest(query_id="current", sample_id="synthetic-0", question="What does Ada keep now?")
    historical = QueryRequest(query_id="historical", sample_id="synthetic-0",
                              question="What did Ada keep in 2005?")
    diagnostics = []
    for request, intent in ((current, "current_state"), (historical, "historical_state")):
        analysis = analyze_query(request)
        assert analysis.query_intent == intent
        trace = VectorRetriever(FixedEncoder(), settings).retrieve_with_trace(request, analysis, index)
        assert len(trace.initial_topk_ids) == 8
        assert len(trace.expanded_ids) == 4
        assert len(trace.budget_excluded_ids) == 8
        assert len(trace.final_candidate_ids) == 12
        assert trace.final_candidate_ids[:8] == trace.initial_topk_ids
        assert trace.final_candidate_ids[8:] == trace.expanded_ids
        assert len(set(trace.initial_topk_ids)) == 8
        assert all(item.origin == "history_expansion" for item in trace.candidates.items[8:])
        assert [item.rank for item in trace.candidates.items] == list(range(1, 13))
        vector = service.run_query(request, "vector_rag")
        pure = service.run_query(request, "pure_vector_topk")
        assert vector.answer_result.status == pure.answer_result.status == "answered"
        assert [item.memory_id for item in vector.retrieved.items] == list(trace.final_candidate_ids)
        # Pure-vector ablation deliberately keeps semantic order, while the
        # hybrid main path may reorder with lexical evidence.
        assert len(pure.retrieved.items) == settings.optional_pure_vector_top_k
        assert all(item.origin == "semantic" for item in pure.retrieved.items)
        for response in (vector, pure):
            used = set(response.answer_result.used_memory_ids)
            candidates = {item.memory_id for item in response.retrieved.items}
            assert used <= candidates
            assert all(service.get_memory("synthetic-0", mid).sample_id == "synthetic-0" for mid in used)
            contexts = {(ref.session_id, ref.dia_id) for ref in response.answer_result.context_source_refs}
            assert {(ref.session_id, ref.dia_id) for ref in response.answer_result.evidence_refs} <= contexts
            assert all(service.get_turn("synthetic-0", ref).text for ref in response.answer_result.evidence_refs)
        diagnostics.append({"query_id": request.query_id, "intent": analysis.query_intent,
            "initial_topk_ids": trace.initial_topk_ids, "expanded_ids": trace.expanded_ids,
            "budget_excluded_ids": trace.budget_excluded_ids,
            "final_candidate_ids": trace.final_candidate_ids,
            "vector_used_memory_ids": vector.answer_result.used_memory_ids,
            "pure_used_memory_ids": pure.answer_result.used_memory_ids})
    manifest = load_question_manifest(source, split, selected_splits=("main_dev",))
    two = QuestionManifest(manifest.snapshot_sha256,
                           tuple(row for row in manifest.records if row.sample_id == "synthetic-0"),
                           manifest.selected_splits, manifest.official_id_field)
    receipts = run_non_d_offline(two, service, orchestrator, tmp_path / "predictions")
    comparison = json.loads((tmp_path / "predictions" / "candidate_comparison.json").read_text(encoding="utf-8"))
    assert len(comparison) == 2
    assert all(len(row["initial_topk_ids"]) == 8 and len(row["expanded_ids"]) == 4
               and len(row["final_candidate_ids"]) == 12 for row in comparison)
    assert all(row["vector_context_excluded_ids"] == [] for row in comparison)
    for method in ("full_text", "vector_rag", "pure_vector_topk"):
        rows = verify_predictions(receipts[method].path, two, receipts[method])
        assert len(rows) == 2 and all(row.answer_result.status == "answered" for row in rows)
        if method == "vector_rag":
            assert all(len(row.retrieved.items) == 12 for row in rows)
        elif method == "pure_vector_topk":
            assert all(len(row.retrieved.items) == 8 for row in rows)
    monkeypatch.setenv("SRE_INTEGRATION_PROFILE", "offline_synthetic")
    monkeypatch.setenv("SRE_OFFLINE_STATE", str(root))
    monkeypatch.setenv("DYNAMIC_MEMORY_WEB_FACTORY", "dynamic_memory.integration.offline:create_web_bindings")
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[2] / "streamlit_app.py")
    ).run(timeout=30)
    assert not app.exception and any("Synthetic" in warning.value for warning in app.warning)
    next(button for button in app.button if button.label == "进入记忆浏览 →").click().run()
    assert not app.exception
    assert any("共 20 条记忆" in caption.value for caption in app.caption)
    next(button for button in app.button if button.label == "查看详情").click().run()
    next(button for button in app.button if button.label.startswith("↗ session_")).click().run()
    assert any("notebook edition" in text.value for text in app.text)
    next(button for button in app.button if button.label == "← 返回首页").click().run()
    next(button for button in app.button if button.label == "进入问答工作台 →").click().run()
    app.selectbox(key="query_method").select("vector_rag").run()
    app.text_area(key="query_question").set_value(current.question)
    next(button for button in app.button if button.label == "提交问题").click().run()
    assert not app.exception
    assert app.session_state["query_response"].answer_result.status == "answered"
    assert len(app.session_state["query_response"].retrieved.items) == 12
    (tmp_path / "long_history_trace.json").write_text(json.dumps({
        "snapshot_sha256": bundle.snapshot_sha256, "build_id": artifact.manifest.build_id,
        "index_fingerprint": index.index_fingerprint, "memory_count": len(artifact.memories),
        "queries": diagnostics, "prediction_sha256": {name: receipt.file_sha256
        for name, receipt in receipts.items()}}, sort_keys=True, indent=2), encoding="utf-8")
