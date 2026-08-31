"""Optional analysis lanes: NLI cross-encoder and LLM jury.

The deterministic core never depends on these; each lane degrades to a
no-op with a warning when its dependencies or credentials are missing.
"""
