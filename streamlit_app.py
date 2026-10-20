"""Standalone entrypoint; system integration may call web.app.main directly."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from dynamic_memory.web.app import main

main()
