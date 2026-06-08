# integration-mvp — a real fleet in containers

Walks the [MVP quickstart](../../doc/quickstart-mvp.md) end-to-end against
local containers instead of AWS + TimeWeb, and verifies the **mtg tunnel comes
up and is reachable from the RU vantage**. Every `mthydra-*` call is the real
CLI; only the three external SaaS dependencies are stood in for locally.

```
make-equivalent:  bash harness/integration-mvp/run.sh
```

Requirements: Docker, an amd64 host (the mtg ELF is amd64), outbound internet
the first time (pulls `ubuntu:24.04`, `minio/minio`, `minio/mc`, sing-box, and
the cached mtg release tarball under `../agent-boot/.cache/`).

## Topology (one Docker network, `mtnet`)

| Container | Role | Quickstart parts it runs |
|---|---|---|
| `mt-minio` | S3 backend (stands in for AWS S3) | — |
| `mt-controller` | EU controller | 3 (install/bootstrap + first-descriptor), 5, 6, 7 |
| `mt-vantage` | RU probe vantage | 5 (host), runs the openssl probe |
| `mt-rubox` | RU box | 7 (boots the **real** `mthydra.ru_agent`) |

## What is real vs. stood-in

**Real** (unmodified production code):
- `mthydra-ops bootstrap` → `init` DB + credential authority + `controller.toml`.
- EU exit + `descriptor-sign-now` (signed descriptor gen 1).
- `backup-now` → real boto3 PutObject under Object-Lock COMPLIANCE to MinIO.
- `vantage-add` / `vantage-attest-active`, `cover-add` / `cover-attest-verified`.
- `ru-bringup`: publishes the agent tarball + descriptor to S3, presigns them,
  and mints the cloud-init bundle via `provision-seed`.
- The RU agent downloads the agent closure + mtg ELF from S3 (presigned URLs),
  renders the mtg + sing-box configs, installs iptables, and brings up the
  tunnel on `:443`. Verbose UNREDACTED debug is enabled (`debug.flag`).
- The vantage and the controller each do a TLS handshake against the box.

**Stood in for locally** (can't run offline / without real accounts):
- AWS S3 → MinIO (real S3 API + object lock + presigned URLs, path-style).
- The GitHub mtg fetch in `image-prepare` → the cached release tarball staged
  via the real `S3Destination.put_image` + `ru_images.insert_candidate/promote`
  (`controller/stage_image.py`).

**Skipped** (irrelevant to the tunnel, need real accounts):
- The Telegram/email preflight sinks (placeholders in `controller.toml`).
- `vantage-setup`'s automated probe-runner SSH lockdown — `serve`'s probe
  runner is a stub in this build, so the vantage probes manually, exactly as
  the MVP quickstart §5 describes ("the vantage doesn't run any mthydra
  software").

## A note on the cloud-init shortcut

The real RU box runs the minted cloud-init via cloud-init/systemd-run. A bare
container has no systemd, so `rubox/boot.sh` replays the same bootcmd
(tmpfs/core_pattern hardening) and runcmd (fetch + verify the agent tarball,
launch `python3 -m mthydra.ru_agent`) steps directly. The seed.json it consumes
is the one the controller actually minted (extracted from the cloud-init bundle
by `controller/extract_seed.py`).

## Inspect / tear down

```
docker logs mt-rubox                      # full agent boot + tunnel log
docker exec -it mt-rubox ss -tlnp         # :443 listener (mtg)
docker exec mt-rubox sed -n 1,40p /run/mthydra/debug/agent-debug.log
docker rm -f mt-minio mt-controller mt-vantage mt-rubox && docker network rm mtnet
```

## Bug this harness caught

The published RU-agent tarball (`ops.agent_ops.package_agent`) shipped only the
`ru_agent` + `descriptor` subpackages, but the agent imports the top-level
modules `mthydra.debuglog` and `mthydra.proxy_link`. Every real RU box on
current `main` would have died on boot with `ImportError: cannot import name
'<mod>' from 'mthydra'`. Fixed by shipping top-level agent modules; locked with
`tests/unit/ops/test_agent_ops.py::test_package_agent_includes_top_level_modules`
and `::test_package_agent_real_closure_imports`.
