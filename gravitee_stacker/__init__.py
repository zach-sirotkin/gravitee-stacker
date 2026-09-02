"""gravitee-stacker — an MCP server that stands up and manages local Gravitee stacks.

Manages the Gravitee Gamma demo stack (a thin wrapper over ``docker/run.sh``), a
self-contained standalone APIM stack (incl. a native-Kafka variant) and AM stack, and
a generic runner for any official ``docker/quick-setup/*`` config. Everything runs via
Docker Compose; bring-ups are backgrounded and surfaced through two-signal status.
"""

__version__ = "0.10.0"
