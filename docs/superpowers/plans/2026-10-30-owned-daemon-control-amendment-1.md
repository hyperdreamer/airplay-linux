# Amendment 1 to the owned-daemon control plan — operator-authorised scope change

- **Applies to:** `docs/superpowers/plans/2026-10-30-owned-daemon-control.md` (plan v2, the reviewed contract)
- **Authorised by:** the operator, run `pm-run-20261030-040814-aefef9c0`, design-review gate site 3
- **Operator reply, verbatim:** `A`
- **Reason:** design review 2, finding F-007. The delivered `connect` admission requires four
  pairing-transaction capabilities (`target_bound_pairing`, `provisional_commit`, `cancel_pairing`,
  `revoke_pairing`) that the patched backend cannot prove and never will without a pairing protocol. Leaving them on
  `connect` makes Mirror unreachable, which contradicts the epoch objective. The contract forbids weakening a gate
  without an explicit scope decision, so this document records that decision rather than reading it into the design.

## 1. What changes

Exactly one admission precondition list changes. Nothing else in the contract changes.

**Before** (`backend.control.PublishedDoubletakeControl._available`, `connect`):

```text
foreground_control and owner_handshake and multi_target and target_bound_pairing
and (physical_source or virtual_source) and provisional_commit
and cancel_pairing and revoke_pairing
```

**After**:

```text
owned_daemon_control and multi_target and (physical_source or virtual_source)
and trust-not-blocked and dispatch-fence-valid
```

`owned_daemon_control` and (where applicable) `physical_source` are composition-derived and may be true only when the
verifier and source verification exist and are tested (`design §4.1`, `§4.3`). `trust-not-blocked` is the delivered
fence (`CleanupRequired`/`RevocationPending` refuse) and is unchanged. `dispatch-fence-valid` is the delivered
receiver/endpoint-generation/fence check and is unchanged.

## 2. Mandatory terms of this authorisation

1. **No pairing at connect.** A credential challenge announced by the acknowledgement or by `status` causes that exact
   target to be disconnected and the start to be refused with `dependency_incompatible`. The application never prompts
   for a credential, never submits one, never leaves a stream behind, and never reports a pairing capability it does not
   have.
2. **The challenge window is bounded and observed.** The post-connect wait that reported-source verification already
   requires (`design §7`) is the same bounded wait that observes a credential challenge, so a credential-waiting start
   cannot sit unnoticed between `connect` and verification.
3. **This amendment is in the contract, not the design.** This file is the authority for the changed predicate; the
   design and the implementation plan cite it.

## 3. What stays closed

Every pairing operation (`begin`, `submit`, `prepare`, `commit`, `cancel`, `revoke`, `mark_invalid`), remembered trust
(`remembered_pairing`), provisional-commit compensation, cancellation, revocation, `virtual_source`, Virtual mode, and
every receiver, trust, provider, target-generation, credential-service and cleanup gate keep their delivered
requirements. The capability report continues to name the withheld pairing flags. The cooperative same-user threat
model and its residual are unchanged.

## 4. Residual this authorisation accepts

The application may start a stream to a receiver it cannot re-pair. If that receiver later demands a credential, every
subsequent start fails with a typed refusal — never with an unverified stream and never with a secret prompt it cannot
service.

## 5. Effect on acceptance

T7's activation test must prove, on a public-shape composition (`foreground_control=False`, `owner_handshake=False`,
pairing flags false, status responses carrying no backend ownership metadata):

1. `connect` is admitted when the owned-daemon capability, the reported-source verifier, clean trust and a valid fence
   are all present;
2. each of those four is removed in turn and `connect` refuses before any I/O, with zero production spawns;
3. a credential challenge disconnects exactly that target and returns the typed refusal, leaving no stream;
4. all four former pairing prerequisites are independently proven *not* to be consulted by `connect`, and to still be
   required by every pairing operation.

## 6. Clarification 1.1 (draftsmanship, no decision change)

§5 item 4's closing phrase — "and to still be required by every pairing operation" — is clarified as follows, because
as written it can be read as adding the four former connect prerequisites to operations that never required them:

- every pairing operation still requires **its own delivered prerequisite list** (Amendment §3), exactly as
  `backend.control.PublishedDoubletakeControl._available` defines it today;
- the phrase does not add a prerequisite to any operation, and does not remove one from any operation;
- the other reading would contradict §3 ("keeps its delivered requirements") and would *strengthen* operations outside
  the authorised change, which this amendment does not authorise.

No operator decision changes. `connect` moves (§1, §2); every pairing operation keeps the predicate it has today.
