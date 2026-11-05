"""A service can satisfy E's existing four browse calls without UI edits."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))

from dynamic_memory.web.bindings import WebBindings
from dynamic_memory.integration.production import ProductionWebService
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


def test_production_web_service_exposes_only_the_configured_samples():
    api = service()
    wrapped = ProductionWebService(api, (SAMPLE, "conv-26"))
    assert wrapped.list_samples() == [SAMPLE, "conv-26"]
    web = WebBindings(service=wrapped, synthetic=False, dynamic_available=True)
    assert web.list_samples() == [SAMPLE, "conv-26"]
    assert web.dynamic_available is True
    assert web.supports("run_query")
    assert web.list_memories(SAMPLE) == [memory()]
    assert web.get_memory(SAMPLE, memory().memory_id) == memory()
    assert web.get_turn(SAMPLE, REF).dia_id == REF.dia_id


def test_production_web_service_accepts_explicit_main_dev_sample():
    wrapped = ProductionWebService(service(), "conv-26")
    assert wrapped.list_samples() == ["conv-26"]
