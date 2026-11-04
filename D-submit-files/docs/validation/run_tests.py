"""Local runner: Anaconda's readline dylib crashes on import on this host.

Tests do not use terminal editing; make that optional interactive module absent.
This does not change production code or pytest behavior beyond its readline probe.
"""
import sys
sys.modules['readline'] = None
import pytest
raise SystemExit(pytest.main(sys.argv[1:]))
