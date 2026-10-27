"""A service can satisfy E's existing four browse calls without UI edits."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))

from dynamic_memory.web.bindings import WebBindings
from test_dynamic_memory_service import SAMPLE, REF, memory, service


def test_existing_web_bindings_accepts_one_synthetic_service():
    api = service()
    web = WebBindings(service=api, synthetic=True)
    assert web.list_samples() == [SAMPLE]
    records = web.list_memories(SAMPLE)
    assert len(records) == 1
    assert web.get_memory(SAMPLE, records[0].memory_id) == memory()
    assert web.get_turn(SAMPLE, REF).dia_id == REF.dia_id
    records[0].content = "changed in web"
    assert web.list_memories(SAMPLE)[0].content != "changed in web"
