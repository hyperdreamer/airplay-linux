"""Widget-level tests for the dense operations dashboard and its prompts.

Every assertion here is deliberately narrow about *rendering*: the widgets are
handed an already-decided projection (``PaneState``/``ReceiverRow``) and must
show exactly that.  Tests build projections by hand with implausible content so
a widget that recomputed its own phase or action set would fail immediately.
They also pin the accessibility contract (tooltips, accessible names, tab order)
and the transient, never-echoed handling of pairing secrets.

No test needs a visible desktop: ``QT_QPA_PLATFORM=offscreen`` is enough.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QToolButton,
    QWidget,
)

from airplay_linux.domain.identity import ReceiverId, ReceiverRecordSet, ReceiverView
from airplay_linux.domain.operations import ChallengeId, ChallengeView, OperationId, OwnerId
from airplay_linux.domain.records import (
    LayoutSnapshot,
    OwnershipRecord,
    RecoveryItem,
    SessionSnapshot,
)
from airplay_linux.domain.resources import (
    CapabilityReport,
    Mode,
    SessionPhase,
    Size,
    TrustState,
)
from airplay_linux.testing.builders import physical, service, target
from airplay_linux.ui.state import (
    Action,
    Badge,
    PaneState,
    ReceiverRow,
    ViewInputs,
    project,
    project_row,
)

RECEIVER = ReceiverId("airplay-device:a")
OTHER_RECEIVER = ReceiverId("airplay-device:b")
HD = Size(width=1920, height=1080)
SMALL = Size(width=1280, height=720)


# --------------------------------------------------------------------------- fixtures


def receiver_row(  # noqa: PLR0913 - mirrors the frozen row record
    name: str = "Living Room",
    receiver: str = "a",
    mode: str = "Mirror",
    phase: str = "Streaming",
    size: str = "",
    online: bool = True,
) -> ReceiverRow:
    return ReceiverRow(
        receiver_id=ReceiverId(f"airplay-device:{receiver}"),
        name=name,
        mode_label=mode,
        phase_label=phase,
        size_label=size,
        online=online,
    )


def capabilities(**overrides: object) -> CapabilityReport:
    defaults: dict[str, object] = {
        "environment": "kde-plasma-6-wayland",
        "discovery": True,
        "mirror": True,
        "virtual_display": True,
        "audio": True,
        "remembered_pairing": True,
        "additional_virtual_outputs": True,
        "diagnostics": (),
    }
    return CapabilityReport(**(defaults | overrides))  # type: ignore[arg-type]


def view_inputs(**overrides: object) -> ViewInputs:
    defaults: dict[str, object] = {
        "receiver": ReceiverView(
            record_set=ReceiverRecordSet(
                records=(service(),),
                receiver_id=RECEIVER,
                identity_kind="device",
                canonical_address="192.0.2.10",
                endpoint_fingerprint="endpoint-a-v1",
                endpoint_generation=1,
                persistable=True,
                continuity_verified=True,
                replacement_of=None,
                ambiguous=False,
            ),
            display_name="Living Room",
            online=True,
            pairing_hint=None,
        ),
        "saved": None,
        "capabilities": capabilities(),
        "selected_mode": Mode.MIRROR,
        "physical_sources": (physical(),),
        "resolution_choices": (HD, SMALL),
    }
    return ViewInputs(**(defaults | overrides))  # type: ignore[arg-type]


def idle_snapshot(**overrides: object) -> SessionSnapshot:
    defaults: dict[str, object] = {
        "receiver_id": RECEIVER,
        "target": target(),
        "online": True,
        "phase": SessionPhase.Available,
    }
    return SessionSnapshot(**(defaults | overrides))  # type: ignore[arg-type]


def pane(  # noqa: PLR0913 - explicit projection fields keep each test self-documenting
    *,
    title: str = "Living Room",
    phase_label: str = "Available",
    badges: tuple[Badge, ...] | None = None,
    actions: frozenset[Action] = frozenset(),
    mode_enabled: bool = True,
    visible_mode: Mode = Mode.MIRROR,
    primary_label: str = "Start",
    source_enabled: bool = False,
    resolution_enabled: bool = False,
    mute_enabled: bool = False,
    muted: bool = False,
    challenge: ChallengeView | None = None,
    notice: str | None = None,
    diagnostic: object | None = None,
) -> PaneState:
    return PaneState(
        title=title,
        phase_label=phase_label,
        badges=(
            (
                Badge(resource="transport", label="Streaming", severity="ok"),
                Badge(resource="display", label="No output", severity="info"),
                Badge(resource="audio", label="Audio on", severity="ok"),
                Badge(resource="trust", label="Paired", severity="ok"),
            )
            if badges is None
            else badges
        ),
        actions=actions,
        mode_enabled=mode_enabled,
        visible_mode=visible_mode,
        primary_label=primary_label,
        source_enabled=source_enabled,
        resolution_enabled=resolution_enabled,
        mute_enabled=mute_enabled,
        muted=muted,
        challenge=challenge,
        notice=notice,
        diagnostic=diagnostic,  # type: ignore[arg-type]
    )


def challenge(kind: str = "pin") -> ChallengeView:
    return ChallengeView(
        challenge_id=ChallengeId(UUID(int=11)),
        receiver_id=RECEIVER,
        endpoint_fingerprint="endpoint-a-v1",
        endpoint_generation=1,
        operation_id=OperationId(UUID(int=12)),
        session_generation=1,
        trust_generation=1,
        created_at=0.0,
        expires_at=120.0,
        credential_kind=kind,  # type: ignore[arg-type]
    )


def ownership(**overrides: object) -> OwnershipRecord:
    defaults: dict[str, object] = {
        "schema_version": 1,
        "owner_uuid": OwnerId(UUID(int=5)),
        "app_instance_uuid": OwnerId(UUID(int=5)),
        "runtime_directory": "/run/user/1000/airplay-linux/instances/5",
        "creation_operation_id": OperationId(UUID(int=6)),
        "receiver_id": RECEIVER,
        "endpoint_fingerprint": "endpoint-a-v1",
        "desired_state": "active",
        "stage": "published",
        "requested_size": HD,
        "observed_size": HD,
        "helper": None,
        "connector": "Virtual-v1",
        "output_id": "v1",
        "created_utc": "2026-10-17T00:00:00Z",
        "last_configuration_step": "published",
        "baseline": LayoutSnapshot(outputs={}, baseline_hash="baseline"),
        "last_applied": {},
        "provider_version": "24.08.2",
        "capability_fingerprint": "fingerprint",
    }
    return OwnershipRecord(**(defaults | overrides))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- models


def test_receiver_model_renders_each_projected_column() -> None:
    from airplay_linux.ui.models import ReceiverListModel

    model = ReceiverListModel()
    model.replace(
        (
            receiver_row(name="Living Room", mode="Mirror", phase="Streaming"),
            receiver_row(
                name="Office TV",
                receiver="b",
                mode="Virtual display",
                phase="Virtual display",
                size="3840x2160",
            ),
        )
    )

    assert model.rowCount() == 2
    assert model.columnCount() == 4
    assert [model.headerData(i, Qt.Orientation.Horizontal) for i in range(4)] == [
        "Device",
        "Mode",
        "State",
        "Size",
    ]
    assert [
        model.data(model.index(0, column), Qt.ItemDataRole.DisplayRole)
        for column in range(4)
    ] == ["Living Room", "Mirror", "Streaming", ""]
    assert [
        model.data(model.index(1, column), Qt.ItemDataRole.DisplayRole)
        for column in range(4)
    ] == ["Office TV", "Virtual display", "Virtual display", "3840x2160"]


def test_receiver_model_maps_indexes_back_to_receiver_id() -> None:
    from airplay_linux.ui.models import ReceiverListModel

    model = ReceiverListModel()
    model.replace((receiver_row(), receiver_row(name="Office TV", receiver="b")))

    assert model.receiver_id(model.index(0, 0)) == RECEIVER
    assert model.receiver_id(model.index(1, 3)) == OTHER_RECEIVER
    assert model.receiver_id(QModelIndex()) is None
    assert model.receiver_id(model.index(2, 0)) is None


def test_receiver_model_offline_and_empty_states_stay_readable() -> None:
    """An offline row and an empty list must both say what they are in words."""
    from airplay_linux.ui.models import ReceiverListModel

    model = ReceiverListModel()
    assert model.rowCount() == 0
    # An empty model still exposes its headers so the list is not a blank hole.
    assert model.headerData(0, Qt.Orientation.Horizontal) == "Device"

    model.replace(
        (
            receiver_row(
                name="Bedroom TV",
                receiver="b",
                mode="",
                phase="Offline",
                size="",
                online=False,
            ),
        )
    )
    index = model.index(0, 0)
    assert model.data(index, Qt.ItemDataRole.AccessibleTextRole) == "Bedroom TV, offline"
    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "Offline"
    assert (
        model.data(model.index(0, 2), Qt.ItemDataRole.AccessibleTextRole) == "Offline"
    )
    assert model.data(model.index(0, 3), Qt.ItemDataRole.DisplayRole) == ""
    assert (
        model.data(model.index(0, 3), Qt.ItemDataRole.AccessibleTextRole)
        == "No output size"
    )
    tooltip = model.data(index, Qt.ItemDataRole.ToolTipRole)
    assert "offline" in tooltip.lower()
    assert "Bedroom TV" in tooltip


def test_receiver_model_replace_is_one_atomic_reset() -> None:
    from airplay_linux.ui.models import ReceiverListModel

    model = ReceiverListModel()
    model.replace((receiver_row(),))
    resets: list[None] = []
    model.modelReset.connect(lambda: resets.append(None))

    model.replace((receiver_row(name="Office TV", receiver="b"),))

    assert len(resets) == 1
    assert model.rowCount() == 1
    assert model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole) == "Office TV"


def test_receiver_model_rejects_non_row_values_without_losing_rows() -> None:
    from airplay_linux.ui.models import ReceiverListModel

    model = ReceiverListModel()
    model.replace((receiver_row(),))

    with pytest.raises(TypeError):
        model.replace((object(),))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        model.replace("Living Room")  # type: ignore[arg-type]

    assert model.rowCount() == 1
    assert model.receiver_id(model.index(0, 0)) == RECEIVER


def test_receiver_model_reports_labels_verbatim_without_inventing_state() -> None:
    """The model is a view of the projection: odd labels survive unchanged."""
    from airplay_linux.ui.models import ReceiverListModel

    model = ReceiverListModel()
    model.replace(
        (
            receiver_row(
                name="Mystery Box",
                receiver="m",
                mode="Sideways",
                phase="Quantum flux",
                size="1x1",
            ),
        )
    )

    assert [
        model.data(model.index(0, column), Qt.ItemDataRole.DisplayRole)
        for column in range(4)
    ] == ["Mystery Box", "Sideways", "Quantum flux", "1x1"]
    assert model.flags(model.index(0, 0)) & Qt.ItemFlag.ItemIsSelectable
    assert not model.flags(model.index(0, 0)) & Qt.ItemFlag.ItemIsEditable
