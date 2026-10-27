"""C-only development retrieval diagnostics, separate from official QA scoring.

Labels are consumed only after retrieval, never passed to the retriever/analyzer.
Recall is conditional on relevant memories existing in the verified index.
"""

from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dynamic_memory.contracts.models import QueryRequest
from .index import fingerprint
from .query_analysis import analyze_query


class RetrievalEvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    case_id: str = Field(min_length=1)
    label_kind: Literal["synthetic_fixture", "team_annotated"]
    split: Literal["main_dev", "extra_dev"]
    request: QueryRequest
    source_manifest_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    relevant_memory_ids: list[str]
    annotator: str | None = None
    reviewer: str | None = None

    @model_validator(mode="after")
    def checked_labels(self):
        if len(self.relevant_memory_ids) != len(set(self.relevant_memory_ids)):
            raise ValueError("duplicate relevant IDs")
        if self.label_kind == "team_annotated" and not all(
                isinstance(name, str) and name.strip() for name in (self.annotator, self.reviewer)):
            raise ValueError("team labels require annotator and reviewer")
        return self


def _metrics(ids, relevant, budget, ks):
    hits = len(set(ids) & relevant)
    denominator = len(relevant)
    return {"returned_count": len(ids), "hits": hits, "relevant_count": denominator,
            "recall": hits / denominator if denominator else None,
            "recall_at_k": {str(k): (len(set(ids[:k]) & relevant) / denominator
                                      if denominator and k <= budget else None) for k in ks}}


def evaluate_retrieval(cases, *, indexes, retriever, recall_at_k=(5, 10), analyzer=analyze_query):
    """Run uncached VectorRetriever on prepared indexes, excluding build/load time.

    Fail fast on stale/unmapped labels and execution errors; never silently omit
    failed queries from a headline score. Use separate runs for label kinds.
    No official Gold loader or answer model is used. This function writes no files.
    """
    from .vector_retrieval import VectorRetriever
    if not isinstance(retriever, VectorRetriever):
        raise ValueError("use an uncached VectorRetriever, not SharedRetrieval")
    if not recall_at_k or any(type(k) is not int or k < 1 for k in recall_at_k):
        raise ValueError("positive recall cutoffs required")
    ks = sorted(set(recall_at_k))
    checked = [RetrievalEvaluationCase.model_validate_json(case.model_dump_json()) for case in cases]
    if not checked or len({c.case_id for c in checked}) != len(checked):
        raise ValueError("nonempty cases with unique case IDs required")
    if len({c.label_kind for c in checked}) != 1 or len({c.split for c in checked}) != 1:
        raise ValueError("do not mix label kinds or development splits in a report")
    # Validate the entire label set before doing model work.
    for case in checked:
        index = indexes[case.request.sample_id]
        if index.sample_id != case.request.sample_id or index.source_manifest_fingerprint != case.source_manifest_fingerprint:
            raise ValueError("annotation source is stale or belongs to another sample")
        if not set(case.relevant_memory_ids) <= set(index.memory_ids):
            raise ValueError("unmapped relevant IDs; resolve annotation mapping before measuring retrieval")
    rows = []
    for case in checked:
        index = indexes[case.request.sample_id]
        request = case.request.model_copy(deep=True)
        start = perf_counter()
        analysis = analyzer(request)
        snapshot = retriever.retrieve_with_trace(request, analysis, index)
        elapsed_ms = (perf_counter() - start) * 1000
        pure, final = snapshot.pure_vector, snapshot.candidates
        relevant = set(case.relevant_memory_ids)
        pure_ids = [item.memory_id for item in pure.items]
        final_ids = [item.memory_id for item in final.items]
        rows.append({"case_id": case.case_id, "sample_id": request.sample_id,
                     "query_fingerprint": snapshot.query_fingerprint,
                     "source_manifest_fingerprint": index.source_manifest_fingerprint,
                     "retriever_fingerprint": index.retriever_fingerprint,
                     "annotation_fingerprint": fingerprint(case.model_dump(mode="json")),
                     "relevant_count": len(relevant), "elapsed_ms": elapsed_ms,
                     "pure_vector": _metrics(pure_ids, relevant, pure.base_top_k, ks),
                     "final_candidates": _metrics(final_ids, relevant, final.max_candidates, ks),
                     "pure_vector_ids": pure_ids, "initial_topk_ids": list(snapshot.initial_topk_ids),
                     "expanded_ids": list(snapshot.expanded_ids), "final_candidate_ids": final_ids,
                     "expansion_budget_excluded_ids": list(snapshot.budget_excluded_ids)})
    scored = [row for row in rows if row["relevant_count"]]
    summary = {"query_count": len(rows), "scored_query_count": len(scored),
               "no_relevant_label_query_count": len(rows) - len(scored),
               "mean_elapsed_ms": sum(row["elapsed_ms"] for row in rows) / len(rows)}
    for method in ("pure_vector", "final_candidates"):
        total = sum(row["relevant_count"] for row in scored)
        summary[method] = {
            "hits": sum(row[method]["hits"] for row in scored), "relevant_count": total,
            "micro_recall": sum(row[method]["hits"] for row in scored) / total if total else None,
            "macro_recall": sum(row[method]["recall"] for row in scored) / len(scored) if scored else None}
    return {"report_version": 1, "label_kind": checked[0].label_kind, "split": checked[0].split,
            "metric_scope": "conditional_on_existing_index_memories", "official_qa_score": None,
            "timing_scope": "analysis_and_uncached_retrieval_including_identity_checks",
            "timing_note": "May include first query model loading; excludes index build/load and QA.",
            "unavailable_recall_note": "null when no relevant labels or cutoff exceeds method budget",
            "summary": summary, "queries": rows}
