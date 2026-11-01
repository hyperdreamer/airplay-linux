from __future__ import annotations

import os
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


def test_complete_token_requires_complete_producer(tmp_path) -> None:
    """Execute the carried producer with offline Variant and size boundaries."""
    producer = (PATCHES / "0001-capture-report-selected-source.patch").read_text()
    propagation = (PATCHES / "0002-status-report-selected-source.patch").read_text()
    capabilities = (PATCHES / "0003-capability-tokens.patch").read_text()

    assert "source     SourceInfo" in propagation
    assert "hasSource  bool" in propagation
    assert "func (p *CapturePreparation) Source() (SourceInfo, bool)" in propagation
    assert "Source:         groupSource(s.captureGroup)" in propagation
    assert "group.source = &stored" in propagation
    assert '[]string{"capabilities-v1", "status-source", "status-source-complete"}' in capabilities

    # Reconstruct the patch's postimage, including unchanged closing braces.
    # Compile the exact declarations; never translate or reimplement the parser.
    postimage = "\n".join(
        line[1:] for line in producer.splitlines()
        if line.startswith(("+", " ")) and not line.startswith("+++")
    )
    start = postimage.index("type SourceInfo struct {")
    end = postimage.index("\n}", postimage.index("func portalStreamSource(")) + 2
    (tmp_path / "go.mod").write_text("module sourceproof.test\n\ngo 1.22\n")
    (tmp_path / "source.go").write_text(
        'package sourceproof\n\nimport "sourceproof.test/dbus"\n\n' + postimage[start:end] + "\n"
    )
    # Only Variant.Value() is consumed. This local boundary preserves Go's
    # dynamic value types without a third-party module, bus client or network.
    (tmp_path / "dbus").mkdir()
    (tmp_path / "dbus" / "variant.go").write_text("""package dbus

type Variant struct { value interface{} }
func MakeVariant(value interface{}) Variant { return Variant{value: value} }
func (v Variant) Value() interface{} { return v.value }
""")
    (tmp_path / "source_test.go").write_text("""package sourceproof

import (
    "testing"
    "sourceproof.test/dbus"
)

// The producer delegates size decoding upstream. Supply the fixture's valid
// size at that boundary; all type and position decisions use the carried code.
func portalStreamDimensions(properties map[string]dbus.Variant) (int, int, bool) {
    size, ok := properties["size"].Value().([]int32)
    if !ok || len(size) != 2 { return 0, 0, false }
    return int(size[0]), int(size[1]), size[0] > 0 && size[1] > 0
}

func TestCarriedPortalStreamSource(t *testing.T) {
    properties := func(position interface{}, present bool) map[string]dbus.Variant {
        values := map[string]dbus.Variant{
            "source_type": dbus.MakeVariant(uint32(1)),
            "size": dbus.MakeVariant([]int32{1920, 1080}),
        }
        if present { values["position"] = dbus.MakeVariant(position) }
        return values
    }
    cases := []struct {
        name string
        position interface{}
        present, usable bool
        x, y int
    }{
        {"missing_position", nil, false, false, 0, 0},
        {"null_position", nil, true, false, 0, 0},
        {"scalar_position", true, true, false, 0, 0},
        {"string_position", "0,0", true, false, 0, 0},
        {"empty_position", []interface{}{}, true, false, 0, 0},
        {"short_position", []interface{}{int32(0)}, true, false, 0, 0},
        {"extra_coordinate", []interface{}{int32(0), int32(0), int32(0)}, true, false, 0, 0},
        {"null_x", []interface{}{nil, int32(0)}, true, false, 0, 0},
        {"null_y", []interface{}{int32(0), nil}, true, false, 0, 0},
        {"boolean_x", []interface{}{true, int32(0)}, true, false, 0, 0},
        {"boolean_y", []interface{}{int32(0), false}, true, false, 0, 0},
        {"fractional_x", []interface{}{0.5, int32(0)}, true, false, 0, 0},
        {"fractional_y", []interface{}{int32(0), 0.5}, true, false, 0, 0},
        {"string_x", []interface{}{"0", int32(0)}, true, false, 0, 0},
        {"string_y", []interface{}{int32(0), "0"}, true, false, 0, 0},
        {"mixed_int32_int64", []interface{}{int32(0), int64(0)}, true, false, 0, 0},
        {"mixed_int64_int32", []interface{}{int64(0), int32(0)}, true, false, 0, 0},
        {"zero_int32", []interface{}{int32(0), int32(0)}, true, true, 0, 0},
        {"zero_int64", []interface{}{int64(0), int64(0)}, true, true, 0, 0},
        {"negative_x", []interface{}{int32(-1920), int32(0)}, true, true, -1920, 0},
        {"negative_y", []interface{}{int32(0), int32(-1080)}, true, true, 0, -1080},
        {"negative_int64", []interface{}{int64(-1920), int64(-1080)}, true, true, -1920, -1080},
        {"positive", []interface{}{int32(1920), int32(1080)}, true, true, 1920, 1080},
    }
    for _, tc := range cases {
        t.Run(tc.name, func(t *testing.T) {
            got, usable := portalStreamSource(properties(tc.position, tc.present))
            if usable != tc.usable {
                t.Fatalf("source usable=%t, want %t; got %+v", usable, tc.usable, got)
            }
            if usable && (got != (SourceInfo{Type: 1, Width: 1920, Height: 1080, X: tc.x, Y: tc.y})) {
                t.Fatalf("wrong source geometry: %+v", got)
            }
        })
    }
}
""")
    result = subprocess.run(
        ["go", "test", "-count=1", "-run", "^TestCarriedPortalStreamSource$", "-v", "."],
        cwd=tmp_path,
        env={
            **os.environ, "GOPROXY": "off", "GOSUMDB": "off",
            "GOTOOLCHAIN": "local", "GOWORK": "off", "GOFLAGS": "",
            "CGO_ENABLED": "0",
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
