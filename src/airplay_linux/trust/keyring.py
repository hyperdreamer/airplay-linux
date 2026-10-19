"""Backend-owned keyring transactions; handles are private, transient adapter state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable
from uuid import uuid4

from airplay_linux.backend.contract import AckReply, BackendFeatures, StatusReply
from airplay_linux.backend.control import BackendControl, BackendOperation, EmptyPayload
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.identity import ReceiverId, ReceiverTarget
from airplay_linux.domain.operations import (
    ChallengeId,
    OpaqueBackendToken,
    OpaqueCredentialRef,
    OpaqueProvisionalCredentialRef,
    OperationContext,
    PairingChallenge,
)
from airplay_linux.domain.ports import CredentialStore
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result


@dataclass(frozen=True, kw_only=True)
class KeyringPairing:
    backend_token: OpaqueBackendToken = field(repr=False)
    credential_kind: Literal["pin", "password"]


@runtime_checkable
class KeyringPairingPort(CredentialStore, Protocol):
    """Trust-local extension: the frozen credential port has no begin operation."""

    async def begin_pairing_token(
        self, target: ReceiverTarget, context: OperationContext,
    ) -> Result[KeyringPairing]: ...


@dataclass(kw_only=True, repr=False)
class _Transaction:
    target: ReceiverTarget
    context: OperationContext
    token: OpaqueBackendToken
    challenge_id: ChallengeId | None = None
    provisional: OpaqueProvisionalCredentialRef | None = None
    credential: OpaqueCredentialRef | None = None
    cancelled: bool = False
    cancel_confirmed: bool = False


def _unavailable() -> Err:
    return Err(error=diagnostic(ErrorCode.keyring_unavailable))


def _acknowledged(value: object, operation: BackendOperation, target: ReceiverTarget) -> bool:
    return (
        isinstance(value, AckReply)
        and value.operation == operation.value
        and value.target == target.canonical_host
    )


class BackendKeyringStore:
    """Only affirmed backend operations can acquire or revoke trust.

    Tokens identify adapter-internal transactions, never backend response text or
    user input. The control contract routes operations by the retained exact
    target. There is no file fallback, secret read, or invented wire field.
    """

    def __init__(self, control: BackendControl, features: BackendFeatures) -> None:
        self._control = control
        self._features = features
        self._transactions: dict[OpaqueBackendToken, _Transaction] = {}
        self._provisionals: dict[OpaqueProvisionalCredentialRef, _Transaction] = {}
        self._credentials: dict[ReceiverId, OpaqueCredentialRef] = {}
        self._revoked: set[ReceiverId] = set()

    def __repr__(self) -> str:
        return "BackendKeyringStore(transactions=<redacted>)"

    def _available(self) -> bool:
        return all((
            self._features.target_bound_pairing,
            self._features.keyring,
            self._features.provisional_commit,
            self._features.cancel_pairing,
            self._features.revoke_pairing,
        ))

    async def _request(self, operation: BackendOperation, transaction: _Transaction):
        try:
            result = await self._control.request(
                operation, transaction.target, EmptyPayload(), transaction.context,
            )
        except Exception:
            return _unavailable()
        if not isinstance(result, Ok):
            return _unavailable()
        return result

    def _current(self, transaction: _Transaction) -> bool:
        current = next((
            item for item in reversed(self._transactions.values())
            if item.target.receiver_id == transaction.target.receiver_id
        ), None)
        return current is transaction

    def _owns(self, transaction: _Transaction, challenge: PairingChallenge) -> bool:
        return (
            transaction.target.receiver_id == challenge.receiver_id
            and transaction.target.endpoint_fingerprint == challenge.endpoint_fingerprint
            and transaction.target.endpoint_generation == challenge.endpoint_generation
            and transaction.context.endpoint_generation == challenge.endpoint_generation
            and transaction.context.session_generation == challenge.session_generation
            and (transaction.context.trust_generation or 0) == challenge.trust_generation
            and transaction.challenge_id in (None, challenge.challenge_id)
        )

    async def begin_pairing_token(
        self, target: ReceiverTarget, context: OperationContext,
    ) -> Result[KeyringPairing]:
        if not self._available() or context.endpoint_generation != target.endpoint_generation:
            return _unavailable()
        if any(
            item.target.receiver_id == target.receiver_id
            and not item.cancel_confirmed
            and (item.credential is None or item.cancelled)
            for item in self._transactions.values()
        ):
            return _unavailable()
        transaction = _Transaction(
            target=target, context=context, token=OpaqueBackendToken(uuid4().hex),
        )
        self._transactions[transaction.token] = transaction
        self._revoked.discard(target.receiver_id)
        result = await self._request(BackendOperation.begin_pairing, transaction)
        if isinstance(result, Ok) and _acknowledged(result.value, BackendOperation.begin_pairing, target):
            # An acknowledgement has no credential kind. Only the typed status
            # for this exact target can establish whether to prompt for a PIN
            # or password; free-form ack state never supplies a token or kind.
            try:
                result = await self._control.request(
                    BackendOperation.status, None, EmptyPayload(), context,
                )
            except Exception:
                result = _unavailable()
        if isinstance(result, Ok) and isinstance(result.value, StatusReply):
            status = result.value
            if (
                status.target == target.canonical_host
                and status.state == "pin_required"
                and status.needs_credential
                and status.credential_kind in ("pin", "password")
            ):
                return Ok(value=KeyringPairing(
                    backend_token=transaction.token, credential_kind=status.credential_kind,
                ))
        # Begin may have acquired a prompt even when its reply was unusable.
        # Keep the handle on failure so receiver-scoped retry can compensate.
        cancelled = await self._request(BackendOperation.cancel_pairing, transaction)
        if isinstance(cancelled, Ok) and _acknowledged(cancelled.value, BackendOperation.cancel_pairing, target):
            self._transactions.pop(transaction.token, None)
            return _unavailable()
        return Err(error=diagnostic(ErrorCode.trust_cleanup_required))

    async def load_credential_ref(self, receiver_id: ReceiverId) -> Result[OpaqueCredentialRef | None]:
        if not self._available():
            return _unavailable()
        # Never fabricate a remembered credential for an unknown receiver. The
        # integrated wire contract has no credential-load reply to recover one.
        return Ok(value=self._credentials.get(receiver_id))

    async def prepare_pairing(
        self, challenge: PairingChallenge, backend_result: OpaqueBackendToken,
    ) -> Result[OpaqueProvisionalCredentialRef]:
        if not self._available() or backend_result != challenge.backend_token:
            return _unavailable()
        transaction = self._transactions.get(challenge.backend_token)
        if transaction is None or transaction.cancelled or not self._current(transaction) or not self._owns(transaction, challenge):
            return _unavailable()
        transaction.challenge_id = challenge.challenge_id
        result = await self._request(BackendOperation.prepare_pairing, transaction)
        if isinstance(result, Err) or not _acknowledged(result.value, BackendOperation.prepare_pairing, transaction.target):
            return _unavailable()
        if transaction.cancelled:
            return _unavailable()
        if transaction.provisional is None:
            transaction.provisional = OpaqueProvisionalCredentialRef(uuid4().hex)
        self._provisionals[transaction.provisional] = transaction
        return Ok(value=transaction.provisional)

    async def commit_pairing(
        self, provisional_ref: OpaqueProvisionalCredentialRef, trust_generation: int,
    ) -> Result[OpaqueCredentialRef]:
        if not self._available():
            return _unavailable()
        transaction = self._provisionals.get(provisional_ref)
        if transaction is None or transaction.cancelled or not self._current(transaction) or trust_generation != (transaction.context.trust_generation or 0):
            return _unavailable()
        result = await self._request(BackendOperation.commit_pairing, transaction)
        if isinstance(result, Err) or not _acknowledged(result.value, BackendOperation.commit_pairing, transaction.target):
            return _unavailable()
        if transaction.cancelled:
            return _unavailable()
        if transaction.credential is None:
            transaction.credential = OpaqueCredentialRef(uuid4().hex)
        self._credentials[transaction.target.receiver_id] = transaction.credential
        return Ok(value=transaction.credential)

    async def cancel_pairing(self, challenge: PairingChallenge) -> Result[None]:
        if not self._available():
            return _unavailable()
        transaction = self._transactions.get(challenge.backend_token)
        if transaction is None or not self._current(transaction) or not self._owns(transaction, challenge):
            return _unavailable()
        transaction.cancelled = True
        result = await self._request(BackendOperation.cancel_pairing, transaction)
        if isinstance(result, Err) or not _acknowledged(result.value, BackendOperation.cancel_pairing, transaction.target):
            return _unavailable()
        transaction.cancel_confirmed = True
        return Ok(value=None)

    async def revoke_pairing(
        self, ref_or_receiver: OpaqueProvisionalCredentialRef | ReceiverId,
    ) -> Result[None]:
        if not self._available():
            return _unavailable()
        if isinstance(ref_or_receiver, OpaqueProvisionalCredentialRef):
            transaction = self._provisionals.get(ref_or_receiver)
            if transaction is None:
                return _unavailable()
            receiver_id = transaction.target.receiver_id
        else:
            transaction = None
            receiver_id = ref_or_receiver
        transactions = [item for item in self._transactions.values() if item.target.receiver_id == receiver_id]
        if not transactions:
            return Ok(value=None) if receiver_id in self._revoked else _unavailable()
        # The wire contract is target-scoped. Only the current provisional may
        # trigger a receiver revoke; an older handle cannot touch a newer pairing.
        if transaction is not None and transactions[-1] is not transaction:
            return _unavailable()
        for transaction in transactions:
            transaction.cancelled = True
            transaction.cancel_confirmed = False
        current = transactions[-1]
        result = await self._request(BackendOperation.revoke_pairing, current)
        if isinstance(result, Err) or not _acknowledged(result.value, BackendOperation.revoke_pairing, current.target):
            return _unavailable()
        for transaction in transactions:
            self._transactions.pop(transaction.token, None)
            if transaction.provisional is not None:
                self._provisionals.pop(transaction.provisional, None)
        self._credentials.pop(receiver_id, None)
        self._revoked.add(receiver_id)
        return Ok(value=None)

    async def mark_invalid(self, receiver_id: ReceiverId) -> Result[None]:
        if not self._available():
            return _unavailable()
        self._credentials.pop(receiver_id, None)
        for transaction in self._transactions.values():
            if transaction.target.receiver_id == receiver_id:
                transaction.cancelled = True
                transaction.cancel_confirmed = False
        # Invalidation is local and immediate. Retain the target/handles until
        # the coordinator's acquisition barrier drains: only then can a revoke
        # confirm that no late backend commit will recreate the credential.
        return Ok(value=None)


__all__ = ["BackendKeyringStore", "KeyringPairing", "KeyringPairingPort"]
