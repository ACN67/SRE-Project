"""Offline C delivery checks, including A integration in a temporary source overlay.

Does not merge branches, edit the checkout, run paid APIs or read official Gold.
Use only reviewed local A revisions: their Python source is executed in the probe.
"""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a-ref", required=True, help="Reviewed A commit available in local Git")
    parser.add_argument("--b-ref", help="Trusted B commit; also check strict disk artifact -> C index")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(["git", "rev-parse", "--verify", f"{args.a_ref}^{{commit}}"], cwd=root, text=True).strip()

    def run(*arguments):
        subprocess.run([sys.executable, *arguments], cwd=root, check=True)

    run("-m", "pytest", "-q", "-p", "no:cacheprovider")
    run("-m", "dynamic_memory.pipeline.mock_smoke")
    run("-m", "dynamic_memory.retrieval.evaluation_smoke")
    run("scripts/check_c_a_integration.py", "--a-ref", revision)
    # A added pure_vector_topk to its existing public QueryResponse. Test that
    # actual contract in an isolated process, without installing or editing it.
    with tempfile.TemporaryDirectory(prefix="acn-c-delivery-") as folder:
        overlay = Path(folder) / "src"
        overlay.mkdir()
        shutil.copytree(root / "configs", Path(folder) / "configs")
        shutil.copytree(root / "src" / "dynamic_memory", overlay / "dynamic_memory",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        model_source = subprocess.check_output(["git", "show", f"{revision}:src/dynamic_memory/contracts/models.py"], cwd=root)
        (overlay / "dynamic_memory" / "contracts" / "models.py").write_bytes(model_source)
        bootstrap = (
            "import runpy,sys; overlay,script=sys.argv[1:3]; "
            "sys.path.insert(0,overlay); sys.path.insert(0,str(__import__('pathlib').Path(script).parent)); "
            "sys.argv=sys.argv[2:]; runpy.run_path(script,run_name='__main__')"
        )
        run("-c", bootstrap, str(overlay), str(root / "scripts/check_c_service_integration.py"),
            "--a-ref", revision, "--check-ablation")
    if args.b_ref:
        run("scripts/check_c_b_integration.py", "--b-ref", args.b_ref)
    print(json.dumps({"c_delivery_checks": "PASS", "a_revision": revision,
                      "scope": "C unit/contract regression and synthetic integration",
                      "B_disk_loader_checked_with_synthetic_data": bool(args.b_ref),
                      "production_B_extraction": False, "real_D_reasoning": False,
                      "real_model_quality": "not_evaluated", "checkout_merged": False}, indent=2))


if __name__ == "__main__":
    main()
