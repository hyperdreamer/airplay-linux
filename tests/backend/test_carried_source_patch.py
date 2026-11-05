from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from airplay_linux.backend.contract import BackendSource, StatusReply, decode_status


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
    go = shutil.which("go")
    if go is None:
        pytest.fail(
            "Go toolchain is required for the executable carried-source producer proof. "
            "Install Go and ensure `go` is on PATH, then rerun this test; "
            "the proof is mandatory for complete verification.",
            pytrace=False,
        )

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
    "encoding/json"
    "fmt"
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
            if usable && (got != (SourceInfo{Type: 1, Width: 1920, Height: 1080, X: tc.x, Y: tc.y, HasPosition: true})) {
                t.Fatalf("wrong source geometry: %+v", got)
            }
            if usable {
                encoded, err := json.Marshal(got)
                if err != nil { t.Fatal(err) }
                fmt.Printf("SOURCE_JSON=%s\\n", encoded)
            }
        })
    }
}
""")
    result = subprocess.run(
        [go, "test", "-count=1", "-run", "^TestCarriedPortalStreamSource$", "-v", "."],
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
    sources = [json.loads(line.removeprefix("SOURCE_JSON=")) for line in result.stdout.splitlines()
               if line.startswith("SOURCE_JSON=")]
    assert len(sources) == 6, result.stdout
    for wire in sources:
        assert set(wire) == {"type", "width", "height", "x", "y", "has_position"}
        assert wire["has_position"] is True
        decoded = decode_status({
            "ok": True, "state": "streaming", "has_audio": False, "audio_muted": False,
            "device": "DP-1", "device_ip": "192.0.2.1",
            "streams": [{
                "device": "DP-1", "device_ip": "192.0.2.1", "state": "streaming",
                "has_audio": False, "audio_muted": False, "source": wire,
            }],
        })
        assert isinstance(decoded, StatusReply), decoded
        assert decoded.streams[0].source == BackendSource(
            type=1, width=1920, height=1080, x=wire["x"], y=wire["y"],
        )


def test_producer_proof_reports_missing_go_with_git_available(tmp_path) -> None:
    """Missing Go is an actionable failure, not a launch error or a skipped gate."""
    git = shutil.which("git")
    assert git is not None, "Git is required for the carried-patch parser proof"
    commands = tmp_path / "bin"
    commands.mkdir()
    (commands / "git").symlink_to(git)
    path = str(commands)
    assert shutil.which("git", path=path) is not None
    assert shutil.which("go", path=path) is None

    test_file = Path(__file__).resolve()
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            f"{test_file}::test_carried_patch_chain_is_parseable",
            f"{test_file}::test_complete_token_requires_complete_producer",
            f"{test_file}::test_target_volume_patch_executes_carried_contract",
            "--basetemp", str(tmp_path / "pytest"),
            "-q", "-rA", "--tb=short", "--color=no",
        ],
        cwd=PATCHES.parents[1],
        env={**os.environ, "PATH": path},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "Go toolchain is required" in output, output
    assert "Install Go" in output, output
    assert "PATH" in output, output
    assert "FileNotFoundError" not in output, output
    assert "2 failed, 1 passed" in output, output
    assert output.count("Go toolchain is required") >= 2, output
    assert "PASSED tests/backend/test_carried_source_patch.py::test_carried_patch_chain_is_parseable" in output, output


def _volume_postimages() -> dict[str, str]:
    """Reconstruct each explicit file section, never concatenate unrelated hunks."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in (PATCHES / "0004-target-volume.patch").read_text().splitlines():
        if line.startswith("+++ b/"):
            current = line.removeprefix("+++ b/")
            sections[current] = []
        elif line.startswith("diff --git "):
            current = None
        elif current is not None and line[:1] in ("+", " "):
            sections[current].append(line[1:])
    return {path: "\n".join(lines) + "\n" for path, lines in sections.items()}


def _go_declarations(source: str, *prefixes: str) -> str:
    """Extract complete top-level declarations, including their closing braces."""
    declarations = []
    lines = source.splitlines()
    for prefix in prefixes:
        starts = [i for i, line in enumerate(lines) if line.startswith(prefix)]
        assert len(starts) == 1, f"expected one complete declaration: {prefix}"
        start = starts[0]
        if lines[start].endswith("}"):
            end = start
        else:
            end = next((i for i in range(start + 1, len(lines)) if lines[i] == "}"), None)
            assert end is not None, f"missing closing boundary: {prefix}"
        declarations.append("\n".join(lines[start:end + 1]))
    return "\n\n".join(declarations) + "\n"


@lru_cache(maxsize=1)
def carried_volume_status_traces() -> tuple[dict[str, object], ...]:
    """Execute the carried Go contracts and return their actual status envelopes."""
    go = shutil.which("go")
    if go is None:
        pytest.fail(
            "Go toolchain is required for the executable carried-volume producer proof. "
            "Install Go and ensure `go` is on PATH, then rerun this test; "
            "the proof is mandatory for complete verification.",
            pytrace=False,
        )
    sources = _volume_postimages()
    with TemporaryDirectory(prefix="av-volume-") as directory:
        root = Path(directory)
        for relative, content in _volume_proof_files(sources).items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        outputs = []
        for package in ("internal/airplay", "internal/daemon", "cmd/doubletake-ctl", "internal/daemon/daemonclient"):
            result = subprocess.run(
                [go, "test", "-count=1", "-v", "."], cwd=root / package,
                env={**os.environ, "CGO_ENABLED": "0", "GOPROXY": "off", "GOSUMDB": "off",
                     "GOTOOLCHAIN": "local", "GOWORK": "off", "GOFLAGS": ""},
                capture_output=True, text=True, timeout=60, check=False,
            )
            assert result.returncode == 0, f"{package}:\n{result.stdout}{result.stderr}"
            outputs.append(result.stdout)
    return tuple(json.loads(line.removeprefix("TRACE_JSON="))
                 for output in outputs for line in output.splitlines()
                 if line.startswith("TRACE_JSON="))


def test_target_volume_patch_executes_carried_contract(tmp_path) -> None:
    patch = PATCHES / "0004-target-volume.patch"
    assert patch.is_file(), "target-volume producer patch is required"
    traces = carried_volume_status_traces()
    assert {item["trace"] for item in traces} >= {
        "ack30_reject40", "unrequested_reject_first", "queued_pending",
        "queued_applied", "closed_refused", "closed_partial",
        "partial_setup_refused", "partial_retry_refused", "partial_retry_applied",
    }
    expected = {
        "ack30_reject40": (30, "applied", False),
        "unrequested_reject_first": (None, "unrequested", False),
        "queued_pending": (30, "pending", False),
        "queued_applied": (60, "applied", False),
        "closed_refused": (None, "unrequested", True),
        "closed_partial": (30, "applied", True),
        "partial_setup_refused": (30, "refused", False),
        "partial_retry_refused": (30, "refused", False),
        "partial_retry_applied": (30, "applied", False),
    }
    for item in traces:
        decoded = decode_status(item["status"])
        assert isinstance(decoded, StatusReply), decoded
        assert len(decoded.streams) == 1
        stream = decoded.streams[0]
        assert (stream.volume, stream.volume_state, stream.audio_closed) == expected[item["trace"]]
        for stream in item["status"]["streams"]:
            assert stream["volume_state"] != "applied" or "volume" in stream
            if item["quiescent"]:
                assert stream["volume_state"] != "pending"


def _volume_proof_files(sources: dict[str, str]) -> dict[str, str]:
    client = sources["internal/airplay/client.go"]
    mirror = sources["internal/airplay/mirror.go"]
    daemon = sources["internal/daemon/daemon.go"]
    cli = sources["cmd/doubletake-ctl/main.go"]
    wrappers = sources["internal/daemon/daemonclient/client.go"]
    setup = _go_declarations(mirror, "func (c *AirPlayClient) setupMirrorSession(")
    assert setup.index("cfg = copyAudioIntent(cfg)") < setup.index("compatibilityForReceiver(")
    assert "c.setupAudioLevel(cfg, audioURI, setupRTSPRequest)" in setup
    assert "controlConnectionUsable = false" in setup
    assert "initialVolume:" in setup and "initialVolumeState:" in setup
    connect = _go_declarations(daemon, "func (d *Daemon) connectAndStream(")
    assert "streamCfg.InitialVolume = copyVolume(entry.initialVolume)" in connect
    assert "streamCfg.AudioEnabled = copyAudio(entry.initialAudio)" in connect
    assert connect.count("SetupMirrorWithCalibratedVideoPreparation(ctx, streamCfg, prepareVideo)") == 2
    assert "sink, published := d.publishStreamSessionLocked(entry, target, session, client, broadcast)" in connect
    assert "session.InitialAudioLevel()" not in connect and "d.maybeStartAudio(entry)" in connect
    assert "audioCapture.Stop()" not in connect
    assert '"target-volume"}' in sources["cmd/doubletake/main.go"]
    assert cli.index("if parseErr != nil") < cli.index("daemonclient.New(")
    assert "decodeControlRequest(dec)" in _go_declarations(daemon, "func (d *Daemon) handleConn(")
    airplay_code = _go_declarations(client,
        "type HTTPStatusError struct", "func (e *HTTPStatusError) Error(",
        "type incompleteResponseError struct", "func (e *incompleteResponseError) Error(",
        "func (e *incompleteResponseError) Unwrap(", "func markIncompleteResponse(",
        "func hasIncompleteResponse(", "type CredentialsRequiredError struct",
        "func (e *CredentialsRequiredError) Error(", "func (e *CredentialsRequiredError) Unwrap(",
        "func (e *CredentialsRequiredError) Is(", "func (c *AirPlayClient) digestRetryHeader(",
        "func (c *AirPlayClient) preemptiveAuthHeader(", "func (c *AirPlayClient) logIfAuthRejected(",
        "func withHeader(", "func (c *AirPlayClient) rtspRequestUntil(",
        "func (c *AirPlayClient) rtspRequestOnceUntil(", "func (c *AirPlayClient) rtspRequest(",
        "func (c *AirPlayClient) rtspRequestOnce(", "func (c *AirPlayClient) readHTTPResponse(",
        "func (c *AirPlayClient) readHTTPResponseWithTimeout(",
        "func (c *AirPlayClient) readPlaintextHTTPResponse(",
        "func (c *AirPlayClient) readEncryptedHTTPResponse(", "func parseHTTPHeader(",
        "func parseContentLength(", "func validateContentLength(", "type StreamConfig struct",
        "func (c *AirPlayClient) Close(")
    airplay_code += _go_declarations(mirror,
        "func audioVolumeBody(", "func copyAudioIntent(", "func (c *AirPlayClient) setupAudioLevel(",
        "func (s *MirrorSession) InitialAudioLevel(", "func (s *MirrorSession) SetAudioLevel(",
        "func (s *MirrorSession) SetAudioMuted(", "func (s *MirrorSession) HasAudio(",
        "func (s *MirrorSession) AudioStream(")
    daemon_code = _go_declarations(daemon,
        "type Request struct", "type StreamInfo struct", "type Response struct", "type DeviceInfo struct",
        "type Config struct", "type activeStream struct", "type volumeRequest struct",
        "type videoCaptureKey struct", "type videoCaptureGroup struct", "type daemonCleanup struct",
        "type Daemon struct", "func copyVolume(", "func copyAudio(", "func decodeControlRequest(",
        "func updateVolumeState(", "func (d *Daemon) publishStreamSessionLocked(",
        "func (d *Daemon) audioTargetsLocked(",
        "func (d *Daemon) handleVolume(", "func (d *Daemon) runVolume(",
        "func (d *Daemon) maybeStartAudio(", "func (d *Daemon) handleAudioOff(",
        "func (d *Daemon) handleConn(", "func (d *Daemon) handleRequest(",
        "func (d *Daemon) overallStateLocked(", "func groupSource(",
        "func (d *Daemon) handleStatus(", "func (d *Daemon) statusResponseLocked(",
        "func waitingCredentialKind(", "func (d *Daemon) handleConnect(",
        "func (d *Daemon) pendingCredentialTargetsLocked(", "func (d *Daemon) clearLastErrorForTargetLocked(",
        "func (d *Daemon) pickFreeDeviceLocked(", "func (d *Daemon) handleSetMute(",
        "func (cleanup *daemonCleanup) addStream(", "func (cleanup *daemonCleanup) addCaptureGroup(",
        "func (cleanup *daemonCleanup) run(", "func (cleanup *daemonCleanup) merge(",
        "func (cleanup *daemonCleanup) empty(", "func (d *Daemon) detachStreamLocked(",
        "func (d *Daemon) captureGroupInUseLocked(", "func (d *Daemon) detachCaptureGroupIfUnusedLocked(",
        "func (d *Daemon) detachCaptureGroupLocked(", "func (d *Daemon) detachAllLocked(",
        "func (d *Daemon) handleDisconnect(")
    return {
        "go.mod": "module doubletake\n\ngo 1.22\n",
        "internal/airplay/carried.go": 'package airplay\nimport ("bytes"; "context"; "encoding/hex"; "errors"; "fmt"; "io"; "log"; "math"; "strconv"; "strings"; "time")\n' + airplay_code,
        "internal/airplay/boundary.go": _VOLUME_AIRPLAY_BOUNDARY,
        "internal/airplay/carried_test.go": _VOLUME_AIRPLAY_TESTS,
        "internal/daemon/carried.go": 'package daemon\nimport ("bytes"; "context"; "encoding/json"; "errors"; "fmt"; "net"; "sort"; "sync"; "time"; "doubletake/internal/airplay")\n' + daemon_code,
        "internal/daemon/boundary.go": _VOLUME_DAEMON_BOUNDARY,
        "internal/daemon/carried_test.go": _VOLUME_DAEMON_TESTS,
        "cmd/doubletake-ctl/main.go": cli,
        "cmd/doubletake-ctl/carried_test.go": _VOLUME_CLI_TESTS,
        "internal/daemon/daemonclient/carried.go": 'package daemonclient\nimport "doubletake/internal/daemon"\n' + _go_declarations(wrappers,
            "type Client struct", "func New(", "func (c *Client) Connect(", "func (c *Client) ConnectWithAudio(",
            "func (c *Client) Volume(", "func (c *Client) AudioOff(", "func (c *Client) Status(",
            "func (c *Client) Discover(", "func (c *Client) Devices(", "func (c *Client) Disconnect(",
            "func (c *Client) DisconnectTarget(", "func (c *Client) Mute(", "func (c *Client) MuteTarget(",
            "func (c *Client) Unmute(", "func (c *Client) UnmuteTarget("),
        "internal/daemon/daemonclient/boundary.go": _VOLUME_CLIENT_BOUNDARY,
        "internal/daemon/daemonclient/carried_test.go": _VOLUME_CLIENT_TESTS,
    }


_VOLUME_AIRPLAY_BOUNDARY = r'''package airplay
import ("bytes"; "context"; "errors"; "io"; "net"; "sync"; "sync/atomic"; "time")
type VideoCodec string
type digestChallenge struct { Realm string }
const DigestUsername = "proof"
var ErrCredentialsRequired = errors.New("receiver credentials required")
const maxResponseBody = 8 << 20
func dbg(string, ...interface{}) {}
func parseDigestChallenge(value string) (*digestChallenge, bool) { return &digestChallenge{Realm:"test"}, value != "" }
func authorizationHeader(string, string, *digestChallenge, string, string) string { return "Digest proof" }
type AirPlayClient struct { conn net.Conn; mu sync.Mutex; cseq atomic.Int64; encrypted bool; encReadKey []byte; encReadNonce uint64; authPassword string; authChallenge *digestChallenge }
func (c *AirPlayClient) encrypt(data []byte) []byte { return data }
func (c *AirPlayClient) readEncryptedFrame() ([]byte,error) { b:=make([]byte,1); n,e:=c.conn.Read(b); return b[:n],e }
type AudioStream struct{}
type MirrorSession struct { client *AirPlayClient; sessionURI string; noAudio bool; audioStream *AudioStream; initialVolume *int; initialVolumeState string }
func (s *MirrorSession) AudioCodec() int { return 1 }
func (s *MirrorSession) Close() error { return nil }
func (s *MirrorSession) StreamAudio(ctx context.Context, capture *AudioCapture, stream *AudioStream) error { <-capture.stopped; return nil }
type AudioCapture struct { Stops atomic.Int32; stopped chan struct{} }
func (c *AudioCapture) Stop() { c.Stops.Add(1); close(c.stopped) }
var CaptureStart = func(context.Context, bool, int) (*AudioCapture,error) { return ProofCapture(),nil }
func ProofCapture() *AudioCapture { return &AudioCapture{stopped:make(chan struct{})} }
func StartAudioCapture(ctx context.Context, test bool, codec int) (*AudioCapture,error) { return CaptureStart(ctx,test,codec) }
type ProofConn struct { ReadBuffer *bytes.Reader; Exchange func([]byte)([]byte,error); Writes [][]byte; Closed bool; ClearFails bool; Short bool; Deadlines []time.Time }
func (c *ProofConn) Read(p []byte)(int,error) { if c.Closed { return 0,net.ErrClosed }; return c.ReadBuffer.Read(p) }
func (c *ProofConn) Write(p []byte)(int,error) { if c.Closed { return 0,net.ErrClosed }; c.Writes=append(c.Writes,append([]byte(nil),p...)); if c.Short { return len(p)-1,nil }; if c.Exchange!=nil { b,e:=c.Exchange(p); c.ReadBuffer=bytes.NewReader(b); return len(p),e }; return len(p),nil }
func (c *ProofConn) Close() error { c.Closed=true; return nil }
func (c *ProofConn) LocalAddr() net.Addr { return nil }
func (c *ProofConn) RemoteAddr() net.Addr { return nil }
func (c *ProofConn) SetDeadline(t time.Time) error { return nil }
func (c *ProofConn) SetReadDeadline(t time.Time) error { c.Deadlines=append(c.Deadlines,t); if t.IsZero() && c.ClearFails { return io.ErrClosedPipe }; return nil }
func (c *ProofConn) SetWriteDeadline(t time.Time) error { return c.SetReadDeadline(t) }
func ProofSession(level *int, state string, exchange func([]byte)([]byte,error)) *MirrorSession { return &MirrorSession{client:&AirPlayClient{conn:&ProofConn{Exchange:exchange}}, sessionURI:"rtsp://proof/audio",audioStream:&AudioStream{},initialVolume:level,initialVolumeState:state} }
// The setup outcome comes from the exact carried helper, never fixture arithmetic.
func ProofSetupSession(percent int, exchange func([]byte)([]byte,error)) (*MirrorSession,error) {
 return ProofSetupWithConfig(StreamConfig{InitialVolume:&percent},exchange)
}
func ProofSetupWithConfig(cfg StreamConfig, exchange func([]byte)([]byte,error)) (*MirrorSession,error) {
 cfg=copyAudioIntent(cfg)
 c:=&AirPlayClient{conn:&ProofConn{Exchange:exchange}}
 level,state,err:=c.setupAudioLevel(cfg,"rtsp://proof/audio",c.rtspRequest)
 return &MirrorSession{client:c,sessionURI:"rtsp://proof/audio",audioStream:&AudioStream{},noAudio:cfg.NoAudio,initialVolume:level,initialVolumeState:state},err
}
type SourceInfo struct { Type uint32 `json:"type"`; Width int `json:"width"`; Height int `json:"height"`; X int `json:"x"`; Y int `json:"y"`; HasPosition bool `json:"has_position"` }
type BroadcastSink struct{}
func (*BroadcastSink) Close() {}
type BroadcastCapture struct { Sinks int }
func (b *BroadcastCapture) AddSink() *BroadcastSink { b.Sinks++;return &BroadcastSink{} }
type ScreenCapture struct{}
func (*ScreenCapture) Stop() {}
type CredentialStore struct{}
type AirPlayDevice struct { IP string; Port int }
'''


_VOLUME_AIRPLAY_TESTS = r'''package airplay
import ("bytes"; "bufio"; "context"; "errors"; "fmt"; "io"; "math"; "net"; "strings"; "testing"; "time")
func response(code int) []byte { return []byte(fmt.Sprintf("RTSP/1.0 %d Result\r\nContent-Length: 0\r\n\r\n",code)) }
func TestMapping(t *testing.T) {
 for _,tc:=range []struct{n int; want string}{{-1,"volume: -144.000000\r\n"},{0,"volume: -144.000000\r\n"},{1,"volume: -40.000000\r\n"},{50,"volume: -6.020600\r\n"},{99,"volume: -0.087296\r\n"},{100,"volume: 0.000000\r\n"},{101,"volume: 0.000000\r\n"}} { if got:=string(audioVolumeBody(tc.n)); got!=tc.want {t.Fatalf("%d: %q",tc.n,got)} }
 previous:=math.Inf(-1); for n:=1;n<=100;n++ {var db float64; fmt.Sscanf(string(audioVolumeBody(n)),"volume: %f",&db);if db<=previous || strings.Contains(string(audioVolumeBody(n)),"-0.000000") {t.Fatal(n,db)};previous=db}
}
func TestSetupMatrixAndReceipt(t *testing.T) {
 for _,requested:=range []bool{false,true} { for _,enabled:=range []bool{false,true} { for _,n:=range []int{0,30} {
  t.Run(fmt.Sprintf("requested=%t/enabled=%t/level=%d",requested,enabled,n),func(t *testing.T){
   cfg:=StreamConfig{AudioEnabled:&enabled};if requested {cfg.InitialVolume=&n};cfg=copyAudioIntent(cfg)
   conn:=&ProofConn{Exchange:func([]byte)([]byte,error){return response(200),nil}};c:=&AirPlayClient{conn:conn};legacyCalls:=0
   level,state,err:=c.setupAudioLevel(cfg,"rtsp://proof/audio",func(m,u,ct string,b []byte,h map[string]string)([]byte,map[string]string,error){legacyCalls++;if string(b)!="volume: 0.000000\r\n" {t.Fatal(string(b))};return nil,nil,nil})
   if err!=nil {t.Fatal(err)}
   if !enabled {if len(conn.Writes)!=0 || legacyCalls!=0 || level!=nil || state!="unrequested" {t.Fatal(level,state,legacyCalls,len(conn.Writes))};return}
   if !requested {if legacyCalls!=2 || level!=nil || state!="unrequested" {t.Fatal(level,state,legacyCalls)};return}
   if len(conn.Writes)!=2 || level==nil || *level!=n || state!="applied" {t.Fatal(level,state,len(conn.Writes))}
   for _,frame:=range conn.Writes {if !bytes.HasSuffix(frame,audioVolumeBody(n)) || !bytes.HasPrefix(frame,[]byte("SET_PARAMETER rtsp://proof/audio RTSP/1.0\r\n")) {t.Fatal(string(frame))}}
   var deadline time.Time;for _,d:=range conn.Deadlines {if !d.IsZero() {if deadline.IsZero(){deadline=d};if !d.Equal(deadline){t.Fatal("setup budget replenished")}}}
   session:=&MirrorSession{initialVolume:level,initialVolumeState:state};copy,_:=session.InitialAudioLevel();*copy=99;if *level!=n {t.Fatal("mutable receipt")}
  })
 }}}
 n:=30; conn:=&ProofConn{};count:=0;conn.Exchange=func([]byte)([]byte,error){count++;if count==1{return response(200),nil};return response(403),nil};c:=&AirPlayClient{conn:conn}
 level,state,err:=c.setupAudioLevel(StreamConfig{InitialVolume:&n},"rtsp://proof/audio",nil);if err!=nil || level==nil || *level!=30 || state!="refused" || conn.Closed || count!=2 {t.Fatal(level,state,err,count,conn.Closed)}
 conn.Exchange=func([]byte)([]byte,error){return response(200),nil};s:=&MirrorSession{client:c,sessionURI:"rtsp://proof/audio"};if err:=s.SetAudioLevel(40);err!=nil{t.Fatal(err)}
 count=0;conn.Exchange=func([]byte)([]byte,error){count++;return []byte("RTSP/1.0 200 OK\r\nContent-Length: 4\r\n\r\nx"),nil};_,_,err=c.setupAudioLevel(StreamConfig{InitialVolume:&n},"rtsp://proof/audio",nil);if err==nil || !conn.Closed || count!=1 {t.Fatal("incomplete setup survived",err,count)}
 on:=true;cfg:=copyAudioIntent(StreamConfig{NoAudio:true,AudioEnabled:&on,InitialVolume:&n});on=false;n=70;if !cfg.NoAudio || !*cfg.AudioEnabled || *cfg.InitialVolume!=30 {t.Fatal("intent was not copied",cfg)}
}
func TestBoundedReadersAndPoisoning(t *testing.T) {
 cases:=[]struct{name string;body []byte;short,clearFails,closed,success bool}{
 {"200",response(200),false,false,false,true},{"201",append([]byte("RTSP/1.0 201 Created\r\nContent-Length: 3\r\n\r\n"),[]byte("abc")...),false,false,false,false},{"204",response(204),false,false,false,false},{"403",response(403),false,false,false,false},
 {"header",[]byte("RTSP/1.0 200"),false,false,true,false},{"body",[]byte("RTSP/1.0 200 OK\r\nContent-Length: 4\r\n\r\nx"),false,false,true,false},{"partial_write",response(200),true,false,true,false},{"clear_failure",response(200),false,true,true,false},
 }
 for _,encrypted:=range []bool{false,true} {for _,tc:=range cases {t.Run(fmt.Sprintf("%s/encrypted=%t",tc.name,encrypted),func(t *testing.T){
  conn:=&ProofConn{Exchange:func([]byte)([]byte,error){return tc.body,nil},Short:tc.short,ClearFails:tc.clearFails};c:=&AirPlayClient{conn:conn,encrypted:encrypted};deadline:=time.Now().Add(time.Second)
  _,_,err:=c.rtspRequestUntil("SET_PARAMETER","rtsp://proof/audio","text/parameters",audioVolumeBody(30),nil,deadline)
  if (err==nil)!=tc.success || conn.Closed!=tc.closed || c.conn!=conn {t.Fatal(err,conn.Closed)}
  if tc.name=="201"||tc.name=="204" {var status *HTTPStatusError;if !errors.As(err,&status) {t.Fatal(err)};if conn.ReadBuffer.Len()!=0{t.Fatal("undrained response")}}
  for _,d:=range conn.Deadlines {if !d.IsZero()&&!d.Equal(deadline){t.Fatal("deadline changed")}}
 })}}
 c:=&AirPlayClient{conn:&ProofConn{ReadBuffer:bytes.NewReader(response(204))}};if _,_,err:=c.readPlaintextHTTPResponse();err!=nil {t.Fatal("legacy 2xx changed",err)}
 conn:=&ProofConn{Exchange:func([]byte)([]byte,error){return response(200),nil}};c=&AirPlayClient{conn:conn};c.mu.Lock();done:=make(chan error,1);go func(){_,_,e:=c.rtspRequestUntil("SET_PARAMETER","u","",nil,nil,time.Now().Add(20*time.Millisecond));done<-e}();err:=<-done;if !errors.Is(err,context.DeadlineExceeded)||len(conn.Writes)!=0||conn.Closed{t.Fatal(err)};c.mu.Unlock();if !c.mu.TryLock(){t.Fatal("orphan mutex waiter")};c.mu.Unlock()
 _,_,err=c.rtspRequestUntil("SET_PARAMETER","u","",nil,nil,time.Now().Add(-time.Second));if !errors.Is(err,context.DeadlineExceeded)||len(conn.Writes)!=0||conn.Closed{t.Fatal(err)}
}
func TestPipeDeadlinesAndDigestShareBudget(t *testing.T) {
 for _,digest:=range []bool{false,true} {t.Run(fmt.Sprintf("digest=%t",digest),func(t *testing.T){
  client,server:=net.Pipe();defer server.Close();c:=&AirPlayClient{conn:client,authPassword:"test"};done:=make(chan struct{})
  go func(){defer close(done);reader:=bufio.NewReader(server);for turn:=0;turn<2;turn++ {var content int;for{line,e:=reader.ReadString('\n');if e!=nil{return};if strings.HasPrefix(line,"Content-Length:"){fmt.Sscanf(line,"Content-Length: %d",&content)};if line=="\r\n"{break}};if _,e:=io.CopyN(io.Discard,reader,int64(content));e!=nil{return};if digest&&turn==0 {time.Sleep(15*time.Millisecond);_,_=server.Write([]byte("RTSP/1.0 401 No\r\nWWW-Authenticate: Digest realm=\"test\"\r\nContent-Length: 0\r\n\r\n"));continue};<-time.After(100*time.Millisecond);return}}()
  start:=time.Now();_,_,err:=c.rtspRequestUntil("SET_PARAMETER","u","text/parameters",audioVolumeBody(30),nil,start.Add(40*time.Millisecond));if err==nil||time.Since(start)>90*time.Millisecond||!hasIncompleteResponse(err){t.Fatal(err,time.Since(start))};if _,e:=client.Write([]byte("x"));!errors.Is(e,io.ErrClosedPipe){t.Fatal("connection not closed",e)};<-done
 })}
}
'''


_VOLUME_DAEMON_BOUNDARY = r'''package daemon
import ("context"; "doubletake/internal/airplay")
type State string
const ( StateIdle State="idle"; StateDiscovering State="discovering"; StateConnecting State="connecting"; StateStreaming State="streaming"; StatePINRequired State="pin_required" )
type CredentialKind string
const (CredentialKindPIN CredentialKind="pin"; CredentialKindPassword CredentialKind="password")
func DefaultSocketPath() string { return "/tmp/av-never-contact.sock" }
func (d *Daemon) handleDiscover() Response { return d.handleStatus() }
func (d *Daemon) handleDevices() Response { return d.handleStatus() }
// Connection establishment is an external boundary; no networking or discovery.
func (d *Daemon) connectAndStream(ctx context.Context, entry *activeStream, target string, port int, pin string) { <-ctx.Done() }
var _ = airplay.VideoCodec("")
'''


# Disposable-postimage mutation probe: replace publishStreamSessionLocked's
# `current.audioLevelReady = current.volumeState == "applied" || current.volumeState == "unrequested"`
# with `current.audioLevelReady = true`. `go test -count=1 -v -run
# '^TestSetupReceiptPublicationAdmission$' .` exited 1 in refused, partial, and
# refused_then_ack; exact failure: `carried_test.go:34: refused or closed setup admitted audio`.
# The unmutated baseline and restored postimage both exited 0.
_VOLUME_DAEMON_TESTS = r'''package daemon
import ("context"; "encoding/json"; "errors"; "fmt"; "net"; "strings"; "sync"; "sync/atomic"; "testing"; "time"; "doubletake/internal/airplay")
func wire(code int) []byte {return []byte(fmt.Sprintf("RTSP/1.0 %d Result\r\nContent-Length: 0\r\n\r\n",code))}
func await(t *testing.T, predicate func()bool) {t.Helper();deadline:=time.Now().Add(time.Second);for !predicate(){if time.Now().After(deadline){t.Fatal("state barrier timed out")};time.Sleep(time.Millisecond)}}
func fixture(t *testing.T, ready bool, level *int, exchange func([]byte)([]byte,error)) (*Daemon,*activeStream,*airplay.AudioCapture) {
 t.Helper();capture:=airplay.ProofCapture();old:=airplay.CaptureStart;airplay.CaptureStart=func(context.Context,bool,int)(*airplay.AudioCapture,error){return capture,nil};t.Cleanup(func(){airplay.CaptureStart=old})
 state:="refused";if ready {state="unrequested";if level!=nil{state="applied"}}
 entry:=&activeStream{device:"TV",deviceIP:"192.0.2.10",state:StateConnecting}
 d:=&Daemon{streams:map[string]*activeStream{entry.deviceIP:entry},captureGroups:make(map[videoCaptureKey]*videoCaptureGroup)}
 d.mu.Lock();_,published:=d.publishStreamSessionLocked(entry,entry.deviceIP,airplay.ProofSession(level,state,exchange),nil,&airplay.BroadcastCapture{});d.mu.Unlock();if !published{t.Fatal("fixture session was not published")}
 t.Cleanup(func(){d.mu.Lock();cleanup:=d.detachAllLocked();d.mu.Unlock();cleanup.run();d.streamWorkers.Wait()})
 return d,entry,capture
}
func TestSetupReceiptPublicationAdmission(t *testing.T) {
 for _,tc:=range []struct{name string;requested bool;percent int;codes [2]int;off,closed,ready bool;state string;hasLevel bool;writes int}{
  {name:"unrequested",codes:[2]int{200,200},ready:true,state:"unrequested",writes:2},
  {name:"applied",requested:true,percent:30,codes:[2]int{200,200},ready:true,state:"applied",hasLevel:true,writes:2},
  {name:"zero",requested:true,codes:[2]int{200,200},ready:true,state:"applied",hasLevel:true,writes:2},
  {name:"refused",requested:true,percent:30,codes:[2]int{403,403},state:"refused",writes:2},
  {name:"partial",requested:true,percent:30,codes:[2]int{200,403},state:"refused",hasLevel:true,writes:2},
  {name:"refused_then_ack",requested:true,percent:30,codes:[2]int{403,200},state:"refused",hasLevel:true,writes:2},
  {name:"audio_off",requested:true,percent:30,off:true,closed:true,state:"unrequested"},
  {name:"latched_during_setup",requested:true,percent:30,codes:[2]int{200,200},closed:true,state:"applied",hasLevel:true,writes:2},
 } {t.Run(tc.name,func(t *testing.T){
  capture:=airplay.ProofCapture();old:=airplay.CaptureStart;var starts atomic.Int32;airplay.CaptureStart=func(context.Context,bool,int)(*airplay.AudioCapture,error){starts.Add(1);return capture,nil};defer func(){airplay.CaptureStart=old}()
  cfg:=airplay.StreamConfig{};if tc.requested{cfg.InitialVolume=&tc.percent};if tc.off{off:=false;cfg.AudioEnabled=&off}
  calls:=0;session,err:=airplay.ProofSetupWithConfig(cfg,func([]byte)([]byte,error){code:=tc.codes[calls];calls++;return wire(code),nil});if err!=nil{t.Fatal(err)}
  entry:=&activeStream{device:"TV",deviceIP:"192.0.2.10",state:StateConnecting,audioClosed:tc.closed}
  d:=&Daemon{streams:map[string]*activeStream{entry.deviceIP:entry},captureGroups:make(map[videoCaptureKey]*videoCaptureGroup)}
  t.Cleanup(func(){d.mu.Lock();cleanup:=d.detachAllLocked();d.mu.Unlock();cleanup.run();d.streamWorkers.Wait()})
  client:=&airplay.AirPlayClient{};broadcast:=&airplay.BroadcastCapture{}
  d.mu.Lock();sink,published:=d.publishStreamSessionLocked(entry,entry.deviceIP,session,client,broadcast);d.mu.Unlock()
  if !published||sink==nil||entry.sink!=sink||entry.session!=session||entry.client!=client||entry.state!=StateStreaming||broadcast.Sinks!=1{t.Fatal("setup session was not fully published")}
  if !tc.ready&&entry.audioLevelReady{t.Fatal("refused or closed setup admitted audio")}
  if entry.audioLevelReady!=tc.ready||entry.volumeState!=tc.state||calls!=tc.writes||entry.audioClosed!=tc.closed{t.Fatal(entry.audioLevelReady,entry.volumeState,calls,entry.audioClosed)}
  if (entry.volume!=nil)!=tc.hasLevel||tc.hasLevel&&*entry.volume!=tc.percent||entry.audioMuted!=(tc.hasLevel&&tc.percent==0){t.Fatal("incorrect setup receipt publication")}
  d.maybeStartAudio(entry)
  if tc.ready{await(t,func()bool{d.mu.Lock();defer d.mu.Unlock();return entry.audioCapture!=nil})}else if starts.Load()!=0||entry.audioDone!=nil{t.Fatal("withheld setup started capture")}
  status:=d.handleStatus();stream:=status.Streams[0];if stream.VolumeState!=tc.state||(stream.Volume!=nil)!=tc.hasLevel||stream.AudioClosed!=tc.closed{t.Fatal("setup status lost receipt",stream)}
 })}
 t.Run("replaced",func(t *testing.T){
  entry:=&activeStream{deviceIP:"192.0.2.10",state:StateConnecting};replacement:=&activeStream{deviceIP:entry.deviceIP,state:StateConnecting}
  d:=&Daemon{streams:map[string]*activeStream{entry.deviceIP:replacement}};broadcast:=&airplay.BroadcastCapture{}
  d.mu.Lock();sink,published:=d.publishStreamSessionLocked(entry,entry.deviceIP,airplay.ProofSession(nil,"unrequested",nil),nil,broadcast);d.mu.Unlock()
  if published||sink!=nil||broadcast.Sinks!=0||replacement.session!=nil||replacement.audioLevelReady||entry.session!=nil{t.Fatal("obsolete setup published into replacement")}
 })
}
func level(d *Daemon,n int) Response {return d.handleVolume(Request{Cmd:"volume",Target:"192.0.2.10",Volume:&n})}
func closeAudio(d *Daemon) Response {off:=false;return d.handleAudioOff(Request{Cmd:"audio",Target:"192.0.2.10",Audio:&off})}
func settled(t *testing.T,d *Daemon,e *activeStream) {t.Helper();await(t,func()bool{d.mu.Lock();defer d.mu.Unlock();return !e.volumeRunning})}
func trace(t *testing.T,d *Daemon,name string,quiescent bool){t.Helper();d.mu.Lock();status:=d.statusResponseLocked(true,"");d.mu.Unlock();b,e:=json.Marshal(struct{Trace string `json:"trace"`;Status Response `json:"status"`;Quiescent bool `json:"quiescent"`}{name,status,quiescent});if e!=nil{t.Fatal(e)};fmt.Printf("TRACE_JSON=%s\n",b)}
func TestRecoverySerializerTraces(t *testing.T) {
 t.Run("ack30_reject40",func(t *testing.T){var calls atomic.Int32;d,e,capture:=fixture(t,true,nil,func([]byte)([]byte,error){if calls.Add(1)==1{return wire(200),nil};return wire(403),nil});if r:=level(d,30);!r.OK{t.Fatal(r)};await(t,func()bool{d.mu.Lock();defer d.mu.Unlock();return e.audioCapture!=nil});if r:=level(d,40);r.OK||r.Error!="receiver rejected volume"{t.Fatal(r)};settled(t,d,e);if e.volumeState!="applied"||e.volume==nil||*e.volume!=30||capture.Stops.Load()!=0{t.Fatal(e.volumeState,e.volume)};trace(t,d,"ack30_reject40",true)})
 t.Run("unrequested_reject_first",func(t *testing.T){d,e,capture:=fixture(t,true,nil,func([]byte)([]byte,error){return wire(403),nil});d.maybeStartAudio(e);await(t,func()bool{d.mu.Lock();defer d.mu.Unlock();return e.audioCapture!=nil});if level(d,40).OK{t.Fatal("rejection acknowledged")};settled(t,d,e);if e.volumeState!="unrequested"||e.volume!=nil||capture.Stops.Load()!=0{t.Fatal(e.volumeState,e.volume)};trace(t,d,"unrequested_reject_first",true)})
 t.Run("latest_slot",func(t *testing.T){entered:=make(chan struct{});release:=make(chan struct{});next:=make(chan struct{});releaseNext:=make(chan struct{});var once,onceNext sync.Once;defer once.Do(func(){close(release)});defer onceNext.Do(func(){close(releaseNext)});var calls atomic.Int32
  d,e,_:=fixture(t,true,nil,func([]byte)([]byte,error){switch calls.Add(1){case 1:return wire(200),nil;case 2:close(entered);<-release;return wire(403),nil;default:close(next);<-releaseNext;return wire(200),nil}})
  if !level(d,30).OK{t.Fatal("first ack")};r40:=make(chan Response,1);go func(){r40<-level(d,40)}();<-entered
  r50:=make(chan Response,1);go func(){r50<-level(d,50)}();await(t,func()bool{d.mu.Lock();defer d.mu.Unlock();return e.volumeDesired!=nil&&e.volumeDesired.percent==50})
  r60:=make(chan Response,1);go func(){r60<-level(d,60)}();if r:=<-r50;r.OK||r.Error!="volume request superseded"{t.Fatal(r)}
  once.Do(func(){close(release)});<-next;if r:=<-r40;r.OK{t.Fatal(r)};trace(t,d,"queued_pending",false)
  d.mu.Lock();if !e.audioLevelReady||e.volumeState!="pending"||*e.volume!=30{t.Fatal(e.volumeState)};d.mu.Unlock()
  onceNext.Do(func(){close(releaseNext)});if r:=<-r60;!r.OK{t.Fatal(r)};settled(t,d,e);if e.volumeState!="applied"||*e.volume!=60||calls.Load()!=3{t.Fatal(e.volumeState,e.volume,calls.Load())};trace(t,d,"queued_applied",true)
 })
}
func TestPartialStartupRecoveryTraces(t *testing.T) {
 var calls atomic.Int32
 exchange:=func([]byte)([]byte,error){n:=calls.Add(1);if n==2||n==3{return wire(403),nil};return wire(200),nil}
 session,err:=airplay.ProofSetupSession(30,exchange);if err!=nil{t.Fatal(err)}
 percent,state:=session.InitialAudioLevel();if state!="refused"||percent==nil||*percent!=30||calls.Load()!=2{t.Fatal(percent,state,calls.Load())}
 d,e,capture:=fixture(t,false,percent,exchange);d.mu.Lock();d.publishStreamSessionLocked(e,e.deviceIP,session,nil,&airplay.BroadcastCapture{});d.mu.Unlock()
 d.maybeStartAudio(e);if e.audioDone!=nil{t.Fatal("partial setup admitted audio")};trace(t,d,"partial_setup_refused",true)
 if level(d,30).OK{t.Fatal("rejected retry acknowledged")};settled(t,d,e)
 if e.audioLevelReady||e.audioDone!=nil||capture.Stops.Load()!=0{t.Fatal("rejected retry admitted audio")};trace(t,d,"partial_retry_refused",true)
 if !level(d,30).OK{t.Fatal("later retry rejected")};settled(t,d,e)
 await(t,func()bool{d.mu.Lock();defer d.mu.Unlock();return e.audioCapture!=nil})
 if !e.audioLevelReady||calls.Load()!=4{t.Fatal("later 200 did not admit audio",calls.Load())};trace(t,d,"partial_retry_applied",true)
}
func TestClosedTerminalTracesAndLateRetry(t *testing.T){for _,partial:=range []bool{false,true}{t.Run(fmt.Sprint(partial),func(t *testing.T){
 var initial *int;if partial{n:=30;initial=&n};entered:=make(chan struct{});release:=make(chan struct{});var once sync.Once;defer once.Do(func(){close(release)})
 d,e,capture:=fixture(t,false,initial,func([]byte)([]byte,error){close(entered);<-release;return wire(200),nil});d.maybeStartAudio(e);if e.audioDone!=nil{t.Fatal("withheld startup captured")}
 result:=make(chan Response,1);go func(){result<-level(d,40)}();<-entered
 if r:=closeAudio(d);!r.OK{t.Fatal(r)};once.Do(func(){close(release)});if r:=<-result;r.OK{t.Fatal("latched acknowledgement escaped",r)};settled(t,d,e)
 name:="closed_refused";want:="unrequested";if partial{name="closed_partial";want="applied"}
 if !e.audioClosed||e.volumeState!=want||e.audioLevelReady||capture.Stops.Load()!=0||e.audioDone!=nil{t.Fatal(e.volumeState,e.audioLevelReady)}
 trace(t,d,name,true);if r:=level(d,50);r.OK||r.Error!="audio is closed for this stream"{t.Fatal(r)}
})}}
func TestRefusedRecoveryAdmitsAudioBeforeQueuedWrite(t *testing.T){release:=make(chan struct{});entered:=make(chan struct{});next:=make(chan struct{});releaseNext:=make(chan struct{});var once,onceNext sync.Once;defer once.Do(func(){close(release)});defer onceNext.Do(func(){close(releaseNext)});var calls atomic.Int32
 d,e,_:=fixture(t,false,nil,func([]byte)([]byte,error){if calls.Add(1)==1{close(entered);<-release;return wire(200),nil};close(next);<-releaseNext;return wire(403),nil})
 first:=make(chan Response,1);go func(){first<-level(d,30)}();<-entered;second:=make(chan Response,1);go func(){second<-level(d,40)}();await(t,func()bool{d.mu.Lock();defer d.mu.Unlock();return e.volumeDesired!=nil});once.Do(func(){close(release)});<-next
 await(t,func()bool{d.mu.Lock();defer d.mu.Unlock();return e.audioCapture!=nil});if r:=<-first;!r.OK{t.Fatal(r)};onceNext.Do(func(){close(releaseNext)});if (<-second).OK{t.Fatal("rejection acknowledged")};settled(t,d,e);if e.volumeState!="applied"||*e.volume!=30{t.Fatal(e.volumeState)}
}
func TestIndependentTargetsAndOriginalEntryFences(t *testing.T){
 t.Run("independent",func(t *testing.T){entered:=make(chan struct{});release:=make(chan struct{});var once sync.Once;defer once.Do(func(){close(release)});d,a,_:=fixture(t,false,nil,func([]byte)([]byte,error){close(entered);<-release;return wire(200),nil});b:=&activeStream{device:"B",deviceIP:"192.0.2.20",state:StateStreaming,audioLevelReady:false,volumeState:"refused",session:airplay.ProofSession(nil,"refused",func([]byte)([]byte,error){return wire(403),nil})};d.streams[b.deviceIP]=b
 n:=50;done:=make(chan Response,1);go func(){done<-d.handleVolume(Request{Cmd:"volume",Volume:&n})}();<-entered;await(t,func()bool{d.mu.Lock();defer d.mu.Unlock();return !b.volumeRunning&&b.volumeSeq==1});d.mu.Lock();if !a.volumeRunning||b.volume!=nil{t.Fatal("targets did not run independently")};d.mu.Unlock();once.Do(func(){close(release)});if (<-done).OK{t.Fatal("broadcast omitted rejection")}
 })
 t.Run("replacement",func(t *testing.T){entered:=make(chan struct{});release:=make(chan struct{});var once sync.Once;defer once.Do(func(){close(release)});d,e,_:=fixture(t,false,nil,func([]byte)([]byte,error){close(entered);<-release;return wire(200),nil});done:=make(chan Response,1);go func(){done<-level(d,30)}();<-entered;d.mu.Lock();cleanup:=d.detachStreamLocked(e.deviceIP);replacement:=&activeStream{deviceIP:e.deviceIP,state:StateStreaming,volumeState:"unrequested",audioLevelReady:true};d.streams[e.deviceIP]=replacement;d.mu.Unlock();once.Do(func(){close(release)});cleanup.run();if r:=<-done;r.OK||r.Error!="stream was replaced"{t.Fatal(r)};if replacement.volume!=nil||replacement.audioClosed||replacement.audioDone!=nil{t.Fatal("replacement changed")}
 })
}
func TestAudioOwnerCloseOrdersAndNoVideoCancel(t *testing.T){
 t.Run("after_ack",func(t *testing.T){d,e,capture:=fixture(t,false,nil,func([]byte)([]byte,error){return wire(200),nil});var videoCancelled atomic.Bool;e.cancelFn=func(){videoCancelled.Store(true)};if !level(d,30).OK{t.Fatal("recovery failed")};await(t,func()bool{d.mu.Lock();defer d.mu.Unlock();return e.audioCapture!=nil});if r:=closeAudio(d);!r.OK{t.Fatal(r)};if capture.Stops.Load()!=1||videoCancelled.Load(){t.Fatal("close ownership",capture.Stops.Load(),videoCancelled.Load())};if r:=closeAudio(d);!r.OK||capture.Stops.Load()!=1{t.Fatal(r,capture.Stops.Load())}})
 t.Run("startup_timeout_then_join",func(t *testing.T){d,e,capture:=fixture(t,true,nil,func([]byte)([]byte,error){return wire(200),nil});entered:=make(chan struct{});release:=make(chan struct{});var once sync.Once;defer once.Do(func(){close(release)});var starts atomic.Int32;airplay.CaptureStart=func(context.Context,bool,int)(*airplay.AudioCapture,error){starts.Add(1);close(entered);<-release;return capture,nil};d.maybeStartAudio(e);<-entered
  if r:=closeAudio(d);r.OK||r.Error!="audio close not confirmed"{t.Fatal(r)};d.mu.Lock();original:=e.audioDone;if !e.audioClosed||!e.audioStarting{t.Fatal("owner retired at timeout")};d.mu.Unlock();once.Do(func(){close(release)});if r:=closeAudio(d);!r.OK{t.Fatal(r)};if capture.Stops.Load()!=1||starts.Load()!=1||original!=e.audioDone||e.audioCapture!=nil{t.Fatal("owner was replaced or stopped twice")}
 })
 t.Run("replacement_late_capture",func(t *testing.T){d,e,capture:=fixture(t,true,nil,func([]byte)([]byte,error){return wire(200),nil});entered:=make(chan struct{});release:=make(chan struct{});var once sync.Once;defer once.Do(func(){close(release)});airplay.CaptureStart=func(context.Context,bool,int)(*airplay.AudioCapture,error){close(entered);<-release;return capture,nil};d.maybeStartAudio(e);<-entered;d.mu.Lock();cleanup:=d.detachStreamLocked(e.deviceIP);replacement:=&activeStream{deviceIP:e.deviceIP,volumeState:"unrequested"};d.streams[e.deviceIP]=replacement;d.mu.Unlock();once.Do(func(){close(release)});cleanup.run();if capture.Stops.Load()!=1||replacement.audioDone!=nil||replacement.audioCapture!=nil{t.Fatal("obsolete capture published")}})
}
func socket(t *testing.T,d *Daemon,request string) Response {t.Helper();client,server:=net.Pipe();done:=make(chan struct{});go func(){defer close(done);d.handleConn(server)}();defer client.Close();if _,e:=client.Write([]byte(request+"\n"));e!=nil{t.Fatal(e)};var response Response;if e:=json.NewDecoder(client).Decode(&response);e!=nil{t.Fatal(e)};<-done;return response}
func TestActualSocketDecoderRejectsPresenceBeforeMutation(t *testing.T){
 var calls atomic.Int32;d,e,_:=fixture(t,true,nil,func([]byte)([]byte,error){calls.Add(1);return wire(200),nil})
 for _,tc:=range []struct{cmd,key,value string}{
 {"connect","audio","null"},{"connect","audio","0"},{"connect","audio","\"false\""},{"connect","volume","null"},{"connect","volume","false"},{"connect","volume","30.5"},{"connect","volume","101"},{"connect","volume","-1"},{"connect","volume","999999999999999999999999"},
 {"volume","volume","null"},{"volume","volume","true"},{"volume","volume","\"30\""},{"volume","volume","[]"},{"volume","volume","{}"},{"volume","volume","1e1"},{"volume","volume","100.0"},{"volume","volume","-1"},{"volume","volume","101"},{"volume","volume","999999999999999999999999"},
 {"audio","audio","null"},{"audio","audio","true"},{"audio","audio","0"},{"audio","audio","\"off\""},
 } {message:="invalid connect audio intent";if tc.cmd=="volume"{message="invalid volume request"};if tc.cmd=="audio"{message="invalid audio request"};request:=fmt.Sprintf(`{"cmd":%q,"target":"192.0.2.10",%q:%s}`,tc.cmd,tc.key,tc.value);if r:=socket(t,d,request);r.OK||r.Error!=message{t.Fatal(request,r)}}
 for _,cmd:=range []string{"volume","audio"}{key,value,message:="volume","0","invalid volume request";if cmd=="audio"{key,value,message="audio","false","invalid audio request"};for _,extra:=range []string{`,"port":0`,`,"pin":""`,`,"port":null`,`,"pin":null`}{r:=socket(t,d,fmt.Sprintf(`{"cmd":%q,"target":"192.0.2.10",%q:%s%s}`,cmd,key,value,extra));if r.OK||r.Error!=message{t.Fatal(r)}}}
 for _,request:=range []string{`{"cmd":"volume"}`,`{"cmd":"audio"}`,`{"cmd":"volume","volume":0,"audio":false}`,`{"cmd":"audio","audio":false,"volume":0}`} {if socket(t,d,request).OK{t.Fatal(request)}}
 if len(d.streams)!=1||e.audioClosed||e.volumeRunning||e.volumeSeq!=0||calls.Load()!=0{t.Fatal("decoder mutated state")}
 for _,request:=range []string{`{"cmd":"connect"}`,`{"cmd":"connect","audio":false,"volume":0}`,`{"cmd":"connect","audio":true,"volume":100}`,`{"cmd":"connect","target":"T","port":7000,"pin":"1234"}`} {req,err:=decodeControlRequest(json.NewDecoder(strings.NewReader(request)));if err!=nil||req.Cmd!="connect"{t.Fatal(request,req,err)}}
 if r:=socket(t,d,`{"cmd":"volume","target":"192.0.2.10","volume":0}`);!r.OK||r.Streams[0].Volume==nil||*r.Streams[0].Volume!=0{t.Fatal(r)}
 if r:=socket(t,d,`{"cmd":"audio","target":"192.0.2.10","audio":false}`);!r.OK||!r.Streams[0].AudioClosed{t.Fatal(r)}
 if r:=socket(t,d,`{"cmd":"status"}`);!r.OK{t.Fatal(r)}
}
func TestAdmittedIntentCopiesAndCredentialResume(t *testing.T){
 d,_,_:=fixture(t,true,nil,func([]byte)([]byte,error){return wire(200),nil});audio:=false;n:=0;r:=d.handleConnect(Request{Cmd:"connect",Target:"192.0.2.30",Audio:&audio,Volume:&n});if !r.OK{t.Fatal(r)};audio=true;n=80;d.mu.Lock();entry:=d.streams["192.0.2.30"];if !entry.audioClosed||*entry.initialAudio||*entry.initialVolume!=0||entry.volumeState!="unrequested"{t.Fatal("intent mutated")};entry.state=StatePINRequired;entry.credentialKind=CredentialKindPIN;d.mu.Unlock()
 if r:=d.handleConnect(Request{Cmd:"connect",Target:entry.deviceIP,Pin:"1234",Audio:&audio});r.OK||r.Error!="connect intent cannot change while waiting for credentials"{t.Fatal(r)}
 if r:=d.handleConnect(Request{Cmd:"connect",Target:entry.deviceIP,Pin:"1234"});!r.OK{t.Fatal(r)}
}
var _ = errors.New
'''


_VOLUME_CLIENT_BOUNDARY = r'''package daemonclient
import ("encoding/json"; "fmt"; "os"; "doubletake/internal/daemon")
var Sent [][]byte
func (c *Client) send(req daemon.Request)(*daemon.Response,error){
 if os.Getenv("VOLUME_CLI_CHILD")=="1" { panic("unexpected daemon client I/O") }
 b,e:=json.Marshal(req);if e!=nil{return nil,e};Sent=append(Sent,append(b,'\n'));return &daemon.Response{OK:true},nil
}
var _ = fmt.Sprint
'''


_VOLUME_CLIENT_TESTS = r'''package daemonclient
import ("bytes"; "testing")
func TestExactWrapperWireAndLegacyShape(t *testing.T){
 Sent=nil;c:=New("unused");on:=true;n:=30
 _,_=c.Connect("192.0.2.10",0,"");_,_=c.ConnectWithAudio("192.0.2.10",0,"",&on,&n);_,_=c.Volume(0,"");_,_=c.AudioOff("192.0.2.10")
 want:=[]string{"{\"cmd\":\"connect\",\"target\":\"192.0.2.10\"}\n","{\"cmd\":\"connect\",\"target\":\"192.0.2.10\",\"audio\":true,\"volume\":30}\n","{\"cmd\":\"volume\",\"volume\":0}\n","{\"cmd\":\"audio\",\"target\":\"192.0.2.10\",\"audio\":false}\n"}
 if len(Sent)!=len(want){t.Fatal(Sent)};for i,b:=range Sent{if !bytes.Equal(b,[]byte(want[i])){t.Fatalf("%s != %s",b,want[i])}}
}
'''


_VOLUME_CLI_TESTS = r'''package main
import ("encoding/json"; "os"; "os/exec"; "strings"; "testing")
func TestConnectGrammar(t *testing.T){
 for _,tc:=range []struct{args []string;target,pin string;audio *bool;volume *int}{
 {nil,"","",nil,nil},{[]string{"30"},"30","",nil,nil},{[]string{"T","30"},"T","30",nil,nil},
 {[]string{"--volume","000"},"","",nil,intp(0)},{[]string{"T","--audio","on","--volume","030","1234"},"T","1234",boolp(true),intp(30)},
 {[]string{"--audio","off"},"","",boolp(false),nil},{[]string{"--audio","on","--volume","100","secret"},"","secret",boolp(true),intp(100)},
 {[]string{"T","--volume","0"},"T","",nil,intp(0)},{[]string{"T","--audio","off","--volume","0"},"T","",boolp(false),intp(0)},
 } {got,e:=parseConnectArgs(tc.args);if e!=nil||got.Target!=tc.target||got.Pin!=tc.pin||(got.Audio==nil)!=(tc.audio==nil)||(got.Volume==nil)!=(tc.volume==nil){t.Fatal(tc.args,got,e)};if got.Audio!=nil&&*got.Audio!=*tc.audio{t.Fatal(got)};if got.Volume!=nil&&*got.Volume!=*tc.volume{t.Fatal(got)}}
 for _,args:=range [][]string{{"volume","0"},{"volume","030","T"},{"audio","off"},{"audio","off","T"}}{if _,e:=parseAudioArgs(args);e!=nil{t.Fatal(args,e)}}
}
func intp(n int)*int{return &n};func boolp(b bool)*bool{return &b}
func TestCLIChild(t *testing.T){if os.Getenv("VOLUME_CLI_CHILD")!="1"{return};var args []string;if e:=json.Unmarshal([]byte(os.Getenv("VOLUME_CLI_ARGS")),&args);e!=nil{panic(e)};os.Args=append([]string{"doubletake-ctl"},args...);main();os.Exit(0)}
func TestFixedParserErrorsBeforeClientIO(t *testing.T){
 type bad struct{args []string;message string};cases:=[]bad{
 {[]string{"connect","--audio"},"audio must be on or off"},{[]string{"connect","--audio","onward"},"audio must be on or off"},{[]string{"connect","--audio","--volume"},"audio must be on or off"},
 {[]string{"connect","T","--volume"},"volume must be an integer from 0 to 100"},{[]string{"connect","T","--volume","--audio"},"volume must be an integer from 0 to 100"},
 {[]string{"connect","--volume","30","--audio","on"},"invalid connect arguments"},{[]string{"connect","--volume=30"},"invalid connect arguments"},{[]string{"connect","--audio=on"},"invalid connect arguments"},
 {[]string{"connect","--audio","on","--audio","off"},"invalid connect arguments"},{[]string{"connect","--volume","30","--volume","40"},"invalid connect arguments"},
 {[]string{"connect","T","pin","--volume","30"},"invalid connect arguments"},{[]string{"connect","T","pin","extra"},"invalid connect arguments"},{[]string{"connect","--unknown"},"invalid connect arguments"},{[]string{"connect",""},"invalid connect arguments"},
 {[]string{"volume"},"volume must be an integer from 0 to 100"},{[]string{"volume","30","T","extra"},"Usage: doubletake-ctl volume <0-100> [target]"},{[]string{"volume","30","--target"},"Usage: doubletake-ctl volume <0-100> [target]"},
 {[]string{"audio"},"Usage: doubletake-ctl audio off [target]"},{[]string{"audio","on"},"Usage: doubletake-ctl audio off [target]"},{[]string{"audio","off","T","extra"},"Usage: doubletake-ctl audio off [target]"},{[]string{"audio","off","--target"},"Usage: doubletake-ctl audio off [target]"},
 }
 for _,operand:=range []string{"-1","101","1000","1.0","1e1","true"," 30","30 ","+30","３０","","9999999999999999999"}{cases=append(cases,bad{[]string{"connect","--volume",operand},"volume must be an integer from 0 to 100"},bad{[]string{"volume",operand},"volume must be an integer from 0 to 100"})}
 for _,tc:=range cases{encoded,_:=json.Marshal(tc.args);command:=exec.Command(os.Args[0],"-test.run=^TestCLIChild$");command.Env=append(os.Environ(),"VOLUME_CLI_CHILD=1","VOLUME_CLI_ARGS="+string(encoded));var stdout,stderr strings.Builder;command.Stdout=&stdout;command.Stderr=&stderr;err:=command.Run();exit,ok:=err.(*exec.ExitError);if !ok||exit.ExitCode()!=1||stdout.String()!=""||stderr.String()!=tc.message+"\n"{t.Fatalf("%q: err=%v stdout=%q stderr=%q",tc.args,err,stdout.String(),stderr.String())}}
}
'''
