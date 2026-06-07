# Spec V — RU-egress obfuscation (fingerprint diversity + nfqws desync + self-measurement)

Status: **draft**
Date: 2026-06-06
Predecessors: `B` (signed descriptor — schema bump lives here), `E` (RU/EU data-plane — the Reality client this spec hardens), `G` (provisioning seed — new binary fields), `I` (probe vantage harness — the measurement loop builds on it), `D`/`D2` (image/canary — the desync-strategy rollout gate).
Successors blocked on this: none yet.

This spec adds three independent-but-coordinated obfuscation units to the RU→EU border-crossing hop (the VLESS+Reality flow established in spec E). It does **not** touch the intra-RU user→box hop (mtg FakeTLS).

---

## 1. Purpose & threat framing

The RU→EU hop today (`mthydra.ru_agent.config_gen.render_sing_box_config`) is **VLESS + Reality + `xtls-rprx-vision`**. That is already a generation ahead of the threat described in the source material (Habr 1041486, "Как DPI вычисляет MTProto-прокси", 30 May 2026), which targets *raw FakeTLS MTProxy*:

- **DPI Stage 1 (Mar–Apr 2026):** JA3/JA4 handshake-fingerprint matching. Reality does a *real* TLS 1.3 handshake to a *real* cover domain, so the server side is genuine and active-probing falls through to the real host. The remaining Stage-1 exposure is the **client ClientHello**: sing-box's uTLS fingerprint.
- **DPI Stage 2 (May 2026):** statistical/behavioral analysis (packet-length & timing distributions, TLS-record structure). `xtls-rprx-vision` pads the inner TLS handshake records, defeating the TLS-in-TLS *handshake* tell. The residual is population-scale correlation and steady-state flow statistics.
- **Packet-level desync** (GoodbyeDPI / zapret / byedpi family): fragmentation + fake-TTL/bad-checksum decoys that poison *stateful* DPI flow reassembly. Linux-native engine: **zapret/nfqws** (NFQUEUE).

The three units this spec adds, in priority order:

1. **Unit 1 — fingerprint freshness + diversity.** Move the hardcoded `utls.fingerprint = "chrome"` into the signed descriptor as a weighted list; each box deterministically self-picks. Closes the Stage-1 client-fingerprint gap and the fleet-uniform-ClientHello population tell. *High value, low effort, no new infra.*
2. **Unit 5 — self-measurement.** RU-vantage handshake-health probe + controller-side fingerprint-staleness check. Turns the arms race from blind to instrumented. *Durable value; precondition for safely operating Unit 2.*
3. **Unit 2 — nfqws desync layer.** zapret/nfqws as a fully cattle-integrated, descriptor-tunable child on the RU→EU outbound. *The arms-race extension; value depends on keeping the strategy tuned, and on the canary gate.*

**Honest headline:** Reality+Vision already beats the article's threat. Units 1 and 5 are the high-ROI core; Unit 2 is the extension whose marginal value is real but conditional on maintenance and on never shipping an untested strategy fleet-wide.

Fleet wire-identity de-correlation (per-box cover SNI / non-empty short_id) was considered and **deliberately excluded** from this spec; it can return as a later amendment.

Out of scope: steady-state traffic padding/shaping (sing-box has weak native support; mux+padding introduces new correlation tells — research, not build); any change to the mtg FakeTLS user→box hop.

---

## 2. Locked design decisions

| ID | Decision | Rationale |
|---|---|---|
| V-D1 | **uTLS fingerprint comes from a signed-descriptor weighted list; box self-picks deterministically by `box_id`.** | Per-box diversity defeats population-scale ClientHello correlation; signed list prevents steering to an unrealistic fingerprint; deterministic-by-`box_id` keeps each box's identity **stable across refreshes** (a mutating ClientHello is itself anomalous) while remaining controller-rollable when the list changes. |
| V-D2 | **Fingerprint pick is stable per box, not rotated per tick.** | A box whose JA3 changes every 15 min is a stronger anomaly than a stable-but-diverse fleet. Rotation only happens when the operator edits the descriptor list. |
| V-D3 | **Desync engine is zapret/`nfqws` via NFQUEUE**, not byedpi SOCKS-chain or a hand-rolled handler. | Mature, Linux-native, fits the agent's existing iptables ownership; no extra proxy hop reshaping the outbound path; the arms-race maintenance is upstreamed to zapret. |
| V-D4 | **`nfqws` is fully cattle-integrated:** B2-distributed sha256-verified binary, supervised child, NFQUEUE rule re-verified each refresh tick, **strategy string carried in the signed descriptor**. | Mirrors the mtg binary + descriptor-refresh patterns exactly (E-D2, §4.2/§4.3 of spec E). Fleet-wide strategy retune without re-imaging when ТСПУ changes. |
| V-D5 | **The desync NFQUEUE rule targets only outbound TCP to EU-exit IPs:443 (the Reality flow).** | The local mtg→sing-box redirect must not be desynced. Exit IPs are read from `descriptor.eu_exit_set`, so the rule set regenerates atomically alongside `sing-box.json` on every refresh. |
| V-D6 | **A `desync_strategy` change MUST be canaried on a shard subset before fleet rollout (gate reuses spec D2/H canary machinery).** | A single signed strategy string reaches every box at once; a bad one is a fleet-wide outage. This is a hard requirement, not advisory. |
| V-D7 | **Self-measurement is two checks: a RU-vantage handshake-health probe (spec I) and a controller-side fingerprint-staleness evaluation that consumes the probe's captured JA3.** | They catch different failure modes (active blocking vs. fingerprint drift). Folding staleness onto the *captured* JA3 avoids abstractly recomputing what sing-box-uTLS emits. |
| V-D8 | **One descriptor schema bump `v2 → v3`** carries both `tls_fingerprints` and `desync_strategy`; RU verifier accepts v2 and v3 during transition. | Single coordinated amendment; matches spec B's v1→v2 transition discipline. |
| V-D9 | **No new RU operator surface.** All three units are controller/descriptor-driven. | Consistent with E-D8 (no phone-home) and the cattle model. |

---

## 3. Unit 1 — uTLS fingerprint freshness + diversity

### 3.1 Descriptor amendment (spec B → v3)

New top-level field in the signed descriptor payload:

```json
"tls_fingerprints": [
  {"fp": "chrome",  "weight": 60},
  {"fp": "firefox", "weight": 15},
  {"fp": "safari",  "weight": 10},
  {"fp": "ios",     "weight": 10},
  {"fp": "edge",    "weight": 5}
]
```

- `fp` MUST be a member of the sing-box-known uTLS allowlist (§3.3).
- `weight` is a positive integer; the list MUST be non-empty when present.
- Field is OPTIONAL on the wire: a v2 descriptor (no field) is still accepted, and the RU box falls back to `"chrome"` (current behaviour) — see §3.4.

### 3.2 Controller side

`controller.toml` gains:

```toml
[descriptor.tls_fingerprints]
# Weighted uTLS fingerprint pool. Operator updates weights/set as the real
# browser population moves; the signer re-emits on the next descriptor build.
chrome  = 60
firefox = 15
safari  = 10
ios     = 10
edge    = 5
```

The descriptor signer (`mthydra.descriptor.payload` / `mthydra.controller.state.descriptor`) reads this map, normalises it into the `tls_fingerprints` array (sorted by `fp` for canonical-bytes stability), and includes it in the v3 payload. Empty/missing config section → omit the field (emit v2-compatible payload).

### 3.3 RU-side selection (`ru_agent/config_gen.py`)

Replace the hardcoded fingerprint at `config_gen.py:62`:

```python
# was: "utls": {"enabled": True, "fingerprint": "chrome"},
fp = _pick_fingerprint(seed.box_id, descriptor_payload.get("tls_fingerprints"))
...
"utls": {"enabled": True, "fingerprint": fp},
```

`_pick_fingerprint(box_id, weighted_list)`:
1. If `weighted_list` is falsy → return `"chrome"` (v2 fallback).
2. Validate every `fp` against `KNOWN_UTLS_FINGERPRINTS` (module constant). Unknown name → `ConfigError` (an unknown fingerprint crashes sing-box at startup; fail fast at render time instead).
3. Deterministic weighted pick: `idx = int.from_bytes(sha256(box_id.encode()).digest()[:8], "big") % total_weight`, then walk the weight-sorted list to map `idx` → `fp`.

`KNOWN_UTLS_FINGERPRINTS` is the set sing-box's `utls` accepts (e.g. `chrome`, `firefox`, `safari`, `ios`, `android`, `edge`, `360`, `qq`, `random`, `randomized`). Pin to the set valid for the sing-box version the image ships; `random`/`randomized` are permitted in the constant but the operator is discouraged from weighting them (they can emit implausible ClientHellos).

The same fingerprint string is what the RU-vantage probe (Unit 5) must reproduce — see §5.2.

### 3.4 Compatibility & invariants

- **#33** — every `fp` in a published v3 descriptor's `tls_fingerprints` MUST be in `KNOWN_UTLS_FINGERPRINTS`. Descriptor-signer check (controller refuses to sign otherwise).
- RU verifier (`mthydra.descriptor.verify`) accepts both `mthydra.descriptor.v2` and `mthydra.descriptor.v3`. Signer emits v3 once this spec ships.
- `short_id` stays `""` (fleet de-correlation excluded — §1).

---

## 4. Unit 2 — nfqws desync layer

### 4.1 Binary distribution (spec G / D-adjacent)

`nfqws` is built from a pinned [zapret](https://github.com/bol-van/zapret) source revision and pushed to B2 as its own artifact (the agent tarball mechanism of spec E §9, *not* through spec D's mtg image lifecycle — nfqws is a tool, not the tracked upstream binary).

Seed (`mthydra.ru_seed` → **v3**) gains:
- `nfqws_url` — anonymous-read B2 URL for the nfqws binary
- `nfqws_sha256` — sha256 hex trust anchor

`ru_agent/binary.py` generalises `fetch_and_verify` to handle a list of `(url, sha256, dest, mode)` binaries (mtg + nfqws). Startup sequence (spec E §4.2) gains a step 4b: fetch+verify nfqws into `/run/mthydra/nfqws`, `chmod +x`.

### 4.2 Strategy via descriptor (spec B → v3)

New top-level descriptor field:

```json
"desync_strategy": "--dpi-desync=fake,split2 --dpi-desync-ttl=4 --dpi-desync-fooling=badsum"
```

- Free-form nfqws argument string (minus `--qnum`, which the agent owns).
- OPTIONAL: absent → Unit 2 is **disabled** (no nfqws process, no NFQUEUE rule). This is the v2-descriptor / opt-out state, so the feature ships dark and is lit per-fleet by adding the field.

`controller.toml`:
```toml
[ru_egress.desync]
# nfqws argument string (without --qnum). Empty/absent => desync disabled fleet-wide.
strategy = "--dpi-desync=fake,split2 --dpi-desync-ttl=4 --dpi-desync-fooling=badsum"
```

### 4.3 Wiring (`ru_agent/desync.py`, new module)

```
desync.py
  build_nfqueue_rules(exit_ips, qnum)   # mangle/OUTPUT TCP -d <ip> --dport 443 -j NFQUEUE --queue-num <qnum>
  install(exit_ips, qnum)               # atomic apply (mirrors iptables.py discipline)
  verify_still_installed(exit_ips, qnum)
  nfqws_argv(strategy, qnum)            # ['/run/mthydra/nfqws', '--qnum=<qnum>', *shlex.split(strategy)]
  clear(qnum)
```

- **Target set:** only outbound TCP to the EU-exit IPs on port 443. Exit IPs are parsed from `descriptor_payload["eu_exit_set"]` (the same source `config_gen` uses). The local mtg→sing-box `redirect` inbound (127.0.0.1) is never matched (V-D5).
- **Regeneration:** on every descriptor refresh tick that changes the exit set, the NFQUEUE rule set is rebuilt atomically alongside `sing-box.json` (the refresh loop already SIGHUPs sing-box on change — extend it to re-apply desync rules in the same critical section).
- **AST invariant:** `mthydra.ru_agent.desync` MUST NOT import `mthydra.controller.*` (extend `test_ast_no_controller_imports.py`).

### 4.4 Supervision & self-termination (spec E §4.4/§4.5)

- `nfqws` is exec'd as a supervised child alongside mtg and sing-box (`supervisor.py`). Same rolling-5-min crash-loop window: ≥4 restarts in 5 min → `shutdown -h now` with audit line.
- NFQUEUE rule re-verified each refresh tick (`desync.verify_still_installed`): first miss → re-install once; second consecutive miss → `shutdown -h now`. Mirrors the iptables path (spec E §4.3).
- `nfqws` needs `CAP_NET_ADMIN`; the agent already runs as root (accepted residual in spec E §10). No persistence — binary lives in tmpfs `/run/mthydra`.
- If `desync_strategy` is absent in the descriptor: no child, no rules; a refresh that *removes* the field tears down the rule + stops the child cleanly (does not self-terminate the box).

### 4.5 Canary gate (V-D6, hard requirement)

A change to `desync_strategy` MUST land on a **shard subset first** (spec H shards / spec D2 canary pattern):
1. Operator stages the new strategy scoped to a canary shard's descriptor.
2. The Unit-5 RU-vantage probe (§5) must show handshake-health on canary boxes holding steady for a defined soak window.
3. Only then is the strategy promoted to the fleet descriptor.

Implementation hook: the descriptor signer refuses to apply a *fleet-wide* `desync_strategy` change unless a matching canary-proven marker exists (a new obligation, §7). This makes V-D6 controller-enforced, not discipline-only — consistent with how spec H turned T6 into an enforced control.

### 4.6 Honest residuals

- **A bad signed strategy is a fleet-wide outage.** The canary gate (§4.5) is the mitigation; the 2h "all exits unreachable" self-terminate (spec E §4.5) is the backstop, but it would still flap a canary, not the fleet, if the gate holds.
- **nfqws on a hardened cattle box** adds one more root-capable process and an NFQUEUE rule to the verify-each-tick set. Accepted, scoped under spec E's existing "agent runs as root" residual.
- **zapret is an arms-race tool.** Strategy efficacy decays as ТСПУ adapts; Unit 5 is what tells you when.

---

## 5. Unit 5 — self-measurement

### 5.1 RU-vantage handshake-health probe (builds on spec I)

A RU-resident prober (provisioned as a probe vantage per spec I; Russian/CIS hoster per the RU-providers constraint — it is a probe, not a serving box) runs a new **reality-handshake** prober (`mthydra.controller.probe_runner.probers`):

For each EU exit in the current exit set, dial with box-equivalent Reality client parameters and record:
- TCP connect success / latency
- TLS (Reality) handshake success / failure / timeout
- observed injected RST or premature reset
- time-to-first-byte
- **the JA3/JA4 of the ClientHello the prober emits** (captured locally; this is the value Unit 3a consumes)

Results flow through the existing pipeline: `probe_results` → `observability/snapshot` → `observability/remediation`, identical in shape to the K3 `box_eu_tunnel_unseen` signal. A rising RST/handshake-failure rate clustered by fingerprint or cover SNI is the actionable signal (→ roll `tls_fingerprints` or retune `desync_strategy`).

New/changed:
- `probe_runner/probers.py` — add `RealityHandshakeProber`.
- `probe_vantages` — a RU vantage class (the prober must originate inside RU to observe ТСПУ behaviour).
- `observability/snapshot.py` + `severity.py` + `remediation.py` — new signal(s): `eu_exit_handshake_degraded`.

### 5.2 Controller-side fingerprint-staleness (Unit 3a)

`mthydra/controller/observability/fingerprint_staleness.py` (new):
- Maintains a **current-browser JA3/JA4 reference set** — operator-maintained ops data, same maintenance class as `[data_exit.telegram_dcs]` (manual, rarely; documented in the runbook).
- Consumes the JA3 the §5.1 probe captured for each deployed fingerprint.
- A deployed fingerprint whose captured JA3 no longer matches any entry in the reference set raises a snapshot signal `tls_fingerprint_stale` + remediation hint ("roll `tls_fingerprints`: <fp> JA3 no longer matches a current popular browser").

Folding the comparison onto the **probe-captured** JA3 (rather than abstractly computing what sing-box-uTLS emits) keeps the check robust to sing-box version changes.

### 5.3 Residuals

- The RU vantage is a RU footprint (mitigated: probe, not serving box; lower burn value).
- The JA3 reference set is manual ops data; staleness of the *reference itself* is an operator responsibility (runbook entry).
- Probe cadence vs. ТСПУ adaptation speed is a tuning parameter; start conservative (e.g. hourly) and tighten if drift is observed.

---

## 6. Cross-spec amendments

### 6.1 Spec B — descriptor (`v2 → v3`)
New OPTIONAL top-level fields: `tls_fingerprints` (§3.1), `desync_strategy` (§4.2). Canonical-bytes ordering: `tls_fingerprints` sorted by `fp`. RU verifier accepts v2 and v3; signer emits v3.

### 6.2 Spec G — seed (`mthydra.ru_seed v2 → v3`)
New fields: `nfqws_url`, `nfqws_sha256` (§4.1). Provisioning writes them from controller config; absent → nfqws fetch skipped and Unit 2 inert (belt-and-suspenders with the descriptor opt-out).

### 6.3 Spec A — controller.toml
New sections: `[descriptor.tls_fingerprints]` (§3.2), `[ru_egress.desync]` (§4.2).

### 6.4 Spec I — probe harness
New `RealityHandshakeProber` + RU vantage class (§5.1).

### 6.5 Spec D2 / H — canary
`desync_strategy` fleet promotion gated on canary-proven marker (§4.5, §7).

---

## 7. Invariants & obligations

New invariants:
- **#33** — every `fp` in a published v3 descriptor's `tls_fingerprints` ∈ `KNOWN_UTLS_FINGERPRINTS`. (§3.4)
- **#34** — a RU agent with a non-empty `desync_strategy` in its current descriptor MUST have the nfqws child running AND the NFQUEUE rule installed; verified each refresh tick (failure path → §4.4).
- **#35** — the NFQUEUE desync rule set MUST match exactly the current `eu_exit_set` endpoint IPs on port 443 — no broader, no narrower. (§4.3, V-D5)
- **#36** — a fleet-wide (non-canary) `desync_strategy` change MUST NOT be signed without a matching canary-proven marker. (§4.5)

New bootstrap obligation (controller `init`, active mode):
- `v_desync_strategy_canary_proven` — reset by the canary soak (§4.5) passing for a `desync_strategy` candidate; consumed by the signer's #36 check. Re-armed on each new candidate strategy.

---

## 8. CLI

New `mthydra-controller` subcommands:
- `tls-fingerprints-show` — print the current weighted pool + the v3 descriptor field that will be signed.
- `desync-strategy-show` — print the current `desync_strategy` and whether it is canary-proven.
- `desync-strategy-stage <shard>` — stage a candidate strategy scoped to a canary shard.
- `desync-strategy-promote` — promote the staged strategy fleet-wide; refuses without the `v_desync_strategy_canary_proven` marker (#36).
- `fingerprint-staleness-show` — print per-fingerprint JA3 match status against the reference set.

No new RU-side CLI (V-D9).

---

## 9. Build order

1. **Unit 1 (fingerprint).** Descriptor v3 field + signer + `config_gen` pick + controller.toml + verifier accept-both + invariant #33. No new infra; immediate value.
2. **Unit 5 (measurement).** RealityHandshakeProber + RU vantage + snapshot/remediation signals + fingerprint-staleness module + reference-set runbook entry. Gives instrumentation *before* desync tuning.
3. **Unit 2 (nfqws desync).** Binary distribution + seed v3 + `desync.py` + supervisor integration + tick-verify + descriptor `desync_strategy` + canary gate (#36 / obligation) + invariants #34/#35.

Rationale: each step is independently shippable, and step 2 is a precondition for operating step 3 without flying blind.

---

## 10. Test discipline

### 10.1 Unit tests
- `tests/unit/ru_agent/test_config_gen.py` — extend: deterministic `_pick_fingerprint` (stable per box_id, weighted distribution, v2 fallback, unknown-fp `ConfigError`), golden v3 render.
- `tests/unit/ru_agent/test_desync.py` — rule construction targets only exit IPs:443; verify-still-installed; argv build; absent-strategy = inert.
- `tests/unit/ru_agent/test_binary.py` — extend: multi-binary fetch+verify (mtg + nfqws).
- `tests/unit/ru_agent/test_ast_no_controller_imports.py` — extend to cover `desync.py`.
- `tests/unit/ru_agent/test_supervisor.py` — extend: nfqws as third supervised child, crash-loop → shutdown.
- `tests/unit/descriptor/test_verify.py` — accepts v2 and v3.
- `tests/unit/controller/test_descriptor_signer.py` — emits v3; #33 refusal on unknown fp; #36 refusal without canary marker.
- `tests/unit/controller/observability/test_fingerprint_staleness.py` — match / stale / missing-reference paths.
- `tests/unit/controller/probe_runner/test_reality_handshake_prober.py` — success / RST / timeout / JA3 capture (mocked socket).

### 10.2 Integration tests
- `tests/integration/test_ru_agent_offline.py` — extend: agent with v3 descriptor (fingerprint pick + nfqws child + NFQUEUE rule), all subprocess/iptables mocked.
- `tests/integration/test_desync_strategy_canary_gate.py` — stage → soak-proven → promote; promote refused without marker.

### 10.3 Coverage
- New `mthydra.ru_agent.desync` ≥ 90%.
- New `mthydra.controller.observability.fingerprint_staleness` ≥ 90%.

### 10.4 Honest residuals (live-only, not pytest)
- Real ТСПУ behaviour vs. a given `desync_strategy` is only observable on real RU VMs — owned by the canary soak (§4.5), not unit tests.
- The JA3 reference set's accuracy is an ops responsibility (runbook).
- nfqws + NFQUEUE interaction with the specific cloud image's netfilter stack is verified during the spec-E provision-replace drill, extended to assert the desync rule + child come up.

---

## 11. Status

Spec drafted 2026-06-06. Implemented 2026-06-06/07 (all three units, build order 1→5→2). High-ROI core is Units 1+5; Unit 2 is the conditional arms-race extension.

---

## 12. As-built deviations (reconciled 2026-06-07)

The implementation matches this spec except for the canary-gate mechanism (Unit 2, §4.5/§7/§8), which was simplified because **no shard-scoped descriptor infrastructure exists** in the codebase (descriptors are signed fleet-wide, not per-shard). As built:

- **Canary proof is manual operator attestation, not auto-derived.** `mark-canary-proven <strategy>` records `sha256(strategy)` in the `desync_strategy` table; `promote` enforces invariant **#36** by refusing unless the staged strategy's hash matches that marker (`src/mthydra/controller/state/desync_strategy.py`). The operator stages the strategy, watches the Unit-5 `eu_exit_handshake_degraded` signal on a canary cohort for the soak window, then attests. The hard gate (#36) is fully enforced; the *derivation* of proof is operator-driven rather than computed from V5 probe rows. Documented in `doc/runbook.md §V.2`.
- **`v_desync_strategy_canary_proven` is a bespoke table column, not a bootstrap obligation.** It does not appear in `obs-status`; visibility is via `desync-strategy-show`.
- **`desync-strategy-stage` has no `<shard>` argument** (§8) — it writes a single global staged slot, consistent with the fleet-wide descriptor model.

Follow-up (non-blocking): if per-shard descriptors are ever introduced, automate canary-proof derivation from V5 signal data and migrate the marker into the obligations framework.
