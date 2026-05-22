# Spec E — RU/EU data-plane (RU agent + EU data-exit)

Status: **draft**
Date: 2026-05-23
Successors blocked on this: `H` (shard manager — consumes per-box exit assignments), `I` (probe vantage harness — exercises the RU box from outside), `J` (observability — alerts on data-exit liveness).
Predecessors: `A` (controller state), `B` (signed descriptor), `C` (cover-domain pool), `D` (mtg image), `F` (EU node setup), `G` (provisioning artifact).

This spec formally subsumes the deferred `F2` (data-exit / Reality-tunnel terminator). Build-plan §2 entries for `E` and `F2` collapse into this single document; downstream references should point here.

---

## 1. Purpose

Make a provisioned RU box (spec G produces the seed; spec D produces the mtg binary) actually carry Telegram client traffic to a cooperating EU exit, and make the EU exit terminate that traffic and forward it to Telegram. This is the spec that turns the cattle into traffic.

The design pins:
- The RU-side **long-lived agent** that supervises mtg + the Reality client, refreshes descriptors, and self-terminates on adverse state.
- The EU-side **data-exit wheel** that maintains the sing-box server config from controller state and supervises the sing-box process.
- The **wire-level identity model**: how the spec-G onward credential maps to a Reality UUID the sing-box server actually enforces.
- The **traffic plumbing**: how mtg's hardcoded-Telegram-upstream gets bent into the local Reality client via iptables.
- The **hardening contract**: tmpfs-only, no swap, volatile journald, no core dumps — installed by cloud-init `bootcmd:` and re-verified by the agent.
- The **self-termination semantics** that operationalise "reboot = death" from design.md §3.

Out of scope: live network validation against real Telegram MTProto DCs (deployment runbook, not pytest); shard-manager-driven per-box exit assignment (spec H); probe-vantage observation (spec I).

---

## 2. Locked design decisions

| ID | Decision | Rationale |
|---|---|---|
| E-D1 | **Long-lived RU agent**, not a boot-only init script. | Make-before-break rotation, descriptor refresh, and self-termination on persistent failure all require runtime awareness. A boot-only script would either push these concerns into mtg (vendor coupling) or skip them. |
| E-D2 | **Descriptor refresh via anonymous B2 pull**, on a jittered 15±5 min timer. | Independent of F2's Reality tunnel; pure-crypto trust via Ed25519 signature against in-seed trust anchors; matches the B2 fetch pattern the box already does at boot for the mtg binary. |
| E-D3 | **RU box runs stock Debian/Ubuntu cloud image**; cloud-init installs Python + cryptography and downloads the agent source tarball from B2. | No custom OS-image pipeline; reuses existing B2 distribution; minimal at-boot surface. |
| E-D4 | **`sing-box` is the Reality implementation** on both sides; locked in here (closing F-D1's deferred tunnel-software choice). | Native outbound-selector pattern is exactly the make-before-break primitive; JSON config our generator can render deterministically; design.md names it three times. |
| E-D5 | **Onward credential maps to a per-box Reality UUID** via a new `ru_boxes.reality_uuid` column. The credential remains the high-level signed artifact; the UUID is its wire-level derivative. EU sing-box enforces a UUID allowlist. | Reality's native auth model is UUID-based; no per-message custom validator on EU side. Revocation = drop UUID from allowlist + SIGHUP sing-box. |
| E-D6 | **Static EU cover SNI per exit**, configured in controller.toml and stored on `eu_nodes.cover_sni`. Propagated to RU boxes via a new per-exit `cover_sni` field in the signed descriptor (small spec-B amendment). | EU exits are few (2-5), handpicked; rotation = redeploy. No attestation-machinery overhead. Signed-descriptor delivery means the RU box can't be tricked into presenting a wrong SNI. |
| E-D7 | **iptables REDIRECT/TPROXY + sing-box transparent inbound** for the mtg→Reality wiring. mtg runs unmodified. | Avoids forking mtg; standard sing-box-as-relay pattern. Cost: agent owns iptables-rule installation and re-verification, and a hardcoded Telegram DC subnet list in controller.toml. |
| E-D8 | **No phone-home; aggressive self-termination on stale state.** External liveness is purely the probe vantage harness (spec I). | Matches cattle/no-state literally; phone-home would create an identifiable RU→controller signal whose absence is the explicit design choice. |
| E-D9 | **Random per-connection spread + sing-box selector natural drain on rotation.** No time-based active-set narrowing. | Jitter comes for free from connection arrival timing; selector drain is idiomatic sing-box; periodic narrowing would create burstiness the probe vantage may flag as anomalous. |
| E-D10 | **EU exit scope**: sing-box config generation from SQLite + supervision + `eu_exit_set` writes + Telegram-DC-list-from-controller.toml. UUID allowlist propagation via on-disk config + SIGHUP. | Same operational shape as spec A's backup wheel; no SQLite-reading adapter inside sing-box. |
| E-D11 | **Hardening via cloud-init `bootcmd:`** entries (added to the spec-G seed wrapper), re-verified by the agent at startup and every refresh tick. | `bootcmd:` runs before networking and most services; verification is defence in depth; consistent with "the agent refuses to start on any inconsistency." |

---

## 3. Identity model

### 3.1 Reality UUID per box

Schema change on `ru_boxes` (v5 → v6, migration in spec E):
```sql
ALTER TABLE ru_boxes ADD COLUMN reality_uuid TEXT;
CREATE UNIQUE INDEX idx_ru_boxes_reality_uuid ON ru_boxes(reality_uuid) WHERE reality_uuid IS NOT NULL;
```

The uniqueness index is partial (excludes NULL) so existing pre-E rows (without UUIDs) don't violate it.

### 3.2 Provisioning amendment to spec G

`mthydra.controller.provisioning.seed.provision_box()` gains an additional atomic write inside its BEGIN/COMMIT block:
```python
reality_uuid = str(uuid.uuid4())
conn.execute(
    "UPDATE ru_boxes SET reality_uuid=? WHERE box_id=?",
    (reality_uuid, box_id),
)
```

The `SeedBundle` dataclass gains a `reality_uuid: str` field, present in both `to_json()` and `to_cloud_init()` outputs. Spec G's `mthydra.ru_seed.v1` schema bumps to `mthydra.ru_seed.v2` to reflect the additional field (the v1 verifier is informed by the explicit version string; this is the seed schema, not the credential schema).

### 3.3 EU-side enforcement

The EU sing-box server config has a per-inbound `users:` array with one entry per active credential × live box:
```json
{
  "users": [
    {"name": "01HXAA-...", "uuid": "9a8b...-..."},
    {"name": "01HXBB-...", "uuid": "7c4d...-..."}
  ]
}
```

The data-exit wheel regenerates this list each tick. Revocation is atomic from sing-box's perspective: a new config without the UUID is written; SIGHUP causes sing-box to drop the user's future handshakes. (Existing established TLS connections stay up until close — this is acceptable per design's "kill latency is in minutes" framing; if surgical termination is needed, the operator can restart sing-box.)

---

## 4. RU agent

### 4.1 Package layout

`src/mthydra/ru_agent/` — new package, shipped as a tarball to B2 alongside the mtg binary.

```
ru_agent/
  __init__.py
  __main__.py             # entry point: python -m mthydra.ru_agent
  seed.py                 # parse + verify /run/mthydra/seed.json
  hardening.py            # swap/journald/coredump/tmpfs sanity checks
  binary.py               # download + sha256-verify mtg
  config_gen.py           # render mtg.toml + sing-box.json
  iptables.py             # tproxy rule install + verify-still-installed
  descriptor_refresh.py   # B2 poll loop, jittered timer
  supervisor.py           # mtg + sing-box child management
  shutdown.py             # orderly `shutdown -h now`
  telegram_dcs.py         # hardcoded subnet list (from seed; seed inherits from controller.toml)
```

**AST-walk invariant**: no module in `mthydra.ru_agent.*` may import from `mthydra.controller.*`. The agent must run on an RU box that has no controller code present. This mirrors the spec-G discipline for `mthydra.descriptor.authority` and `mthydra.descriptor.keys`. The agent **may** import from `mthydra.descriptor.*` (those modules are already RU-embeddable).

### 4.2 Startup sequence (idempotent; refuses on any failure)

```
1. seed.load("/run/mthydra/seed.json")
2. hardening.verify_all()
       — swapoff active, journald Storage=volatile, kernel.core_pattern=|/bin/false,
         /var/log on tmpfs, /run/mthydra on tmpfs
3. seed.verify_credential(seed)
       — uses mthydra.descriptor.authority.verify_onward_credential
4. binary.fetch_and_verify(seed.image)
       — curl seed.image.url, sha256 check, chmod +x in /run/mthydra/mtg
5. config_gen.render_mtg(seed) → /run/mthydra/mtg.toml
6. config_gen.render_sing_box(seed, descriptor=seed.initial_descriptor)
       → /run/mthydra/sing-box.json
7. iptables.install(seed.telegram_dcs)
       — REDIRECT outbound TCP to listed DC subnets → 127.0.0.1:<sing-box-tproxy-port>
8. supervisor.launch_children()
       — exec mtg, sing-box; tail stderr to journald
9. descriptor_refresh.start_loop()
       — background thread, jittered 15±5 min cadence
10. shutdown.install_signal_handlers()
        — SIGTERM / SIGINT triggers a graceful child-stop + audit line
```

### 4.3 Descriptor refresh loop

Each tick (cadence: 15 min ± 5 min jitter):
1. HTTP GET `seed.descriptor_refresh_url` with `If-Modified-Since: <last_descriptor_issued_at>`
2. On 304: noop; reset failure counter; log "descriptor.unchanged"
3. On 200: verify signature against `seed.descriptor_trust_anchors`
4. On signature invalid: drop response; increment failure counter; log "descriptor.bad_signature"
5. On hash unchanged from current: noop; reset failure counter
6. On hash changed:
   - render new sing-box.json (atomic tempfile + rename)
   - SIGHUP sing-box
   - update internal "current descriptor" hash + issued_at
   - reset failure counter
   - log "descriptor.refreshed" with new generation
7. On HTTP error: log "descriptor.refresh_failed"; increment failure counter
8. If failure counter × tick-interval > 6h: `shutdown -h now`

Additional per-tick verifications:
- Re-run `hardening.verify_all()`; on failure → `shutdown -h now`
- Re-run `iptables.verify_still_installed()`; on first failure re-install once, on second failure → `shutdown -h now`

### 4.4 Supervisor

mtg and sing-box are exec'd as direct children of the agent process. The supervisor watches both stderr streams (forwarded to journald) and SIGCHLD. On exit:
- Track restarts in a rolling 5-minute window.
- < 4 restarts in 5min → systemd-style backoff (1s, 2s, 4s, 8s) and re-exec.
- ≥ 4 restarts in 5min → `shutdown -h now` with audit line.

### 4.5 Self-termination paths

| Trigger | Detection | Latency budget |
|---|---|---|
| Descriptor refresh fails persistently | refresh-loop counter | 6h |
| All EU exits unreachable | sing-box log scrape + agent probe per refresh tick | 2h |
| mtg or sing-box crash-loop | supervisor | <5min |
| Hardening sanity-check regresses | refresh-tick verify | 15min |
| iptables rules disappear | refresh-tick verify | 15min |
| Onward credential signature fails on startup | startup | immediate |

All self-termination paths converge on `shutdown -h now` after a final audit line. The box is cattle; the controller learns of the death through (a) the probe vantage (spec I) seeing no response, then (b) the operator running `ru-box-terminate` (spec G CLI), which revokes the credential and burns the SNI.

---

## 5. EU data-exit wheel

### 5.1 Module layout

`src/mthydra/controller/data_exit/` — new package in the controller.

```
data_exit/
  __init__.py
  config_writer.py        # render /etc/mthydra/sing-box.json from SQLite
  wheel.py                # APScheduler BackgroundScheduler ticker
  exit_set.py             # write/clear eu_exit_set on sing-box state transitions
  telegram_dcs.py         # parse controller.toml telegram_dcs section
  signals.py              # SIGHUP-or-restart sing-box wrapper
```

### 5.2 Wheel tick (every 60s)

```
1. read mtimes of: ru_boxes, onward_credentials, eu_nodes, descriptor_signing_keys
2. if any newer than last_tick OR no previous run:
     a. SELECT box_id, reality_uuid
          FROM ru_boxes
          JOIN onward_credentials USING (box_id)
          WHERE onward_credentials.revoked_at IS NULL
            AND ru_boxes.state = 'live'
            AND ru_boxes.reality_uuid IS NOT NULL
     b. SELECT cover_sni FROM eu_nodes WHERE host_id = this_node
     c. read telegram_dcs from controller.toml
     d. render sing-box.json into a tmpfile in the same dir; atomic rename
     e. if hash changed:
          - SIGHUP sing-box (or systemd reload)
          - audit-log "data_exit.config_rewritten"
3. update last_tick
```

### 5.3 sing-box server config shape (rendered)

```json
{
  "log": {"level": "warn", "timestamp": true},
  "inbounds": [
    {
      "type": "vless",
      "listen": "0.0.0.0",
      "listen_port": 443,
      "users": [
        {"name": "<box_id>", "uuid": "<reality_uuid>", "flow": "xtls-rprx-vision"}
      ],
      "tls": {
        "enabled": true,
        "server_name": "<cover_sni>",
        "reality": {
          "enabled": true,
          "handshake": {"server": "<cover_sni>", "server_port": 443},
          "private_key": "<sing-box-generated; stored in /etc/mthydra/reality.key>",
          "short_id": [""],
          "max_time_difference": "1m"
        }
      }
    }
  ],
  "outbounds": [
    {"type": "direct", "tag": "telegram-direct"}
  ],
  "route": {
    "rules": [
      {"ip_cidr": ["<telegram_dc_subnets>"], "outbound": "telegram-direct"}
    ],
    "final": "telegram-direct"
  }
}
```

The Reality `private_key` is generated once at first start (`sing-box generate reality-keypair`), stored in `/etc/mthydra/reality.key` (chmod 0600), and the corresponding public key is written to `eu_nodes.reality_pubkey` (new column). The descriptor's per-exit entry includes the `reality_pubkey` so the RU client can verify.

### 5.4 Schema changes for EU side

```sql
ALTER TABLE eu_nodes ADD COLUMN cover_sni TEXT;
ALTER TABLE eu_nodes ADD COLUMN reality_pubkey TEXT;
ALTER TABLE eu_nodes ADD COLUMN data_exit_started_at TEXT;

-- Optional value 'degraded' for state column (spec F currently allows 'active' | 'standby' | 'decommissioned')
-- We extend with 'degraded' to capture sing-box crash-loop state.
```

### 5.5 `eu_exit_set` writes

When sing-box reports healthy startup (first successful Reality handshake observed in its log, or `sing-box version` check), the wheel inserts a row into `eu_exit_set` (spec B's table) with:
- `endpoint_address` = `eu_nodes.public_ip`
- `endpoint_port` = 443 (configurable via controller.toml)
- `cover_sni` = `eu_nodes.cover_sni`
- `reality_pubkey` = `eu_nodes.reality_pubkey`
- `since` = `now()`

On wheel shutdown (or sing-box persistent failure / `degraded` state), the row is removed. The descriptor signer (spec B) reads `eu_exit_set` to build the next descriptor.

---

## 6. Cross-spec amendments

### 6.1 Spec G — seed bundle

`mthydra.ru_seed` schema bumps **v1 → v2**:

New top-level fields:
- `reality_uuid` — UUID4 string assigned to this box (§3.2)
- `descriptor_refresh_url` — **anonymous-read** B2 URL pointing to the current signed descriptor object. Same URL for every box (one descriptor object per fleet). Trust is purely cryptographic: the URL has no auth, and the response is verified against `descriptor_trust_anchors`. The controller updates the object on each descriptor rotation.
- `agent_source_url` — **anonymous-read** B2 URL for the agent tarball; same URL for every box of the same agent release. sha256 in `agent_source_sha256` is the trust anchor.
- `agent_source_sha256` — sha256 hex of the agent tarball
- `telegram_dcs` — array of CIDR strings (mirrors controller.toml `[data_exit.telegram_dcs]`)

Cloud-init wrapper gains a `bootcmd:` section:
```yaml
bootcmd:
  - swapoff -a
  - sysctl -w kernel.core_pattern='|/bin/false'
  - mkdir -p /var/log /run/mthydra && mount -t tmpfs tmpfs /var/log
  - mkdir -p /etc/systemd/journald.conf.d
  - printf '[Journal]\nStorage=volatile\n' > /etc/systemd/journald.conf.d/99-mthydra.conf
  - systemctl restart systemd-journald
```

`runcmd:` is extended with apt-install, agent-tarball-download, and a systemd-run for the agent.

### 6.2 Spec B — descriptor

Per-exit entry in the signed descriptor gains:
- `cover_sni: string` — the EU exit's static cover SNI
- `reality_pubkey: string` — base64 of the sing-box-generated Reality public key

Schema label bumps to `mthydra.descriptor.v2`. RU-side verifier accepts both v1 and v2 during transition; descriptor signer always emits v2 once spec E ships.

### 6.3 Spec A — controller.toml

New section:
```toml
[data_exit]
listen_port = 443
sing_box_socket = "/run/mthydra/sing-box.sock"  # for SIGHUP / control
config_path = "/etc/mthydra/sing-box.json"
reality_key_path = "/etc/mthydra/reality.key"

[data_exit.telegram_dcs]
# Hardcoded list of Telegram MTProto DC subnets. Update on the rare occasions
# Telegram changes its IP plan.
v4 = ["149.154.160.0/20", "91.108.4.0/22", "91.108.8.0/22", "91.108.16.0/22",
      "91.108.56.0/22", "95.161.64.0/20"]
v6 = ["2001:b28:f23d::/48", "2001:b28:f23f::/48", "2001:67c:4e8::/48"]

[data_exit.cover_sni]
# Per-host static cover SNI; written to eu_nodes.cover_sni at first start.
# `default` applies if no host_id key matches; per-host keys override.
# The domain must be a real, reachable HTTPS host — Reality dials it on
# probe fall-through. Operator picks domains that look credible as
# Western-Internet traffic.
default = "www.example-cover-domain.invalid"  # placeholder; pick a real one
# alpine = "host.specific.cover.example"
```

### 6.4 Schema bump

DB schema v5 → v6 with the column additions above (§3.1, §5.4).

---

## 7. Invariants

Extending spec G's #26–#28:

- **#29** — `/run/mthydra` and `/var/log` MUST be on tmpfs on a running RU agent. Re-verified each refresh tick; failure → `shutdown -h now`.
- **#30** — EU sing-box config MUST contain a UUID for every (box_id, reality_uuid) where `ru_boxes.state='live'` AND `onward_credentials.revoked_at IS NULL`. Wheel refuses to publish a config that fails this check. (Defence against accidentally-included revoked UUIDs.)
- **#31** — An EU node with `state IN ('active','standby')` MUST have non-NULL `cover_sni` and `reality_pubkey`. Startup invariant check.
- **#32** — Per-exit `cover_sni` in the published descriptor MUST equal the corresponding `eu_nodes.cover_sni`. Descriptor-signer check.

---

## 8. CLI

New `mthydra-controller` subcommands:

- `data-exit-status` — prints sing-box pid, last config-write timestamp, UUID-allowlist size, `eu_exit_set` row presence.
- `data-exit-rewrite` — force a wheel tick now (regenerate config + SIGHUP). Idempotent.
- `data-exit-config-show` — print the current rendered sing-box.json to stdout. Read-only.
- `data-exit-reality-keygen` — operator-attested generation of the initial Reality keypair if not already present. Refuses if `eu_nodes.reality_pubkey` is already non-NULL (use `data-exit-reality-rotate` for rotation, deferred to a future spec).

No new RU-side CLI — the agent has no operator surface; control happens through provisioning + revocation on the controller.

### 8.1 Bootstrap obligations

`mthydra-controller init` (in active mode) seeds two new obligations:
- `e_ru_agent_provision_replace_drill_proven`: 30 × 24 hours (= 30 days)
- `e_data_exit_drill_proven`: 30 × 24 hours

§12 budget for E:
- Provision-then-self-terminate drill on a real cloud VM (RU box reaches steady state, controller revokes credential, agent self-terminates within the 6h budget) — resets `e_ru_agent_provision_replace_drill_proven`
- sing-box-kill drill (operator SIGKILL sing-box on the EU exit; wheel restarts it within one tick; `eu_exit_set` row reappears) — resets `e_data_exit_drill_proven`

---

## 9. Cross-spec contracts

- **Spec G** must amend `provision_box()` to assign `reality_uuid` atomically and include all new fields in the seed bundle. Schema bump v5 → v6 happens here.
- **Spec B** must extend the descriptor with per-exit `cover_sni` and `reality_pubkey`. Schema label `mthydra.descriptor.v2`.
- **Spec D** is unchanged. The mtg binary tracked by D is one artifact; the RU agent tarball is a separate artifact built directly from this repo (no upstream tracking — it's our own code) and pushed to B2 on each tagged release. A small CI step packages `src/mthydra/ru_agent/` + a `requirements.txt` pin into a tarball; the upload mechanics reuse `S3Destination` helpers but don't go through spec D's image lifecycle.
- **Spec F** is amended only by the new `eu_nodes` columns (`cover_sni`, `reality_pubkey`, `data_exit_started_at`) and the optional `degraded` state value. `promote-active` is unchanged.
- **Spec H** (future) will introduce per-shard descriptor filtering. The data-exit wheel and the RU agent are unaware of sharding; they consume whichever descriptor reaches them.
- **Spec I** (future) is the external observer; this spec does not depend on or talk to I.

---

## 10. Honest residuals

- **Live network testing requires real VMs.** No pytest covers the path "real client → real RU box → real EU exit → real Telegram." A deployment runbook will own that gate.
- **Telegram DC subnet maintenance** is a manual ops responsibility. When Telegram changes its IP plan (rare but happens), `controller.toml`'s `[data_exit.telegram_dcs]` must be updated and `data-exit-rewrite` invoked on each EU exit. We do not auto-pull this list — that would be an attack surface (an MITM on Telegram's published config could redirect the data path).
- **Reality keypair rotation** is operator-attested only in this spec. A future amendment can add controlled rotation with the spec-G-style migration pattern.
- **Make-before-break on credential revocation is "eventually" rather than instant.** A revoked UUID is dropped from the config + SIGHUP, but existing established TLS connections from that UUID continue until they naturally close. For surgical termination, the operator restarts sing-box (drops all connections). We accept this; design.md's "kill latency is in minutes" framing covers it.
- **The agent runs as root** (needs raw iptables + tmpfs mounts + `shutdown -h`). Running mtg + sing-box as separate non-root child processes is in scope (Unix `setresuid` after fork), but the supervisor itself stays root.
- **No surgical credential revocation on the RU box.** When the controller revokes a credential, the EU side stops accepting that UUID's handshakes (next tick). The RU box only notices via "all exits unreachable >2h" → self-terminate. This is the design, but it means there's a 2h window where the RU box is alive without service.
- **`/var/log` on tmpfs interacts with journald.** The bootcmd mounts tmpfs over `/var/log` before journald starts, then configures `Storage=volatile`. Together this should keep journals in RAM, but the precise interaction depends on the cloud image's journald default. Verify per-image during the provision-replace drill (`e_ru_agent_provision_replace_drill_proven`).

---

## 11. Test discipline

### 11.1 Unit tests

- `tests/unit/ru_agent/test_seed.py`
- `tests/unit/ru_agent/test_hardening.py`
- `tests/unit/ru_agent/test_binary.py`
- `tests/unit/ru_agent/test_config_gen.py` — golden-file rendering
- `tests/unit/ru_agent/test_iptables.py` — mock subprocess
- `tests/unit/ru_agent/test_descriptor_refresh.py` — mock B2, clock-faked
- `tests/unit/ru_agent/test_supervisor.py` — mock subprocess, crash-loop window
- `tests/unit/ru_agent/test_ast_no_controller_imports.py` — AST walk
- `tests/unit/controller/data_exit/test_config_writer.py` — golden-file
- `tests/unit/controller/data_exit/test_wheel.py` — drive tick-by-tick
- `tests/unit/controller/data_exit/test_exit_set.py`
- `tests/unit/controller/data_exit/test_telegram_dcs.py`

### 11.2 Integration tests

- `tests/integration/test_ru_agent_offline.py` — full agent against stub seed; mocked subprocess + iptables + B2
- `tests/integration/test_data_exit_lifecycle.py` — provision 3 RU boxes → wheel renders config → revoke one → wheel removes it → terminate another → wheel removes it
- `tests/integration/test_eu_node_promotion_with_data_exit.py` — standby running sing-box stays serving across `promote-active`

### 11.3 Coverage targets

- `mthydra.ru_agent.*` ≥ 90% line coverage
- `mthydra.controller.data_exit.*` ≥ 90% line coverage

### 11.4 Failure-mode catalogue (mapped to design.md §4/§13)

| Failure | Path |
|---|---|
| Seed tampered (signature) | startup `shutdown -h` |
| Image binary tampered (sha256) | startup `shutdown -h` |
| Hardening regression (swap re-enabled, etc.) | per-tick verify → `shutdown -h` |
| Descriptor expired & refresh path broken | 6h counter → `shutdown -h` |
| All EU exits unreachable | 2h budget → `shutdown -h` |
| mtg or sing-box crash-loop | 4-in-5-min counter → `shutdown -h` |
| iptables rules tampered | second consecutive miss → `shutdown -h` |
| EU sing-box crash | wheel restart; 3 in 5min → `eu_nodes.state='degraded'` + `eu_exit_set` row cleared |
| EU disk full at config-write time | wheel skip-tick + audit + retry next tick |

---

## 12. Status

Spec drafted 2026-05-23. Subsumes the `F2` placeholder. Ready for implementation plan.
