"""gravitee-stacker — an MCP server that manages the Gravitee Gamma demo stack.

Thin wrapper over ``docker/run.sh`` in the stack repo. It does not reimplement
orchestration; it invokes ``run.sh`` and surfaces status.
"""

__version__ = "0.1.0"
