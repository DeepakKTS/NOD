"""Corpus manifests and the Track A generator.

BENCH_SPEC.md §2. Source clips are CC-licensed read speech listed in a manifest
with license fields; Track B ships a download script and a manifest, never audio.

CLI: `python -m nod_bench.corpus build --seed 7 --out data/corpus/trackA`
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


class SourceClip(BaseModel):
    """One licensed source clip (BENCH_SPEC.md §2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    corpus: str
    license: str
    file: Path
    sha256: str


class CorpusManifest(BaseModel):
    """The manifest for one track.

    A corpus file that changed since the manifest fails the run loudly (EC-39).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    track: str
    clips: Sequence[SourceClip]


def load_manifest(path: Path) -> CorpusManifest:
    """Read and validate a corpus manifest.

    Args:
        path: Path to the manifest JSON.

    Returns:
        The validated manifest.
    """
    raise NotImplementedError


def build(manifest: CorpusManifest, *, seed: int, out: Path) -> CorpusManifest:
    """Generate a perturbed corpus with `.truth.json` sidecars.

    Deterministic: the same `seed` and `GENERATOR_VERSION` produce byte-identical
    audio, asserted by a test that hashes the output. The seed is recorded in the
    run manifest.

    Args:
        manifest: The source clips.
        seed: Generator seed.
        out: Output directory.

    Returns:
        The manifest of generated clips.
    """
    raise NotImplementedError


def main(argv: Sequence[str] | None = None) -> int:
    """Run the corpus CLI.

    Args:
        argv: Arguments, defaulting to `sys.argv[1:]`.

    Returns:
        Process exit code.
    """
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
