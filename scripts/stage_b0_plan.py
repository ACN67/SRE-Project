"""Read-only planning on one actual conversation, with no model or Gold access."""

import json
from pathlib import Path

from dynamic_memory.data.loader import load_conversations
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.memory.extraction import build_extraction_request
from dynamic_memory.memory.windows import plan_extraction_windows
from dynamic_memory.pipeline.settings import load_settings

root = Path(__file__).resolve().parents[1]
data = root / "data" / "raw" / "locomo10.json"
settings = load_settings(models_profile=root / "configs" / "models_high_pilot.yaml")
bundle = load_conversations(str(data), str(root / "configs" / "data_split.yaml"))
repo = SourceRepository(bundle)
group = settings.data_split.groups.main_dev.sample_ids
assert group is not None and len(group) == 4
turn_counts = {sample_id: sum(len(s.turns) for s in repo.get_sample(sample_id).sessions) for sample_id in group}
sample_id = min(group, key=lambda value: (turn_counts[value], group.index(value)))
sample = repo.get_sample(sample_id)
windows = plan_extraction_windows(sample, settings.extraction, reserved_prompt_tokens=1800)
assert windows
total_refs = 0
max_estimated = 0
max_final_estimated = 0
for window in windows:
    request = build_extraction_request(window, settings.models)
    assert request.settings.thinking == "enabled" and request.settings.reasoning_effort == "high"
    assert request.model_id == "deepseek-flash" and request.output_mode == "json"
    assert window.estimated_total_tokens <= settings.extraction.window_max_input_tokens
    final_estimate = sum(len(message.content.encode("utf-8")) + 16 for message in request.messages) + 128
    assert final_estimate <= request.settings.max_input_tokens
    max_final_estimated = max(max_final_estimated, final_estimate)
    for ref in window.source_refs:
        repo.get_turn(sample_id, ref)
    total_refs += len(window.source_refs)
    max_estimated = max(max_estimated, window.estimated_total_tokens)
assert not set(sample.model_dump()) & {"qa", "answer", "evidence", "summary"}
print(json.dumps({"chosen_sample": sample_id, "main_dev_turn_counts": turn_counts,
                  "source_turns": turn_counts[sample_id], "session_count": len(sample.sessions),
                  "window_count": len(windows), "source_ref_occurrences": total_refs,
                  "window_max_input_tokens": settings.extraction.window_max_input_tokens,
                  "max_estimated_total_tokens": max_estimated,
                  "max_prompt_with_safety_estimate": max_final_estimated,
                  "reserved_prompt_tokens": 1800,
                  "max_output_tokens_per_window": settings.models.extraction_max_output_tokens,
                  "max_completion_tokens_if_all_windows_run": len(windows) * settings.models.extraction_max_output_tokens,
                  "no_llm_or_gold_used": True}))
