"""Strict extraction (docs/superpowers/specs/2026-10-26-strict-extraction-design.md).

The extractor sees each uploaded table's shape — codes, rows, periods, units, never the values —
and names the exact cells every claim needs, or says the data is not in the tables. The engine
reads those cells and computes: no word matching, no row guards. Behind STRICT_EXTRACTION.

tables   → the tables as the extractor sees them (codes, shape, exact row and period lookup)
claims   → the claim schema the extractor fills, and its serialisation for replays
extract  → the extraction prompt and the claims it returns
evaluate → one claim judged on its cited cells
pipeline → a whole check, handed over from paired_verifier.verify_paired
"""
