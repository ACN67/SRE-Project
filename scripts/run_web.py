"""Run from any working directory: python scripts/run_web.py [Streamlit flags]."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if __name__ == "__main__":
    from streamlit.web import cli

    sys.argv = ["streamlit", "run", str(ROOT / "streamlit_app.py"),
                "--server.address=127.0.0.1", "--server.headless=true", *sys.argv[1:]]
    raise SystemExit(cli.main())

