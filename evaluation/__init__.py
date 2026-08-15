# -*- coding: utf-8 -*-
"""研究驗證邊界：IS、embargo、OS、相位掃描與後續 walk-forward 工具。"""

from .holdout import (
    HoldoutLedgerError,
    read_ledger,
    record_reveal,
    reveal_status,
    rules_fingerprint,
    verify_ledger,
)
from .phases import (
    PhaseSweep,
    phase_indices,
    phase_stats,
    sweep_phases,
)
from .splits import EvaluationSplit, build_evaluation_split

__all__ = [
    "EvaluationSplit",
    "HoldoutLedgerError",
    "PhaseSweep",
    "build_evaluation_split",
    "phase_indices",
    "phase_stats",
    "read_ledger",
    "record_reveal",
    "reveal_status",
    "rules_fingerprint",
    "sweep_phases",
    "verify_ledger",
]
