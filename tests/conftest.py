"""Pytest configuration for stock-chart-analyzer.

Adds the repository root to ``sys.path`` so that ``import common`` resolves
without needing an editable install. Place this file at ``tests/conftest.py``.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
