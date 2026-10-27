"""A-share stock tracking and mechanical review reports.

This package deliberately lives beside ``fund_tracker`` and reuses its public
HTTP, macro, metrics and Feishu notification helpers.  Stock data, reports,
configuration and the launchd job are kept separate from the fund tracker.
"""

__version__ = "0.1.0"
