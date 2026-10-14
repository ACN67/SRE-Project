"""Synthetic, offline contract smoke check. No business answer is produced."""

from dynamic_memory.contracts.models import (
    AnswerResult, CandidateSet, QueryAnalysis, QueryRequest, QueryResponse,
    SourceTurn, render_visible_turn,
)


def main() -> None:
    turn = SourceTurn(sample_id="synthetic_001", session_id="s1", dia_id="d1", speaker="Alex", text="I opened a shop.", image_caption="shop front", caption_origin="blip_caption")
    request = QueryRequest(query_id="synthetic_q1", sample_id=turn.sample_id, question="When did Alex open a shop?")
    analysis = QueryAnalysis(query_id=request.query_id, sample_id=request.sample_id, query_intent="event_fact", time_mode="latest_known")
    candidates = CandidateSet(query_id=request.query_id, sample_id=request.sample_id, base_top_k=8, max_candidates=12, retriever_fingerprint="synthetic_empty", items=[])
    response = QueryResponse(query_id=request.query_id, sample_id=request.sample_id, question=request.question, method="vector_rag", query_analysis=analysis, retrieved=candidates, resolution=None, answer_result=AnswerResult(status="no_evidence", uncertainty="Synthetic fixture contains no extracted Memory; no answer was generated."))
    assert render_visible_turn(turn) == "I opened a shop. [image_caption: shop front]"
    assert response.answer_result.status == "no_evidence"
    print("synthetic mock smoke: PASS (no API, data, or business answer)")


if __name__ == "__main__":
    main()
