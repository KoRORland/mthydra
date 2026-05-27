# Resilient Telegram Access Under DPI — Master Design Document

## What this is

A design for a **private** circumvention fleet: a small, trusted circle of family and friends keeping access to Telegram (a lawful messaging app) from inside Russia under Roskomnadzor DPI. Not a public service. The operator is a single person, physically outside Russian jurisdiction, and always reachable in the EU.

This is defensive anti-censorship work in the same problem space the Tor Project, OONI, and the sing-box/MTProto-proxy community document openly. The design is deliberately honest about what it does *not* solve; those sections are the important ones, not boilerplate.

## How to read this document

- **§1–§4** are the architecture: topology, transport, operational model, and the threat model with its honestly-bounded residuals.
- **§5** is the build register: what is designed-and-drafted vs still open.
- **§6–§11** are the built operator artifacts (T1–T6), ordered by when you'd reach for them operationally, not by number.
- **§12** consolidates every recurring obligation in one place, with a single health metric.
- **§13** is the honesty ledger — the explicit list of what is *bounded* vs *closed*, kept separate so later edits cannot quietly upgrade one to the other.

The recurring theme throughout: **each artifact existing is the cheap half. Its value is entirely in the exercised half** — per-user setup actually run, dry-runs actually performed, thresholds actually tuned. An unexercised runbook is the same trap as an untested backup.

---

# §1 — Topology

```
[ Endpoint: Telegram client, app-lock on, no system VPN ]
        │  user → RU hop   (crosses DPI — must look like ordinary browsing)
        ▼
[ RU in-points: stateless container relays on minimal domestic VMs ]
        │  RU → EU hop     (datacenter-to-datacenter, less scrutinised)
        ▼
[ EU node(s): active = data-exit + controller; warm standbys = exit-only ]
        ▼
[ Telegram infrastructure ]
```

**RU in-points** — stateless cattle. Nothing in space (tmpfs only; no swap; volatile journald; no core dumps) and nothing in time (reboot is treated as death, not a recoverable state). Replace-on-burn, never repair.

**RU → EU hop** — an obfuscated tunnel, not bare WireGuard/VPN, rotating across multiple EU IPs on a jittered schedule so there is no static RU↔EU pair to correlate.

**EU side** — one active node runs two logically separate roles: a replaceable *data-exit* and a stateful *controller* (health probing, self-monitoring, credential lifecycle, multi-path publishing). One or more **warm standbys** run exit-only and are controller-capable on **manual operator promotion**. No auto-failover, no consensus — a deliberate choice for a single-operator system, trading recovery latency for the elimination of split-brain risk.

---

# §2 — Transport

**Front-line, user-facing: MTProto Fake-TLS only.** It is the only option that is natively self-disguising (benign TLS fall-through to a real cover domain for any non-client prober) *and* reduces to a single `tg://proxy?…` link / QR — satisfying the non-negotiable "trivial onboarding, no extra software, no settings" constraint for non-technical users.

**SOCKS5: dropped.** Bare SOCKS5 fails active probing; the inner-carrier role was never load-bearing.

**Reality/VLESS: not user-facing.** It cannot be used client-free — the protocol lives in the client implementation, and there is no Reality-speaking client inside Telegram. Reality is retained only for the internal RU→EU hop, where both ends are our boxes and a client is fine.

**Consequence, consciously owned:** the front line is a **single-transport monoculture**. There is no seamless second transport. Resilience comes from (a) staying current with upstream MTProto implementations (T4), (b) aggressive cover-domain rotation with strict burned-SNI hygiene (T5), (c) continuous realistic active-probing as early warning (T3), and (d) a pre-staged, operator-assisted **break-glass** client path used *only* in a circle-wide burn (T1).

---

# §3 — Operational model

**Provisioning seed** arrives via instance metadata / cloud-init into tmpfs at first boot: per-box Fake-TLS SNI, per-box revocable onward credential, descriptor-verify public key, transport role. Never written to disk.

**Signed endpoint descriptor** — the controller signs the current valid EU exit set; the RU box verifies (verify-not-forge) and spreads onward connections across the set, make-before-break, jittered. A seized RU box yields only a verification key.

**Replace-on-burn** — any anomaly → terminate + replace with fresh SNI + fresh credential + current image. Burned SNI never reused. A dead box's every parameter is assumed adversary-known.

**Distribution to users** — primary: a private Telegram channel/bot publishing a *rotating subset* (never the whole fleet), to a trusted circle who confirm links with the operator out-of-band. Backstop: email from the EU controller to the operator — alerting + dead-man's-switch + manual fallback. Both ends outside DPI; deltas only, never a standing full inventory.

**Controller state** — the one stateful "pet." Authoritative items: burned-domain set, credential-authority key material, publishing tokens, current inventory snapshot. Encrypted to an operator-held key not present on any deployed box. Pushed off-box on change and on timer; multiple generations retained; restore is operator-driven and assumes compromise of the old controller by default (see T2).

---

# §4 — Threat model & honestly-bounded residuals

The design is solid against the failures it was built for: a blocked in-point (replace-on-burn), a seized in-point (stateless in space and time, revocable credentials, verify-not-forge), and technical loss of the controller (backed-up state, operator-driven standby promotion). Operator legal exposure is treated as low given the out-of-jurisdiction assumption (held consciously, not asserted as fact).

The genuinely unclosed parts — stated here once, plainly, and tracked in the §13 ledger:

- **Front-line transport monoculture** — *accepted, not solved.* Mitigated by T4 + T5, recovered by T1. With one client-free transport this is a race you stay in, not one you win.
- **A compromised in-point can see a connecting user's IP** — *bounded, not closed.* Inherent: a working relay must know where to return packets. Bounded by T6 shard size × T3 dwell time. The irreducible core is a user who connects through a compromised box during the short window before T3 kills it.
- **Global timing/volume correlation** — *not solved at any scale*, including by Tor against a global adversary. Mitigated in practice mainly by the fleet staying small, private, and undiscovered — an obscurity assumption held consciously, not a guarantee.
- **On-device inspection of a stopped user** — *bounded, not closed.* Installing/using a VPN or Telegram is not currently illegal in Russia, but that is a policy variable that can change, and an *active* Telegram plus a visible circumvention footprint can itself draw suspicion during the informal, not-strictly-lawful device checks that are common in practice. Bounded by T13 (§14): steady-state footprint reduced to a single Telegram proxy line (no extra app installed), account locked behind a passcode + cloud password. The irreducible core — a coerced or forcibly-unlocked device — is outside our control and rests on user caution.

None of these is a build-stopper for a small trusted circle. All of these are reasons to keep N small, keep it private, and keep the test clock (§12) running. The safety margin is **short dwell time on anything bad plus loud failure to the operator** — and that margin holds only if §12 runs on a schedule rather than on noticing.

---

# §5 — Build register

| ID | Item | Status | Priority |
|---|---|---|---|
| T1 | Break-glass kit | **Drafted** (§6). Not done until per-user Part 1 setup is run; trigger message finalized/translated | Critical |
| T2 | Controller restore/promotion runbook | **Drafted** (§7). Not real until the end-to-end dry-run (Case A *and* B) is performed | Critical |
| T3 | Monitoring spec (Jobs 1/2/3) | **Drafted** (§8). Not real until thresholds tuned on live data and vantages verified | High |
| T4 | Image-currency process | **Drafted** (§9). Not real until upstream is tracked on a cadence and one validated promotion is exercised | High |
| T5 | Cover-domain discipline | **Drafted** (§10). Not real until the verified pool exists and burned-set is structurally enforced | High |
| T6 | Per-user in-point isolation | **Drafted** (§11). Not real until shard size/cadence chosen and disjointness controller-enforced | High |
| T7 | Independent probe vantages | **Open** — sourcing disposable, non-attributable, Russia-approximating probes; rotating them. Has real unresolved design substance | High |
| T8 | Cold-acquisition verification | **Open** — periodic reboot-then-image test that tmpfs/no-swap/volatile-journal actually yield a blank disk on the current provider image; verify container runtime isn't persisting config | Medium |
| T9 | Off-box backup independence | **Open** — backups on infra independent of the EU node *and* its provider; decryptable with operator key only | Medium |
| T10 | Multi-path publishing | **Open** — more than one user-facing distribution mechanism so a single channel takedown doesn't sever the circle | Medium |
| T11 | Endpoint-descriptor signing scheme | **Open** — pin exact contents, keys, validity window, generation so verify-not-forge is airtight | Medium |
| T12 | Document the obscurity assumption in the operator runbook | **Open** — make "stay small and unremarkable" an explicit, maintained control | Low |
| T13 | User-device opsec & inspection-resistance | **Drafted** (§14). Not real until every user has Controls 1+2 enabled and the opsec note is translated + delivered per onboarding (runbook §5.5) | High |

T7–T12 are intentionally left as open TODOs. T7 in particular has genuine unresolved design substance (vantage sourcing without creating an attributable pattern) and is worth a dedicated session on its merits rather than a template fill.

---

---

# §6 — T1: Break-Glass Kit

*Reach for this when T3 Job 3 fires: the front-line transport itself is burned circle-wide.*

**Purpose:** Recovery procedure for a circle-wide front-line transport burn (Job 3 fires). The steady-state no-software path is dead in this scenario; break-glass is the degraded bridge back to normal. Scope: private trusted circle, mostly non-technical, mostly Android, reachable out-of-band on a Telegram-independent channel.

**Core principle:** All difficulty is moved to calm-time setup. The burn-day user action is reduced to: *open an app already on the phone, tap one button.* Nothing is found, installed, or configured during the event.

---

#### Part 1 — Calm-time setup (once per user, with operator help, long before any burn)

Done during a relaxed out-of-band session, per Android device:

1. Install the Reality-capable client. Leave it **dormant** — not used in steady state.
2. Pre-import the break-glass profile via a single QR/link. **No field is ever typed by the user.** Profile points at the **separate dormant break-glass path**, NOT the primary fleet.
3. Supervised one-time test: user opens app, taps connect once, confirms it works, closes it. *A path never once exercised by the user is not a path you have.*
4. Teach exactly one sentence: *"If Telegram stops working and you get a message from [operator] on [out-of-band channel] saying switch, open [app] and tap connect."*
5. Hand over the user-facing card (Part 3). Explain the one accepted trade: a dormant app now lives on the phone (mild deniability cost) in exchange for surviving a full burn.

If any user cannot reach step 3 unaided after setup, the kit has failed its floor — absorb more into setup, never into burn-day.

---

#### Part 2 — Burn-day trigger message (pre-written, pre-translated, stored here)

Sent over the **Telegram-independent out-of-band channel** only after the Part 4 gate is passed. Keep it this short:

> *Telegram is being blocked. Switch now: open [app name], tap Connect. That's all. Reply "ok" when it's connected. — [operator]*

(Finalize the real app name, operator name, and translated text before storing. Do not improvise wording on burn day.)

Return-to-normal message (also pre-written):

> *Back to normal. Telegram works directly again — you can stop using [app name]. Nothing else to do. — [operator]*

---

#### Part 3 — User-facing card (one image, pre-distributed)

A single screenshot of the user's actual Android screen showing the one Connect button, with one arrow. Not prose steps. One image, one arrow, one button. If a second image is needed, it exceeds the floor — push the difficulty back into Part 1.

Distributed during Part 1 and re-sendable over the out-of-band channel on burn day.

---

#### Part 4 — Operator pivot runbook (execute at alarm)

1. **Gate: confirm real burn, not flap.** Require the Job 3 pattern: many boxes failing the *same check class* while spanning *different providers, IPs, regions, SNIs*. If not that pattern → Job 2 (replace boxes), do NOT invoke break-glass. False invocation exposes the dormant path early and erodes user trust in the signal.
2. **Activate the dormant break-glass path.** Verify it carries traffic *before* telling anyone to use it.
3. **Send the Part 2 trigger message** over the pre-agreed out-of-band channel, per user.
4. **Confirm per user.** Track "ok" replies. Direct-call non-responders (the reason channel choice mattered).
5. **Freeze same-transport provisioning.** Stop spending fresh SNIs/boxes on a burned transport.
6. **Return to normal.** When a fresh steady-state transport is healthy: send the return-to-normal message; users revert to the no-software path; **re-stage the break-glass path with rotated parameters** (it was exposed in use → treat as partially burned). An un-re-staged path after use is stale.

---

#### Accepted residuals (own these, don't bury them)

- **Device artifact:** the dormant client erodes some steady-state device-deniability. Conscious trade, explained to each user in Part 1.
- **Maintenance debt:** the separate dormant path rots like an untested backup. T1 inherits the T2/T3 test-clock obligation — periodically verify it is still live, current, importable, one-tap.
- **Degraded bridge, not a home:** break-glass exists to carry the circle back to steady state, not to run indefinitely; the longer it runs the more it is itself a target.
- **Trigger dependency:** the entire kit depends on the out-of-band channel being genuinely Telegram-independent for *every* user. Re-verify this periodically; if it ever quietly becomes "we use Telegram for that too," the kit has silently lost its trigger.

---

#### Standing obligations (recurring, not one-time)

- Re-stage break-glass parameters after every use.
- Periodic dormant-path health/currency check (alongside T2/T3 test clock).
- Periodic re-verification that each user's out-of-band trigger channel is real and Telegram-independent.
- Re-run Part 1 step 3 (the supervised test) for any user whose device changes, since a pre-imported profile does not survive a new phone.

---

# §7 — T2: Controller Restore / Promotion Runbook

*Reach for this when the controller is lost — technical failure or suspected compromise.*

**Purpose:** Recover the EU controller — the one stateful component the cattle model does not protect — from operator-held backups, by promoting a warm standby. Scope: single operator, physically out of jurisdiction, always EU-reachable. No auto-failover, no consensus (deliberate, single-operator system).

**Core principle:** An untested restore is not a restore. This runbook only counts once it has been executed end-to-end on throwaway infrastructure. Metric to track: *time since recovery was last proven.* It must never go stale.

---

#### Gate 0 — Classify the failure (do this first, every time)

Branch the entire runbook on **why** the controller is gone:

- **Case A — down / lost, not exposed:** host died, provider suspended account, disk/region failure. State intact in backups; adversary has NOT seen it. → Faithful restore.
- **Case B — suspected compromised:** anomalous controller behaviour, evidence of access, OR ambiguous loss you cannot positively rule out as clean. → Safe-direction restore (rotate everything).

**Default under uncertainty = B.** Cost of B-on-a-clean-loss = some wasted rotation. Cost of A-on-a-real-compromise = you re-arm a fleet whose credential authority and published in-points the adversary already holds. Asymmetric → when unsure, B.

---

#### What the controller holds (restore inventory)

| State | Class | On restore |
|---|---|---|
| Credential-authority key material | Authoritative, crown jewel | A: restore as-is. B: **re-key**, fleet rotates via replace-on-burn |
| Burned-domain set | Authoritative | Always restore faithfully (reusing a burned SNI is the classic failure — must survive restore in BOTH cases) |
| Publishing tokens (bot/channel, email sender) | Rotatable | A: restore. B: **mint fresh**, update channel/sender |
| Provider API credentials | Rotatable | A: restore. B: **rotate** |
| Fleet inventory snapshot | Cheap to rebuild | Restore, then reconcile against provider APIs (self-heals on next health sweep) |
| Published-subset / rotation history | Regenerable | Intentionally **discard and rotate forward** (safe direction) in BOTH cases |

Backups are encrypted to an **operator-held key not present on any deployed box**. The live controller can write backups it cannot itself decrypt. Multiple generations retained; generation number is the tripwire that backups are flowing.

---

#### Restore / promotion procedure

1. **Classify (Gate 0).** Record A or B and the reason. This decision drives every later step.
2. **Select a warm standby.** It already runs exit-only and already carries some RU tunnels. Promotion adds the controller role; it does not disrupt the data path.
3. **Retrieve the latest good backup generation.** Pull off-box (storage independent of the dead node *and its provider*). Decrypt **locally on the operator machine** with the operator-held key. Sanity-check generation number and contents before pushing anything.
4. **Push state to the standby and start the controller role.**
5. **Apply the Case branch:**
   - **A:** restore authority key, tokens, API creds as-is. Restore burned-domain set faithfully.
   - **B:** re-key credential authority; mint fresh publishing tokens and update channel/sender; rotate provider API creds. **Still restore the burned-domain set faithfully** (never the exception).
6. **Always, both cases — rotate the published subset forward.** Retire currently-public in-points on an accelerated timer; publish fresh ones. (If the old controller died ungracefully you do not know whether its published set was also exposed; rotating forward is the safe direction and is why published-subset history is treated as regenerable.)
7. **Reconcile fleet inventory** against provider APIs; let the next health sweep self-heal residual drift.
8. **Verify recovery:**
   - Health sweep green from an independent, Russia-approximating vantage (not EU→RU).
   - Email "all green" / dead-man's-switch cleared (operator is receiving heartbeats again).
   - At least one user path confirmed working end-to-end.
9. **Record:** date, Case, standby used, backup generation, time-to-recover. Update *time since recovery was last proven* = now.

---

#### Case B fleet consequence (state plainly)

Re-keying the credential authority invalidates every RU box's onward credential. Do **not** try to re-credential boxes in place — that contradicts the cattle model and risks touching boxes you should treat as compromised. Instead let the fleet **roll via replace-on-burn**: provision fresh boxes against the new authority, age out the old ones. The fleet is cattle precisely so a full authority rotation is survivable without per-box surgery. Expect a degraded window during the roll; this is the accepted cost of B and is why B is reserved for actual/suspected compromise, not routine loss.

---

#### Standing obligations (recurring, not one-time)

- **Scheduled dry-run** on throwaway infrastructure: execute this entire runbook (Case A *and* Case B paths) end-to-end. The success metric is *time since recovery last proven*; if it goes stale, the runbook is aspirational, not real.
- **Backup-flow check:** confirm encrypted generations are actually arriving off-box and are decryptable with the operator key alone (a backup you cannot decrypt is not a backup).
- **Generation-gap alarm:** if the off-box generation number stops advancing, the live controller may be silently failing to back up — investigate before it becomes an unrecoverable loss.
- **Standby readiness:** periodically confirm a warm standby is actually exit-live and promotable (an un-rehearsed standby is the controller-side analogue of T1's untested user path).

---

#### Honest residuals specific to T2

- Recovery is operator-driven and manual by design. This trades recovery *latency* for the elimination of split-brain / conflicting-authority risk — the right trade for a single operator, but it means the controller's downtime is bounded by *operator availability*, which the design already assumes is high (out-of-jurisdiction, always EU-reachable). If that assumption ever weakens, T2's bound weakens with it.
- Gate 0 is a judgement call under uncertainty. Defaulting to B makes it safe but not free: every ambiguous loss costs a fleet roll. This is accepted on purpose; do not "optimise" it away by getting clever about declaring losses clean.
- T2 depends on the same discipline as T1 and T3: the backup and the standby are only real if exercised on a clock. The failure mode is silent until the moment of need.

---

# §8 — T3: Monitoring Specification

*The early-warning system. Everything else is a response to a failure this must detect in time.*

**Purpose:** Turn the Job 1/2/3 monitoring from prose into concrete, codifiable detection logic. T3 is the *early-warning system* the rest of the design depends on: T1 (break-glass) and T2 (controller restore) are responses to failures T3 must detect *in time for the response to matter*. Scope: own-fleet observation only — detecting when our own RU boxes are blocked or tampered with.

**Core principle:** A tampered box lies about itself. All trust comes from *external comparison against a pinned known-good profile*, never from asking the box how it is.

---

#### The pinned known-good profile

Built at image-build time, versioned with the image, stored on the controller (operator side). It is the reference every probe compares against. Minimum contents:

- Expected TLS handshake characteristics for a non-client connection to :443 (cipher/extension ordering, cert chain consistent with the assigned cover domain — i.e. what the *real* cover site would present).
- Expected response to deliberately malformed / non-protocol input (timing and content).
- Expected responsive surface: **:443 only**, nothing else.
- Baseline latency/loss distribution for a valid path (per box, learned over its first stable period, plus a fleet norm).
- Image version + transport build hash, so "expected behaviour" changes *deliberately* on a refresh (T4), never by drift.

---

#### Job 1 — Realistic active probing

**Vantage:** disposable, non-attributable, Russia-approximating. NOT EU→RU (RKN may not interfere on that path, producing false greens). Rotate vantages so the probe pattern is not itself a stable fingerprint.

**Schedule:** jittered, not metronomic (a fixed-interval probe is a signature). Target a mean interval short enough that detection latency ≪ user-pain onset; exact value is an operator tunable, documented per deployment.

**Checks, per box, per cycle:**

| Check | Method | Known-good | Deviation = |
|---|---|---|---|
| TLS fall-through | Connect to :443 with NO valid secret | Genuine TLS consistent with assigned cover domain | Disguise broken / tampered |
| Cover-domain consistency | Compare cert/handshake to assigned SNI | Exact match to controller-assigned value | Misconfig or tamper |
| Surface | Port/behaviour scan from vantage | :443 only, cover behaviour | Anything else responsive |
| Valid-path liveness | Connect with a **probe** credential (never a user's) | Carries to EU and back within bounds | Path down |
| Latency/loss | Sample distribution, not one point | Within box baseline + fleet norm | Possible inserted tap / redirect |
| Behavioural identity | Response to malformed input vs profile | Matches pinned profile | Identity failure |

---

#### Job 2 — Anomaly-as-compromise termination

**Rule:** a deviating box is terminated and replaced, never investigated in place. Do not SSH into a possibly-tampered box — that wastes time and risks the operator path.

**Hard-kill (single occurrence, no debounce):**
- Failed TLS fall-through
- Cover-domain / cert mismatch
- Any responsive surface beyond :443
- Behavioural-identity failure

Rationale: each is a signature of *either* a burned disguise *or* an active tamper. In both cases the box is worthless-or-dangerous; cheapest correct action is destruction. False-positive cost (kill a healthy box) is trivial at this fleet size; false-negative cost (run a tapped box) is a user's IP in an adversary's log.

**Soft (debounced, but endpoint is still terminate):**
- Latency/loss divergence
- Intermittent valid-path failure

Debounce: require **N-of-M consecutive bad cycles across rotated vantages** before acting (suggested starting point M=4, N=3 — operator-tunable; tighter for higher-paranoia deployments). Debounce reduces churn from transient Russian-network noise; it does NOT convert "anomalous" into "tolerated."

**On termination (every kill):** revoke onward credential immediately → remove from published subset → mark SNI burned, never reuse → provision replacement with fresh SNI + fresh credential + current image. Treat dead box's every parameter as adversary-known.

**Deliberately surrendered:** root-cause certainty. You will sometimes kill flaky-but-clean boxes and rarely know if a killed box was truly tampered. Accepted on purpose — the design optimises *short dwell time* over diagnosis, because dwell time of a bad box is what actually exposes users.

---

#### Job 3 — Correlated-fleet-failure (protocol-burn tripwire)

**Discriminator (this is the load-bearing definition):** multiple boxes failing the **same check class** within a short window **while spanning diverse providers, IP ranges, regions, AND cover domains**. Independent boxes dying for independent reasons across the *same* provider/range is normal attrition (Job 2). Correlated failure across *diversity axes* is the protocol itself being fingerprinted.

Concrete trigger condition (starting point, operator-tunable):
- ≥ K boxes (e.g. K=3) failing the *same* hard-kill class
- within a rolling window W (e.g. 30 min)
- spanning ≥ 2 distinct providers AND ≥ 2 distinct regions AND ≥ 2 distinct cover domains
- → classify as **protocol burn**, NOT box death.

**Response (opposite of Job 2 — do NOT mass-replace):**
1. Alert operator out-of-band immediately (email / dead-man's channel).
2. Freeze same-transport provisioning (stop spending fresh SNIs/boxes on a burned transport).
3. Invoke T1 break-glass (this trigger condition *is* T1 Part 4 step 1's gate).

**Latency = outage duration.** With one front-line transport, time from burn-onset to Job 3 firing *is* the circle's dark period. Job 3 runs on the freshest signal; the T1 kit must already be staged.

---

#### Keystone — controller liveness

Sits above Jobs 1–3. A silently dead controller = no job runs, boxes age out unreplaced, circle goes dark with NO alarm. Therefore:
- Controller emits a periodic "all green" heartbeat to the operator out-of-band.
- **Loss of heartbeat is itself the alarm** (dead-man's switch) → operator invokes T2.
- Heartbeat must traverse a path independent of the fleet's data path.

---

#### Honest residuals specific to T3

- Job 2 shortens but cannot close the compromised-in-point window: a working relay must see who connects to it. Mitigation is short dwell + per-user isolation, not prevention.
- Job 3 thresholds are a tuning compromise: too loose → burn detected late (longer outage); too tight → false burn declarations that needlessly invoke T1 and expose the dormant path. The starting numbers above are *starting points*, to be tuned against observed false-positive/negative rates, not constants.
- All of T3 assumes the pinned known-good profile is itself current. A stale profile (image refreshed but profile not re-pinned) silently makes every probe compare against the wrong reference. T3 is therefore coupled to T4 (image-currency) discipline.
- Vantage quality is decisive and fragile: if vantages drift away from "Russia-approximating," Job 1 produces confident false greens. Periodically re-validate that vantages still see what a real user sees.

---

# §9 — T4: Image-Currency Process

*Keeps the single front-line transport from silently going stale. Hardens T3.*

**Purpose:** Keep the RU relay image's MTProto Fake-TLS transport current with upstream, so deployed boxes never silently drift onto a stale, already-fingerprintable build. Directly hardens T3 (a refreshed image with a stale known-good profile makes every probe compare against the wrong reference). Scope: own-fleet build/release hygiene only.

**Core principle:** Never upgrade a box. Build a new image; new boxes provision from it; old boxes age out via normal replace-on-burn and rotation. Currency = "what image do new boxes get," not "how do we patch the fleet." The cattle model is the rollout mechanism — for free.

**The non-trivial part:** the validation gate before a new image becomes the provisioning default. A new upstream build can be *less* evasive, can change behaviour in ways that break the T3 profile, or can regress cover-domain handling. Promoting an unvalidated image fleet-wide burns the fleet faster than any adversary could. T4 is mostly this gate.

---

#### Why this can't be skipped

- MTProto Fake-TLS is a moving target on *both* sides: Telegram iterates the protocol; RKN iterates detection.
- A box fine three months ago can be fingerprintable now while upstream already shipped the fix.
- Replace-on-burn does NOT cover this: a burned box replaced from the *same stale image* is just another burnable box. T4 is the only thing that closes the protocol-staleness gap.

---

#### The process

1. **Track upstream.** Watch the upstream MTProto proxy / transport implementation the image is built from for releases and, specifically, for changes motivated by detection/evasion. Treat an evasion-motivated upstream release as a *prompt to run T4*, not optional housekeeping.
2. **Build candidate image.** New image = new transport build + version + transport-build hash recorded. Everything else (tmpfs/no-swap/volatile-journal/no-coredump hardening, stateless seed model) unchanged and re-verified (this is also the T8 reboot-then-image check point).
3. **Pin a candidate known-good profile.** Generate the T3 known-good profile *against the candidate image* (TLS fall-through characteristics, malformed-input response, surface, baseline). This is the new reference — do NOT reuse the old profile against a new image.
4. **Canary.** Provision a small number of boxes from the candidate image into the live fleet, published to *no one* or to operator test paths only. Run full T3 Job 1 against them from real Russia-approximating vantages for a defined soak period.
5. **Validation gate (all must pass before promotion):**
   - Canary passes every T3 hard-kill check against the *candidate* profile.
   - Canary's real-vantage behaviour is *at least as evasive* as the outgoing image (not just "different" — regressions in evasiveness are a fail).
   - Cover-domain handling intact (cert/SNI consistency, fall-through to real site behaviour).
   - No new responsive surface beyond :443.
   - Latency/loss within acceptable bounds vs the outgoing baseline.
6. **Promote.** Candidate image becomes the provisioning default. Re-pin T3's active known-good profile to the candidate profile *atomically with promotion* (a promoted image with the old profile silently breaks all probing).
7. **Age out.** Do nothing to existing boxes. Normal replace-on-burn + rotation moves the fleet onto the new image over the natural box lifetime. No migration, no maintenance window.
8. **Rollback.** If the canary fails the gate: discard the candidate, keep provisioning from the current image, keep the current profile, record the failure reason. If a *promoted* image is later found regressed in the wild: revert provisioning default to the last-good image, re-pin its profile, accelerate rotation of boxes built from the bad image (treat them as soft-burned), investigate before re-attempting.

---

#### Coupling to other TODOs

- **T3:** the profile re-pin in steps 3/6 *is* the coupling. T3 is only valid if its known-good profile matches the image actually deployed. T4 owns keeping that true.
- **T8:** step 2's re-verification of the stateless/hardening properties on the new image is the natural place to run the reboot-then-image check.
- **Replace-on-burn / rotation:** is the rollout transport. T4 adds no new rollout mechanism; it only governs *which* image new boxes get.

---

#### Honest residuals specific to T4

- T4 reduces staleness latency to "time between an evasion-relevant upstream release and a passed validation gate." It does not make the transport un-fingerprintable — with a single front-line transport (consciously accepted), currency is a race you stay in, not one you win. A determined detection advance can still outpace any upstream; that scenario is a Job 3 burn → T1, not something T4 prevents.
- The validation gate depends on the canary soak being run against *genuinely* Russia-approximating vantages. A canary validated only from benign vantages produces false confidence and promotes a regressed image fleet-wide. T4's quality is bounded by vantage quality (same dependency as T3).
- "At least as evasive as the outgoing image" is a judgement call with imperfect signal — you cannot fully measure evasiveness, only approximate it against current detection. Promotion is therefore a calculated bet, not a proof. Default conservative: if the canary is ambiguous, do not promote.
- T4 is only real if upstream is actually tracked. An untracked upstream means evasion fixes ship and the fleet never learns — the silent-staleness failure T4 exists to prevent, reintroduced through neglect. This is the T4 entry in the standing-obligations clock.

---

# §10 — T5: Cover-Domain Discipline

*With the monoculture accepted, the cover domain is a primary defence, not a minor one.*

**Purpose:** Govern selection, rotation, and retirement of the Fake-TLS cover domain (SNI) each RU relay presents. With the front-line transport consciously accepted as a single-protocol monoculture, the cover domain is a *primary* defence — a large fraction of what stands between an active probe and a positive identification. Scope: own-fleet blending discipline only.

**Core principle:** The most common real-world failure in operations like this is not broken crypto — it is a bad or reused cover domain. T5 makes that failure structurally hard rather than relying on the operator remembering.

---

#### 1. What makes a good cover domain

A cover domain is good to the extent that a probe completing a TLS handshake to the box, and seeing it behave like that domain, learns *nothing suspicious*. Properties to select for:

- **Genuinely reachable from inside Russia.** A cover domain that is itself blocked/throttled in Russia is worse than useless — it makes the box stand out *and* breaks the fall-through. (Verification is mandatory: section 4.)
- **High baseline traffic / unremarkable.** A domain many people legitimately reach over TLS is camouflage; an obscure one is a fingerprint.
- **Plausible for a Russian residential client to contact.** The disguise is "ordinary browsing"; the domain must fit that story for the box's apparent location.
- **Not associated with circumvention.** Never a domain known for proxies/VPNs/anti-censorship — that inverts the camouflage.
- **Stable TLS behaviour** the box can faithfully present on fall-through (cert chain, handshake characteristics consistent with the real site).

Do **not** select: domains you control (defeats the point — a prober can correlate), niche domains, anything politically loaded, anything already saturated with circumvention traffic, or anything whose real TLS behaviour the box cannot convincingly mirror.

---

#### 2. The burned-set rule (non-negotiable)

- Every cover domain ever assigned to a box that is later **terminated for any reason** is marked **burned** and **never reused**, fleet-wide, permanently.
- The burned set is **authoritative controller state** and **must survive controller restore in both Case A and Case B** (this is already specified in T2's restore inventory — T5 is why it is non-negotiable there).
- Rationale: a terminated box's parameters are assumed adversary-known. A burned SNI reused on a fresh box hands the adversary a pre-correlated identifier — the single highest-leverage operator mistake. Structural enforcement (controller refuses to assign from the burned set) beats operator memory.
- "Burned" is monotonic: it only ever grows. Never "un-burn" a domain because the pool is running low — that is the exact rationalisation that causes the failure. Running low is a sourcing problem (section 5), not a reason to weaken the rule.

---

#### 3. Rotation independent of health

- A published cover domain is rotated on a **timer regardless of health**, not only on failure. A box can be fingerprinted without yet being blocked; health-only rotation leaves a silently-tagged box live.
- Any domain that has been **published** (i.e. handed to users via the distribution channel) is treated as having a **shorter expected lifetime** and rotated on an accelerated schedule vs unpublished reserve.
- Rotation = provision a fresh box with a fresh (never-burned) domain, publish it, retire the old box (its domain → burned). This rides the existing replace-on-burn/cattle mechanism; T5 adds no new rollout path.
- Rotation cadence is an operator tunable, documented per deployment; bias shorter under elevated threat. The principle is fixed (timer-based, published = shorter life); the number is tuned.

---

#### 4. Reachability verification before go-live (mandatory gate)

Before any domain is assigned to a box that will serve users:

- Verify from a **Russia-approximating vantage** (same vantage discipline as T3 Job 1 — NOT EU→RU, which gives false greens) that the domain is genuinely reachable and behaves normally over TLS from inside the censored environment.
- Verify the box's fall-through faithfully mirrors the real domain's TLS behaviour from that vantage (a mismatch is itself the fingerprint).
- A domain that fails this gate goes back to the candidate pool as "unverified," never to a live box. Failing this gate is NOT a burn (the domain wasn't exposed) — it is simply "not currently usable."

This gate is the difference between "we picked a plausible-looking domain" and "we confirmed it works where it has to." Skipping it is how the most common failure happens despite a good selection.

---

#### 5. Pool sourcing & sizing

- Maintain a candidate pool large enough that burned-set growth + rotation never pressures the burned-set rule. If the pool runs low, the response is **source more candidates**, never **reuse burned ones**.
- Periodically re-verify reserve candidates (section 4) — reachability from Russia changes over time; a candidate verified months ago may now be blocked.
- Pool state classes: `candidate-unverified` → `candidate-verified` → `in-use` → `burned` (terminal). Controller assigns only from `candidate-verified`; only ever moves toward `burned`.

---

#### Coupling to other TODOs

- **T2:** burned-set persistence across restore (both cases) is mandatory; T5 is the justification for T2's "always restore burned-domain set faithfully."
- **T3:** the go-live reachability gate uses the same Russia-approximating vantage discipline as Job 1; cover-domain/cert consistency is a T3 hard-kill check, so a T5 failure surfaces as a T3 kill.
- **Replace-on-burn / rotation:** the rollout mechanism for both health-driven and timer-driven domain rotation. T5 adds policy, not new machinery.

---

#### Honest residuals specific to T5

- A perfect cover domain does not make the transport un-fingerprintable — it removes the *easiest* identification path, not all of them. With a single front-line transport this is risk reduction, not elimination; a true protocol-level burn is still Job 3 → T1, unaffected by domain quality.
- Reachability-from-Russia is a moving target the adversary controls. A domain verified today can be throttled tomorrow without warning; T5's verification is point-in-time and must be re-run, not assumed durable (the T5 standing-obligation entry).
- The burned-set rule's value depends entirely on the burned set actually surviving restore. A botched T2 Case-A/B restore that drops the burned set silently re-enables the worst mistake — which is why it is called out as non-negotiable in both artifacts rather than once.
- Pool sourcing has a practical ceiling: good cover domains meeting all criteria are finite. Sustained high rotation under pressure consumes the pool; the design assumes rotation cadence and pool-replenishment are balanced by the operator, and degrades (not fails) if replenishment lags — the honest failure mode is "rotation slows," never "reuse burned."

---

# §11 — T6: Per-User In-Point Isolation

*Bounds (does not close) the compromised-in-point residual.*

**Purpose:** Cap the blast radius of gap 5 — the *unclosable* residual that a fully-compromised in-point can observe the IP of whoever connects through it. T6 does not prevent this (a working relay must know where to send return packets; nothing closes that). T6 ensures one bad box exposes **one person** for the short pre-termination dwell, not the whole circle's connection pattern or social graph. Scope: own-fleet blast-radius reduction only.

**Core principle:** Do not blindly maximise isolation. Full one-box-per-user minimises blast radius but creates a *stable per-user fingerprint* (one user always on one box is itself a correlation handle) and is the hardest to rotate. Full commingling is cheap but one bad box burns the whole circle at once. T6 chooses a defensible middle, consciously, because maximising isolation trades one gap-5 harm for a different one.

---

#### The tension, stated plainly

| Approach | Blast radius | Cost | New problem it creates |
|---|---|---|---|
| Full commingle (all users, shared boxes) | Whole circle per bad box | Cheapest, easiest rotation | One compromise = entire circle's pattern + social graph |
| Full isolation (1 dedicated box / user) | One user per bad box | Most expensive, slow rotation, most boxes | Stable user↔box mapping = a persistent per-user fingerprint |
| **Small-group sharding (chosen)** | One *shard* per bad box | Moderate | Manageable if shards are small and shuffled |

The chosen point: **small disjoint shards**, not individuals, not one big pool.

---

#### The design

1. **Shard the circle into small disjoint groups.** Each shard = a few users who share a set of in-points. Shard size is the blast-radius knob: smaller = less exposure per bad box, more boxes/cost; the operator picks the smallest size sustainable for the circle's actual size. For a genuinely small family/friends circle this may be 1–3 users per shard.
2. **No cross-shard commingling on a box.** A given in-point only ever serves one shard. A compromised box therefore exposes only that shard's members, never the whole circle, and never reveals that *other* shards exist (no fleet-wide social graph leaks from one box).
3. **Periodic shard reshuffle.** To defeat the "stable user↔box mapping is itself a fingerprint" problem of pure isolation, shard membership and shard↔box assignment are **rotated on a timer**, not fixed for the life of the circle. This means even the user↔shard relationship is not a durable handle. Reshuffle rides the existing replace-on-burn/rotation mechanism (new boxes, new assignment, old boxes age out → SNIs burned per T5).
4. **No shared stable per-user identifier reaches the box.** The box sees a connecting IP (unavoidable). It must NOT additionally see anything that lets it link that IP to a *persistent identity* across sessions or correlate across shards. Per-shard credentials/parameters, not per-user-global ones; the box can use them, never forge or cross-reference them (consistent with verify-not-forge throughout).
5. **Compromise of one shard's box → terminate + replace + reshuffle that shard.** On T3 Job-2 kill of a box, the affected shard is not just moved to a fresh box — it is **reshuffled** (membership and assignment changed) on the assumption the dead box's shard composition is now adversary-known. This is the T6-specific addition to the standard replace-on-burn response.

---

#### What T6 does and does NOT achieve

**Does:**
- Bounds gap-5 exposure to one small shard per compromised box, for the dwell time before T3 kills it.
- Prevents one bad box from revealing the whole circle's existence/size/social graph.
- Removes the durable user↔box fingerprint that naive full isolation would create (via reshuffle).

**Does NOT:**
- Prevent a compromised box from seeing its current shard's IPs during dwell. Unclosable. Mitigation remains *short dwell* (T3 Job 2) + *small shard* (T6) acting together — neither alone is sufficient.
- Protect a user whose shard's box is compromised *and* who connects during the dwell window before T3 fires. T6 shrinks *who*; T3 shrinks *how long*. Gap 5's irreducible core is the overlap of those two.
- Help if shards are too large to matter or reshuffle is skipped — then T6 degrades toward full-commingle behaviour silently.

---

#### Coupling to other TODOs

- **T3 Job 2:** the kill is the trigger for T6's reshuffle-the-shard step. T6 adds shard-reshuffle to the standard terminate+replace.
- **T5:** shard-box rotation/reshuffle consumes cover domains and grows the burned set like any rotation; T5's pool sourcing must account for T6 reshuffle cadence, not just health-driven rotation.
- **Replace-on-burn / cattle model:** reshuffle is just assignment change over the existing mechanism; T6 adds policy (sharding + reshuffle), not new machinery.
- **Gap 5 (design-level residual):** T6 is the *who* mitigation; T3 Job 2 is the *how-long* mitigation. The master doc's gap-5 residual statement should read as "bounded by T6 shard size × T3 dwell time," not "mitigated."

---

#### Honest residuals specific to T6

- Gap 5 is **not closed**, only bounded. T6's value is a strictly smaller blast radius, not elimination — a user in a compromised shard during dwell is still exposed. Stating otherwise would be dishonest.
- Shard size is a real cost/safety trade with no free optimum: smaller shards cost more boxes, more rotation, more pool consumption (T5). The operator must pick consciously; the design degrades gracefully (larger shards = more exposure) rather than failing, but "we made shards big to save money" silently reintroduces the full-commingle failure.
- Reshuffle frequency trades fingerprint-resistance against churn/cost the same way. Too infrequent → user↔shard becomes a durable handle again; too frequent → operational load and pool pressure. Another conscious tunable, not a constant.
- T6 assumes shards are genuinely disjoint on-box. Any accidental cross-shard commingling (mis-assignment, a box serving two shards) silently collapses the guarantee and leaks the existence of multiple shards from one box. This must be structurally enforced by the controller (refuse multi-shard assignment), not left to operator care — same lesson as T5's burned-set rule.
- For a very small circle, shards may be near-individual, which reintroduces some of the stable-mapping fingerprint that reshuffle is meant to defeat; reshuffle must work harder (more frequent) the smaller the circle, which is the opposite of what cost pressure wants. Stated so the operator does not discover it under load.

---

# §12 — Consolidated standing obligations

The whole design's safety margin depends on these running on a clock rather than on noticing. This is the single most important section for an operator.

- **T1** — re-stage break-glass after every use; periodic dormant-path health check; periodically re-verify each user's out-of-band trigger channel is genuinely Telegram-independent; re-run the supervised user test on any device change.
- **T2** — scheduled end-to-end dry-run of *both* Case A and Case B on throwaway infra; backup-flow + decryptability check; generation-gap alarm; standby-readiness check.
- **T3** — re-pin the known-good profile on every image refresh (couples to T4); re-validate that probe vantages still see what a real Russian user sees; tune Job 3 thresholds against observed false-positive/negative rates.
- **T4** — track upstream for evasion-relevant releases on a cadence; run canary → validate → promote → re-pin per refresh; never promote an ambiguous canary.
- **T5** — re-verify reserve cover-domain candidates from Russia-approximating vantages; replenish the pool so burned-set growth never pressures the never-reuse rule; confirm the burned set survives every T2 restore.
- **T6** — keep shards small and genuinely disjoint (controller-enforced, not memory-based); reshuffle shard membership and assignment on a timer (more frequently the smaller the circle); reshuffle the affected shard on every box compromise.
- **T13** — at onboarding, verify each user has enabled Telegram Passcode Lock + Two-Step Verification (cloud password) and has installed no extra circumvention app; re-verify on any device change; keep the user-facing opsec note translated and actually delivered.

**One metric to surface upward:** *time since each obligation was last proven.* If any goes stale, that protection is aspirational, not real.

---

# §13 — Honesty ledger

Kept separate and explicit so later edits cannot quietly upgrade *bounded* to *solved*.

| Item | Status | What carries it |
|---|---|---|
| Front-line transport detectability | **Accepted, not solved** | T4 currency + T5 cover-domain; recovered (degraded) by T1; it is a race, not a win |
| Compromised in-point sees a user IP | **Bounded, not closed** | T6 shard size × T3 dwell time; irreducible core remains |
| Global timing/volume correlation | **Not solved at any scale** | Mitigated mainly by staying small/private/undiscovered — an assumption, not a guarantee |
| Operator legal exposure | **Assumed low, not proven** | Rests entirely on the out-of-jurisdiction premise; revisit if that premise weakens |
| On-device footprint / inspection of a stopped user | **Bounded, not closed** | T13 (§14): minimal footprint (native Telegram proxy, no extra app) + passcode lock + cloud password; the coerced / forced-unlock core is outside our control and rests on user caution |

The design is honest about these *by construction*. Treat any future revision that softens this table as a regression, not an improvement.

---

# §14 — T13: User-Device Operational Security & Inspection-Resistance

*A user-side threat the network design does not touch: what is found on the phone itself.*

**Purpose:** The rest of this document defends the *connection* — the bytes crossing the DPI boundary. T13 defends the *device and account* against the other half of the real threat model: a user who is physically stopped and whose phone is inspected. Scope: reduce what an inspection reveals, and what an unauthorised holder of the phone can reach. Like T6, it **bounds**; it does not close.

---

#### The threat, stated plainly

As of now it is **not illegal** in Russia to install or use a VPN, other DPI-avoidance software, or Telegram itself. Two things make that an unsafe thing to lean on:

1. **It can change.** Legal status is a policy variable, not a constant. A design that is only safe while today's rules hold is not safe.
2. **An active Telegram plus a visible circumvention footprint can itself draw suspicion during a device check** — and informal, not-strictly-lawful inspections of what is on a phone are common in practice. The question at the checkpoint is rarely "is this illegal"; it is "does this person look unusual."

So the user-side goal is **unremarkableness**: a device that, on inspection, looks like an ordinary Russian user's, and an account that does not hand itself over if the phone is taken.

---

#### Control 1 — Smallest possible footprint (install nothing; use Telegram's own tools)

This is *why* the front line is MTProto Fake-TLS reached by a `tg://proxy` link (§2), restated as a security control rather than only a UX one:

- The circumvention lives **inside Telegram's built-in proxy setting**. No VPN app, no sing-box, no foreign tunnelling client is installed on the user's device for steady-state use. There is nothing extra to find.
- Everything else on the device behaves the way a proper Russian user's would — domestic apps present, no conspicuous foreign-tooling pattern. The proxy entry inside Telegram is the *only* artifact, and a Telegram proxy is itself an ordinary, widely-used Telegram feature.
- **The one acknowledged exception is the break-glass dormant client (§6).** It is a real footprint cost, owned consciously in §6 Part 1, and is *not* present for steady-state operation — only pre-staged for circle-wide-burn recovery. Until that path is needed, the steady-state footprint is "a proxy line in Telegram settings," nothing more.

---

#### Control 2 — Lock the account against an unauthorised holder

If the phone is taken (checkpoint, search, theft), the controls that matter are the ones already built into Telegram. Both are **mandatory** at onboarding (runbook §5.5):

- **Passcode Lock** (Telegram → Settings → Privacy and Security → Passcode Lock): a local lock on the Telegram app, with auto-lock enabled, so an unlocked *phone* does not equal an open *Telegram*.
- **Two-Step Verification / cloud password** (Settings → Privacy and Security → Two-Step Verification): a password required to log the account in on a new device, so possession of the SIM or an intercepted SMS code is not enough to seize the account and its history. Set a recovery email the user controls.

Together these mean a seized-but-locked device does not immediately yield the account, the chat history, or the active sessions.

---

#### What T13 does and does NOT achieve

**Does:**
- Removes the steady-state on-device circumvention artifact (Control 1) — there is nothing obvious to find on inspection.
- Raises the cost of seizing the account from a taken device (Control 2).

**Does NOT — own this plainly:**
- A passcode and a cloud password are **not fool-proof and not brute-force- or coercion-proof.** They do not survive a user compelled to unlock, a shoulder-surfed passcode, device malware, or a sufficiently determined forensic effort. They raise the bar; they do not close the threat.
- T13 cannot protect a user who is coerced, careless, or specifically targeted by a capable adversary. Much of what happens at an actual inspection is **outside our control** — it rests on the user's own caution and judgement in the moment.
- This is acknowledged, not engineered away. The honest position: minimise the footprint, lock the account, and **be clear with every user that the residual risk is real and partly theirs to manage.**

---

#### Honest residuals specific to T13
- The protection is only as good as the user's habits: a passcode never enabled, a cloud password written on the same phone, or a break-glass client left installed-and-used in steady state all silently erase it.
- "Unremarkable" is an obscurity property, not a guarantee (same class as §4's correlation residual) — it degrades the moment the circle or its behaviour becomes a known pattern.
- We can *require and teach* Controls 1 and 2 at onboarding (§5.5) and *re-verify* them on the §12 clock; we cannot enforce them on the device. The gap between "told the user" and "the user did it and kept doing it" is real and belongs to the operator relationship, not the controller.

---

#### User-facing opsec note (pre-written; translate before use)

Deliver verbally during §5.5 and as a short written note the user keeps, in their language. Keep it plain:

> - You don't need any extra app. Your Telegram is set up to connect through a proxy — that's all. Don't install VPNs or other tools for this.
> - Turn on a **Passcode Lock** in Telegram (Settings → Privacy and Security → Passcode Lock) and set it to lock automatically.
> - Turn on **Two-Step Verification** (Settings → Privacy and Security → Two-Step Verification) and remember the password — it stops anyone from logging into your account on another phone. Keep the password somewhere safe, NOT on this phone.
> - These help, but they are **not a guarantee.** If you are ever asked to unlock your phone, think about your own safety first — none of this protects a phone you are made to open.
> - If Telegram stops working, wait for a message from me on [out-of-band channel] before doing anything.
