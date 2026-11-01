from __future__ import annotations

import subprocess
from pathlib import Path


PATCHES = Path(__file__).parents[2] / "vendor" / "doubletake"


def test_carried_patch_chain_is_parseable() -> None:
    """Every documented patch is accepted by Git's patch parser."""
    for patch in sorted(PATCHES.glob("*.patch")):
        result = subprocess.run(
            ["git", "apply", "--numstat", str(patch)],
            cwd=PATCHES.parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{patch.name}: {result.stderr}"


def test_complete_token_requires_complete_producer() -> None:
    """The advertised completion token is backed by the entire patch chain."""
    producer = (PATCHES / "0001-capture-report-selected-source.patch").read_text()
    propagation = (PATCHES / "0002-status-report-selected-source.patch").read_text()
    capabilities = (PATCHES / "0003-capability-tokens.patch").read_text()

    assert "type SourceInfo struct" in producer
    assert 'source_type' in producer
    assert '"position"' in producer
    assert "Width: width, Height: height, X: x, Y: y" in producer
    assert "return info, true" in producer

    assert "source     SourceInfo" in propagation
    assert "hasSource  bool" in propagation
    assert "func (p *CapturePreparation) Source() (SourceInfo, bool)" in propagation
    assert "Source:         groupSource(s.captureGroup)" in propagation
    assert "group.source = &stored" in propagation

    assert '[]string{"capabilities-v1", "status-source", "status-source-complete"}' in capabilities
