"""Measure a left hemisphere on decisions with known answers.

    hemisphere-harness eval                      the left from your settings, all scenarios
    hemisphere-harness eval --json report.json   also save every answer

The harness trusts the left's probabilities in two places: to let it commit a pick on its
own, and to let it gate options against the operator's policy. This measures both, on
scenarios whose best option and policy verdicts are known, asking the left exactly the
questions the harness would ask. See docs/evaluation.md.
"""

from .runner import EvalReport, Scenario, ScenarioResult, ScenarioSet, format_report, load_scenarios, run_eval

__all__ = ["EvalReport", "Scenario", "ScenarioResult", "ScenarioSet", "format_report", "load_scenarios", "run_eval"]
