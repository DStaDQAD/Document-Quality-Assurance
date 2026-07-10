"""Accuracy evaluation harness for the paired PDF↔Excel fact-checker.

Two layers, because the pipeline mixes a deterministic comparison engine with a
non-deterministic LLM extraction step and measuring both with one number would be
misleading:

  Layer 1 (this package, run_comparison_eval.py): scores the deterministic comparison
    engine (paired_verifier._evaluate_fact) against hand-labelled cases. No API keys,
    fully reproducible, safe to run in CI.

  Layer 2 (run_e2e_eval.py, added later): scores the full pipeline including LLM
    extraction against document-level labels — extraction recall + verdict accuracy.
    Needs real providers, non-deterministic, run on demand.
"""
