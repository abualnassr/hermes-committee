"""hermes-committee core: pure logic with no Hermes imports (unit-testable with a fake LLM).

The Hermes-facing glue (tools, hooks, ctx.llm adapter, progress messages) lives in the
plugin's top-level ``__init__.py``.
"""

__version__ = "0.4.0"
