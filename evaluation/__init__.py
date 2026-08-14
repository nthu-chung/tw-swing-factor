# -*- coding: utf-8 -*-
"""研究驗證邊界：IS、embargo、OS 與後續 walk-forward 工具。"""

from .splits import EvaluationSplit, build_evaluation_split

__all__ = ["EvaluationSplit", "build_evaluation_split"]
