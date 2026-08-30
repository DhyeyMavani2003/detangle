"""detangle — merge-conflict detection and CI for English-as-code.

Statically analyzes an AI agent's natural-language configuration
(CLAUDE.md / AGENTS.md hierarchies, skills, rules, subagents, tool and
MCP descriptions) and reports conflicting, contradictory, redundant,
shadowed, and precedence-ambiguous instructions — each with evidence
spans, a co-activation account, and a precedence account.
"""

__version__ = "0.1.0"
