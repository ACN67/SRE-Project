"""Gold-free evaluation execution and persisted-run verification."""

from .three_method_runner import (RunProtocol, RunResult, run_three_method_batch,
                                  verify_run)

__all__ = ["RunProtocol", "RunResult", "run_three_method_batch", "verify_run"]
