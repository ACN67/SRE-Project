"""Fixed synthetic browsing fixtures; no extraction, retrieval, or inference."""
from datetime import datetime, timedelta

from dynamic_memory.contracts.memory_id import compute_memory_id
from dynamic_memory.contracts.models import MemoryRecord, SourceRef, SourceTurn, TimeSpan
from .bindings import WebBindings


def make_data():
    # Migrated from the standalone E prototype. These are not LoCoMo records.
    facts = [
        ("2023-03-12", "I am researching adoption agencies.", "adoption_stage", "researching", "state", "fact", "positive", None),
        ("2023-06-18", "I applied to an adoption agency today.", "adoption_stage", "applied", "event", "fact", "positive", None),
        ("2023-10-20", "I passed the adoption agency interviews today!", "adoption_stage", "interviews_passed", "event", "fact", "positive", "A handwritten card with the words Congratulations."),
        (None, "I plan to move to a larger apartment.", "housing_plan", "move_to_larger_apartment", "state", "plan", "positive", None),
        ("2023-10-23", "I still work at North Studio today.", "employment", "North Studio", "state", "fact", "positive", None),
        ("2023-10-23", "I do not work at North Studio today.", "employment", "North Studio", "state", "fact", "negative", None),
        ("2023-11-01", "I left North Studio and now work at South Studio.", "employment", "South Studio", "state", "fact", "positive", None),
    ]
    memories, turns = [], {}
    for i, (day, text, attribute, value, kind, assertion, polarity, caption) in enumerate(facts, 1):
        ref = SourceRef(session_id=f"session_{i}", dia_id=f"D{i}:1")
        start = datetime.fromisoformat(day) if day else None
        turn = SourceTurn(sample_id="demo_caroline", **ref.model_dump(), speaker="Caroline",
                          text=text, recorded_at=start, image_caption=caption,
                          caption_origin="synthetic_caption" if caption else None)
        turns[(ref.session_id, ref.dia_id)] = turn
        draft = MemoryRecord(memory_id="mem_" + "0" * 20, sample_id=turn.sample_id,
                             content=text, entity="Caroline", attribute=attribute, value=value,
                             fact_kind=kind, assertion_type=assertion, polarity=polarity,
                             recorded_at=start, source_refs=[ref], extractor_version="synthetic-ui-v1",
                             event_time=TimeSpan(raw=day, start=start, end_exclusive=start + timedelta(days=1),
                                                 precision="day", resolution="explicit") if start else None)
        memories.append(MemoryRecord.model_validate({**draft.model_dump(), "memory_id": compute_memory_id(draft)}))
    return memories, turns


class DemoService:
    def __init__(self):
        self.memories, self.turns = make_data()

    def list_samples(self):
        return ["demo_caroline", "demo_empty"]

    def list_memories(self, sample_id):
        if sample_id not in self.list_samples():
            raise KeyError("Sample not found")
        return [m.model_copy(deep=True) for m in self.memories] if sample_id == "demo_caroline" else []

    def get_memory(self, sample_id, memory_id):
        for memory in self.list_memories(sample_id):
            if memory.memory_id == memory_id:
                return memory
        raise KeyError("Memory not found")

    def get_turn(self, sample_id, ref):
        if sample_id != "demo_caroline":
            raise KeyError("Source not found")
        return self.turns[(ref.session_id, ref.dia_id)].model_copy(deep=True)


def demo_bindings():
    return WebBindings(DemoService(), label="Synthetic · 合成演示数据", synthetic=True)
