"""Minimal Streamlit entry for query-panel tests."""

import os

from dynamic_memory.web.app import report_error
from dynamic_memory.web.bindings import load_bindings
from dynamic_memory.web.query import query_panel


bindings = load_bindings(os.environ["DYNAMIC_MEMORY_WEB_FACTORY"])
query_panel(bindings, bindings.list_samples()[0], report_error)
