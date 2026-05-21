# Spec D — RU Image Build Pipeline (D1: build + upstream-tracking + image catalog)

Status: **Draft, awaiting operator review.**
Predecessor: `doc/design.md` §9 (T4 Image-Currency Process) + §2 (Transport), `doc/specs/2026-05-18-A-controller-state-and-backup.md`, `doc/specs/2026-05-20-F-eu-node-setup.md`.
Successors blocked on this: `D2` (canary + validation gate + profile pinning — itself blocked on specs I, G, H), `E` (RU node init — reads the promoted image), `G` (RU provisioning artifact generator — embeds the binary URL in the seed bundle).

---

## 1. Purpose

Build the artifact every RU box will be provisioned from, track upstream MTProto releases so the artifact never silently drifts onto a stale build, and maintain a controller-state catalogue of every image we have ever built, promoted, or retired. This is **D1** — the buildable subset of T4. Steps 3–6 of design §9 (T3 profile pinning, canary soak, validation gate, atomic profile re-pin) are deferred to **D2** because they are structurally blocked on specs I (probe vantages), G (provisioning artifact generator), and H (shard manager).

Spec A's `ru_boxes.image_version` column was always a forward reference to this spec. Spec D1 now provides the table that column points at, the build pipeline that fills it, and the obligation tracking that ensures upstream releases never go unnoticed.

Out of scope: the canary deployment plumbing, the T3 hard-kill profile pinning, the validation gate, the atomic profile re-pin on promote, the "soft-burn all boxes built from a retired image" rollback mechanism. All deferred to **D2** with a placeholder operator-attested `--evidence` flag on `image-promote` standing in for the gate.

---

## 2. Locked design decisions

Approved during brainstorming session 2026-05-21.

| ID | Decision | Rationale |
|---|---|---|
| D-D1 | **Scope = D1 (build + upstream-tracking + image catalog).** Canary, validation gate, profile pinning, atomic re-pin deferred to **D2**. | Steps 3–6 of design §9 are structurally blocked on specs I/G/H. Shipping D1 alone delivers a buildable artifact and a tracked upstream-currency obligation without over-promising the gate. Matches the project's split-aggressively pattern. |
| D-D2 | **Upstream implementation = `9seconds/mtg`.** | Go reimplementation with first-class Fake-TLS support, actively maintained, small static binary (~10MB), tagged GitHub releases with checksum files. The de-facto modern choice for MTProto+Fake-TLS. |
| D-D3 | **Artifact = static binary + JSON manifest, stored in B2 under `images/<image_version>/`.** | mtg is a single static Go binary; a container runtime adds nothing. Reusing the backup bucket avoids new infrastructure and avoids creating an attributable public registry target. Distribution (signed URLs, cloud-init embedding) is spec G's problem. |
| D-D4 | **Binary acquisition = download upstream's GitHub release artifact + verify sha256 against upstream's published checksum.** No source builds. | Lowest-friction, no Go toolchain dependency on the build host. Supply-chain trust = "we trust GitHub releases of 9seconds/mtg" — the same trust we already require for the source. |
| D-D5 | **Upstream tracking = APScheduler weekly poll of GitHub releases API.** Anti-obligation `t4_upstream_release_available::<tag>` emitted when newer release available; `t4_upstream_check` heartbeat proven on each tick. | Closes the silent-staleness failure mode T4 exists to prevent (design §9 honest residuals). New runtime dep: HTTPS reachability to `api.github.com` (unauthenticated, 60 req/hr is plenty for weekly polls). |
| D-D6 | **`image-promote` is operator-attested via `--evidence`.** Atomic in a single transaction: candidate → promoted; prior promoted (if any) → retired; obligation re-stamping. | Placeholder for D2's real validation gate. Matches spec C's vantage-attest pattern: operator runs whatever probes they have, types/pastes evidence, the audit row captures it verbatim. |
| D-D7 | **`image_version = hex sha256 of the binary`** — content-addressed. | The cattle model treats a binary's identity as its hash. Two builds of the same release deduplicate naturally (PRIMARY KEY collision). No build-timestamp dependence on image identity. |
| D-D8 | **B2 layout: `images/<image_version>/{mtg,manifest.json}`. Object Lock COMPLIANCE on both.** | Image binaries are immutable by definition (sha256-addressed). Object Lock here is durability, not security. B2 retention bloat from never-promoted candidates is documented as accepted residual. |

---

## 3. Schema additions (v4 → v5)

```sql
CREATE TABLE ru_images (
  image_version       TEXT    PRIMARY KEY,           -- hex sha256 of the binary
  upstream_release    TEXT    NOT NULL,               -- e.g. "v2.1.7"
  upstream_repo       TEXT    NOT NULL,               -- e.g. "9seconds/mtg"
  binary_url          TEXT    NOT NULL,               -- B2 key: "images/<image_version>/mtg"
  manifest_url        TEXT    NOT NULL,               -- B2 key: "images/<image_version>/manifest.json"
  binary_sha256       TEXT    NOT NULL,
  binary_size_bytes   INTEGER NOT NULL,
  state               TEXT    NOT NULL CHECK (state IN ('candidate','promoted','retired')),
  built_at            TEXT    NOT NULL,
  promoted_at         TEXT,
  retired_at          TEXT,
  notes               TEXT
);

CREATE INDEX ix_ru_images_state ON ru_images(state);
```

**Schema version bump:** `meta.schema_version` advances `4 → 5`. Forward-only migration `migrate_v4_to_v5(conn)` creates the table + index. No data backfill — the existing `ru_boxes.image_version` column stays free-text TEXT for now; D2 may add an FK constraint when boxes are actually provisioned from catalog rows.

**State transitions allowed:**

```
              insert (image-build)
                   │
                   ▼
              [candidate]
              │        │
   image-     │        │ image-retire (discard)
   promote    │        ▼
              │     [retired]
              ▼
           [promoted]
              │
   image-     │
   retire     ▼
           [retired]
```

The `promoted → retired` direction happens both via explicit `image-retire` and implicitly when another candidate is promoted (the prior promoted row atomically becomes retired in the same transaction).

---

## 4. Invariants (extending spec F's #21–#23)

- **#24 — Singleton promoted.** `SELECT COUNT(*) FROM ru_images WHERE state='promoted'` ≤ 1. Zero is allowed during early operation before any promotion; never two.
- **#25 — State timestamps consistent.**
  - `state='promoted'` ⇔ `promoted_at IS NOT NULL`
  - `state='retired'` ⇔ `retired_at IS NOT NULL`
  - `state='candidate'` ⇔ `promoted_at IS NULL AND retired_at IS NULL`

Each check raises `InvariantViolation` with the violating row(s). Failures here would only come from manual SQL tampering — the repository functions in §5 are transaction-bounded to preserve both invariants.

---

## 5. Repository API + B2 layout

### 5.1 `mthydra.controller.state.ru_images`

Thin repository, spec-A pattern. All functions take `sqlite3.Connection` + explicit timestamps; emit audit rows for every state transition.

```python
@dataclass(frozen=True)
class RUImage:
    image_version: str
    upstream_release: str
    upstream_repo: str
    binary_url: str
    manifest_url: str
    binary_sha256: str
    binary_size_bytes: int
    state: str
    built_at: str
    promoted_at: str | None
    retired_at: str | None
    notes: str | None


def insert_candidate(
    conn, *, image_version, upstream_release, upstream_repo,
    binary_url, manifest_url, binary_sha256, binary_size_bytes,
    built_at, notes=None, actor="operator",
) -> None
    # state='candidate'; emits audit_log action='image_built'

def promote(conn, image_version, *, at, evidence, actor="operator") -> None
    # Single transaction:
    #   1. Verify the target row is state='candidate' (raises ValueError otherwise).
    #   2. If a prior 'promoted' row exists: UPDATE to state='retired', retired_at=at.
    #   3. UPDATE target row: state='promoted', promoted_at=at.
    #   4. log_event action='image_promoted', target=image_version,
    #        details_json={"evidence": evidence, "retired_predecessor": <prior_iv or null>}.
    #   5. prove obligation 't4_image_promoted' with next_due = at + 30d.
    #   6. DELETE obligation 't4_upstream_release_available::<upstream_release>'
    #      (the new release has been acted on; clear the anti-obligation).

def retire(conn, image_version, *, at, reason, actor="operator") -> None
    # state -> 'retired'. Legal from 'candidate' or 'promoted'.
    # Retiring a 'promoted' row leaves the fleet with no current default;
    # the CLI prints a warning, the function itself just records it.
    # Emits action='image_retired', details_json={"reason": reason, "prior_state": ...}.

def current_promoted(conn) -> RUImage | None
    # SELECT * WHERE state='promoted' LIMIT 1. None if no image promoted yet.

def list_images(conn, *, state=None) -> list[RUImage]
def get_image(conn, image_version) -> RUImage  # raises LookupError on miss
```

### 5.2 B2 layout

```
images/<image_version>/mtg              # the binary (raw bytes), Object Lock COMPLIANCE
images/<image_version>/manifest.json    # metadata,                Object Lock COMPLIANCE
```

**Manifest format:**

```json
{
  "schema": "mthydra.ru_image.v1",
  "image_version": "abc123...",
  "upstream_repo": "9seconds/mtg",
  "upstream_release": "v2.1.7",
  "binary_filename": "mtg-linux-amd64",
  "binary_sha256": "abc123...",
  "binary_size_bytes": 10485760,
  "built_at": "2026-05-21T12:00:00Z",
  "built_by": "operator"
}
```

### 5.3 `S3Destination` extensions

Mirrors the heartbeat methods from spec F:

```python
@staticmethod
def _image_binary_key(image_version: str) -> str:
    return f"images/{image_version}/mtg"

@staticmethod
def _image_manifest_key(image_version: str) -> str:
    return f"images/{image_version}/manifest.json"

def put_image(self, *, image_version: str, binary_path: Path, manifest: bytes) -> None
    """Upload binary + manifest to B2, both under Object Lock COMPLIANCE."""

def head_image(self, *, image_version: str) -> dict[str, Any] | None
    """Returns binary head info ({'etag', 'last_modified_iso', 'size_bytes'})
    or None if absent. Used by image-list to surface drift between catalog
    and B2 reality."""
```

---

## 6. Builder + upstream tracker

### 6.1 `mthydra.controller.image.builder`

```python
class BuildError(RuntimeError): ...


def build_image(
    *,
    conn: sqlite3.Connection,
    b2_destination,
    upstream_repo: str,
    upstream_release: str,
    asset_filename: str,
    github_api_url: str,
    tmp_dir: Path,
    now: str,
    actor: str = "operator",
    http_client: Callable | None = None,   # injectable for tests
) -> str:
    """Download upstream binary, verify sha256, upload to B2, insert ru_images row.

    Returns the new image_version (hex sha256 of the binary).

    Procedure:
      1. GET <api>/repos/<upstream_repo>/releases/tags/<upstream_release>.
      2. Locate the asset matching asset_filename in the release's `assets` list.
      3. Locate the checksum file (one of: SHA256SUMS, <asset>.sha256, checksums.txt).
      4. Download both into tmp_dir/<random>.
      5. Verify the binary's sha256 against the upstream checksum file.
      6. Recompute sha256 of the downloaded bytes as image_version (defensive).
      7. Build the manifest JSON.
      8. b2_destination.put_image(image_version=..., binary_path=..., manifest=...).
      9. Insert ru_images row with state='candidate', built_at=now.
     10. Emit audit row action='image_built', target=image_version.

    Raises BuildError on: GitHub non-200, missing asset, missing checksum file,
    sha256 mismatch, B2 upload failure. On every error path no DB row is written.
    """
```

The `http_client` parameter is injectable for unit tests (a `MagicMock` with `.get(url) → Response` semantics); production uses `urllib.request` (stdlib, no new dep).

**Ordering invariant: B2 upload happens before DB insert.** A B2 success without DB insert produces an orphaned B2 object (visible via `head_image()` but no catalog row — `image-list` surfaces this as a drift signal). A DB insert without B2 success would produce a phantom catalog row pointing at non-existent storage — much worse. The chosen order means catalog rows always reflect existing B2 objects modulo manual B2 tampering.

### 6.2 `mthydra.controller.image.upstream_tracker`

```python
class UpstreamReleaseTracker:
    """APScheduler-driven weekly poll of GitHub releases. Active-only."""

    def __init__(
        self, *,
        db_path: Path | str,
        upstream_repo: str,
        github_api_url: str,
        poll_interval_seconds: float,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
        http_client: Callable | None = None,
    ) -> None: ...

    def arm(self) -> None: ...
    def disarm(self) -> None: ...

    def run_once(self) -> str | None:
        """Returns the latest upstream release tag, or None if the check failed.

        On success:
          - stamps `t4_upstream_check` proven (next_due = now + 2 * interval)
          - if latest tag is not yet in any ru_images row:
              sets `t4_upstream_release_available::<tag>` anti-obligation
          - emits audit row action='upstream_release_seen', target=<tag>
        On failure: returns None; obligation NOT stamped (never lie about checking);
        logs warning.
        """
```

Follows the spec C / F scheduler pattern exactly: `BackgroundScheduler` + `ThreadPoolExecutor(max_workers=1)`, `mode="offline"` short-circuits `arm()`.

---

## 7. Config

New `[image]` section in `controller.toml`:

```toml
[image]
upstream_repo           = "9seconds/mtg"
upstream_release_asset  = "mtg-linux-amd64"      # asset filename in the GitHub release
upstream_check_interval = "168h"                  # weekly
github_api_url          = "https://api.github.com"
build_tmp_dir           = "/var/lib/mthydra/tmp"
```

Loaded into `ImageConfig` dataclass. The `github_api_url` override exists for tests; production deployments leave the default.

---

## 8. CLI

All commands active-only unless flagged otherwise; all emit audit rows; structured commands accept `--json`.

```
mthydra-controller image-build --release <tag> [--asset <filename>] \
    [--notes <text>] [--db-path ...] [--config ...]
    # Downloads, verifies sha256, uploads to B2, inserts ru_images row as candidate.
    # Exit codes: 0 OK / 2 input err / 3 checksum mismatch / 4 GitHub API err / 5 B2 err.

mthydra-controller image-list [--state candidate|promoted|retired] [--json]
    # Prints rows + per-row `b2_present` flag (head_image check). --json structured.

mthydra-controller image-promote <image_version> --evidence <text-or-path> \
    [--db-path ...]
    # Atomic candidate → promoted; prior promoted (if any) → retired; obligation
    # re-stamping. --evidence captured verbatim in audit_log (placeholder for D2 gate).

mthydra-controller image-retire <image_version> --reason <text> [--db-path ...]
    # state -> 'retired'. Legal from candidate or promoted. Retiring promoted
    # without a candidate to promote next leaves the fleet with no default
    # (CLI warns; operator must act).

mthydra-controller image-current [--json] [--db-path ...]
    # Read-only. Prints the currently-promoted image_version (or 'none').
    # The ONE command callable on standby (diagnostic — what is active serving).

mthydra-controller upstream-check [--db-path ...] [--config ...]
    # Forces an immediate UpstreamReleaseTracker.run_once(). Useful for manual
    # re-checks after a known upstream release.
```

`image-current` is the only command in the spec-D suite that does not require `_require_active_role()` — it is read-only and useful from a standby for diagnostic purposes. All mutating commands enforce the active-only check.

---

## 9. Bootstrap + obligations

### 9.1 New obligations seeded by bootstrap

Added to the `obligation_timer_hours` dict in the `init` subcommand:

```python
"t4_image_promoted":  30 * 24,    # 30 days — heartbeat for image-promote cadence
# `t4_upstream_check` is already seeded by spec A at 168 hours (weekly).
```

Per-tag anti-obligations (`t4_upstream_release_available::<tag>`) are created lazily by the tracker; cleared lazily by `promote`.

### 9.2 §12 obligation summary

| Obligation | Healthy interval | What "proven" means |
|---|---|---|
| `t4_upstream_check` | ≤ `upstream_check_interval × 2` (default 14d) | Tracker successfully polled GitHub at least once recently |
| `t4_upstream_release_available::<tag>` (anti-obligation) | absent | Presence = a newer upstream release exists that we have not built/promoted |
| `t4_image_promoted` | ≤ 30d | Operator has promoted at least one image in the last 30 days (image-currency heartbeat) |

`t4_upstream_check` going stale is the spec's primary silent-failure tripwire: it means the tracker has been failing or the controller has been offline. Going > 14d without an upstream check is exactly the silent-staleness mode T4 exists to prevent.

---

## 10. Cross-spec contracts

- **Spec D2 (canary + validation gate).** D2 adds the `canary` state to `ru_images` (between `candidate` and `promoted`), the canary deployment plumbing (uses spec G/H), the T3 hard-kill profile check (uses spec I), and the atomic profile re-pin. The `--evidence` parameter on `image-promote` becomes optional once D2's gate is wired in (auto-promote on green canary).
- **Spec E (RU node init).** Reads `current_promoted(conn)` at provisioning time to know which binary to fetch on first boot. The B2 binary URL is included in the seed via a short-lived signed URL minted by spec G's artifact generator (NOT by spec D — D never mints credentials).
- **Spec G (RU provisioning artifact generator).** Owns the seed bundle. Calls `current_promoted(conn)`; if it returns None, refuses to generate a seed (cannot provision a box without a promoted image). G mints the signed URL the box uses to fetch the binary; G's job, not D's.
- **Spec H (shard manager).** When H terminates a box, the box's `ru_boxes.image_version` is already recorded. H may later optionally consult `ru_images` to know whether the box was on a retired image — relevant for D2's "soft-burn after rollback" mechanism.
- **Spec I (probe vantages).** Owns T3 hard-kill profile machinery. D2 defines the seam where I's profile is pinned to a candidate image and re-pinned atomically on promote.
- **Existing `ru_boxes.image_version` column (spec A).** Stays free-text TEXT for now. D2 may add an FK `ru_boxes.image_version → ru_images.image_version` once H provisions boxes from catalog rows.

---

## 11. Honest residuals (Spec D)

- **No validation gate.** `image-promote` is operator-attested via `--evidence`, not gated by automated probes. An operator can promote a regressed image and the catalog will dutifully record their evidence string. Placeholder for D2's real gate. T4 §6 (atomic promote + profile re-pin) is therefore not real in D1 — there is no profile to re-pin yet.
- **No canary deployment.** D1 has no mechanism to provision a small subset of boxes from a candidate image. Operators who want canary discipline today must do it manually: build → promote → watch (accepting the fleet-wide blast radius). D2 will add provisioning hooks once specs G/H land.
- **No automatic rollback.** If an in-use promoted image is found regressed in the wild, the operator runs `image-retire` + `image-promote` of the prior good image. There is no "soft-burn all boxes built from `<bad_image>`" mechanism — that needs spec H's box-level termination. T4 §8 rollback in D1 is therefore manual: catalog state updates only; the fleet rolls naturally via spec C's `cover-rotate`.
- **`upstream-check` trust assumption.** The tracker trusts GitHub's API responses verbatim. A compromised GitHub account or DNS hijack of `api.github.com` could feed a forged release tag. Same trust we already have for the source. The operator's `image-build` step verifies the binary against upstream's published checksum file, which is the only crypto check in the pipeline.
- **B2 retention bloat.** Each image binary is stored under Object Lock COMPLIANCE for `object_lock_days`. Fifty never-promoted candidates means fifty binaries × ~10MB held until retention expires. Accepted vs the alternative (governance-mode or no-lock for image objects) which would weaken artifact immutability.
- **`image_version = sha256(binary)` deduplicates concurrent builds.** Two builds of the same release at different times produce the same image_version (correct — cattle model treats binary identity as its hash). The second `INSERT` collides on PRIMARY KEY; CLI surfaces a clean "image already in catalog" diagnostic. Documented; no programmatic guard needed.
- **No hardening verification in D1.** Design §9 step 2 wants "tmpfs/no-swap/volatile-journal/no-coredump hardening … re-verified" on each new image. mtg is a single binary; the hardening lives in the systemd unit / cloud-init that runs it — territory belonging to spec E. D1 just produces the binary and records the catalog entry.
- **Tracker depends on `api.github.com` reachability.** A controller in a network-partitioned state will report `t4_upstream_check` going stale; the obligation system surfaces this correctly. The dependency itself is new vs. specs A–F (which only touched B2). Accepted as the cost of T4.

---

## 12. Test discipline

Coverage target: ≥ 90% line coverage on `mthydra.controller.image.builder`, `mthydra.controller.image.upstream_tracker`, `mthydra.controller.state.ru_images`.

### 12.1 Unit tests

- `state/test_ru_images.py` — repository CRUD + state-machine semantics:
  - `insert_candidate` round-trip via `get_image`
  - `promote` happy path: candidate → promoted, prior promoted → retired, audit row, obligation re-stamping
  - `promote` refuses non-candidate (retired, already-promoted)
  - `retire` from candidate; `retire` from promoted (warn-but-allow at CLI layer; repo just records)
  - `current_promoted` returns singleton or None
  - `list_images` filters by state

- `image/test_builder.py` — happy path + every named failure mode in §6.1, with mocked `http_client` returning fake release JSON + fake binary + fake checksum file. No real HTTP, no real B2 (use a `MagicMock` for `b2_destination`).

- `image/test_upstream_tracker.py` — first-run sets anti-obligation, repeat-with-same-tag is a no-op, new tag emits anti-obligation, GitHub 5xx returns None without stamping, rate-limit (429) graceful skip.

### 12.2 Invariant tests

Extend `tests/unit/controller/state/test_invariants.py` with checks for #24 and #25 (raw-SQL constructed broken states; assert `check_all` raises with the right "check 24" / "check 25" message).

### 12.3 CLI tests

Extend `tests/unit/controller/test_cli.py`:
- `image-build` happy path (mock `build_image`)
- `image-build` refused on standby (`_require_active_role`)
- `image-promote` requires `--evidence` (argparse rejects)
- `image-promote` clears the matching `t4_upstream_release_available::<tag>` obligation
- `image-list --json` schema check
- `image-current` runs on standby read-only without error
- `upstream-check` calls the tracker and prints the latest tag

### 12.4 Integration test

`tests/integration/test_image_lifecycle.py` — uses `moto[s3]` for B2 + a stub HTTP client for GitHub. Full lifecycle: upstream-check (no rows) → image-build → image-list (candidate present) → image-promote → image-current (returns promoted) → image-retire (a different candidate) → image-list (mixed states). Asserts audit-log content and obligation timestamps after each step.

### 12.5 Failure-mode catalogue

See §6 failure-mode catalogue above (the implementation plan will mirror it as a table in the spec doc).

---

## 13. Status

**D1: drafted, awaiting implementation.** Once D1 ships, the next foundation-side spec by build-plan ordering is **E** (RU node init from cloud-init seed), which is the consumer of D1's catalog + binary. **D2** (canary + validation gate) waits on specs I (probe vantages) + G/H (provisioning, shard manager) before it can be brainstormed.
