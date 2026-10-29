"""Scoring / evidence formula version.

Bump ``FORMULA_VERSION`` whenever the evidence weights, grade thresholds or
the score-composition rules change.  Snapshots carry this value so two numbers
computed by different formulas are never compared as if they were the same
metric (a silent "score dropped 9 points" is worse than no delta at all).
"""

FORMULA_VERSION = "ev-2026.10"
