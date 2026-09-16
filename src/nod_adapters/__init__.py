"""Adapters for everything outside the controller.

Every adapter sits behind a `typing.Protocol` in `nod_adapters.protocols`.
Concrete implementations land in the `assemblyai`, `llm` and `tts` subpackages.
"""

from __future__ import annotations

__all__ = ["protocols"]
