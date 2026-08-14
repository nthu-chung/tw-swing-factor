# -*- coding: utf-8 -*-
"""第一階段套件搬遷的相容性回歸測試。"""
from __future__ import annotations

import unittest

import backtest
import chip_momentum_strategy
import evaluation_split
import factors
import operators
from evaluation import build_evaluation_split
from execution import detect_limit_lock, load_disposition_days
from factor_engine import PanelOps, attach_fields
from factor_engine.legacy_factors import compute_factors
from strategies.s19_chip_momentum import build_signal


class PackageMigrationCompatibilityTest(unittest.TestCase):
    def test_legacy_operator_path_points_to_new_implementation(self):
        self.assertIs(operators.PanelOps, PanelOps)
        self.assertIs(operators.attach_fields, attach_fields)

    def test_legacy_factor_and_strategy_paths_still_work(self):
        self.assertIs(factors.compute_factors, compute_factors)
        self.assertIs(chip_momentum_strategy.build_signal, build_signal)

    def test_legacy_evaluation_path_still_works(self):
        self.assertIs(evaluation_split.build_evaluation_split, build_evaluation_split)

    def test_backtest_uses_execution_tradability_boundary(self):
        self.assertIs(backtest._limit_lock, detect_limit_lock)
        self.assertIs(backtest._load_disposition_days, load_disposition_days)


if __name__ == "__main__":
    unittest.main()
