"""Pluggable integrations (calendar, task source, todo source, notifier).

Each sub-module exposes one provider class plus any supporting types. The
orchestrator picks an implementation at startup based on config/env and
hands it to the solver + agent layer.
"""
