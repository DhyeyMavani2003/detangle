"""Optional analysis lanes: the TypeSafe lane (calibrated pair judgments) and
the experimental LLM screen and jury.

The deterministic core never depends on these; each lane degrades to a
no-op with a note when its dependencies or credentials are missing.
"""
