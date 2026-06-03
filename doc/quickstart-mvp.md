# mthydra MVP Quickstart

A from-scratch step-by-step to get a working private proxy fleet running for the first time. Aimed at an operator with shell-and-browser comfort but no special networking or sysadmin background. Roughly **2–4 hours** of hands-on work the first time you do it.

## What you're building

- One **EU controller** on AWS EC2 (the brain — runs in Ireland/Frankfurt, signs everything, holds the database).
- One **S3 bucket** on AWS for encrypted backups (Standard tier — *not* Glacier).
- One **RU box** on TimeWeb (the proxy your circle actually connects to).
- One **probe vantage** on TimeWeb (a second small VPS that watches the RU box looks correct).
- A **Telegram proxy link** you hand out to your circle.

You can grow to more RU boxes / users / vantages later. This guide gets the smallest end-to-end thing working.

## Cost estimate (USD per month)

| What | Where | Tier | ~Cost |
|---|---|---|---|
| EU controller | AWS EC2 | t4g.small (Ireland/Frankfurt) | $12 |
| Backup bucket | AWS S3 | Standard, ~5GB | $0.15 |
| RU box | TimeWeb VPS | smallest cloud plan | $3–5 |
| Probe vantage | TimeWeb VPS | smallest cloud plan | $3–5 |
| Email | any | use your existing Gmail/Outlook (app password) | $0 |
| Telegram bots | Telegram | free | $0 |
| **Total** | | | **~$20/mo** |

## What you need before you start

Have these open in browser tabs / installed before step 1:

1. An **AWS account** with billing set up (you'll create EC2 + S3).
2. A **TimeWeb account** (https://timeweb.cloud) with billing — Russian-billable card or crypto.
3. A **Telegram account** on your phone.
4. An **email mailbox you check daily**, with **2-factor authentication enabled** (Gmail or Outlook). You'll create an "app password" later.
5. A **laptop** running Linux or macOS with `ssh`, `git`, and `age` (`brew install age` on macOS; `apt install age` on Linux/WSL).
6. About **2 hours uninterrupted**. The Telegram-bot + sink-verification step does not work well in pieces.

You do **not** need to know Python, Docker, or systemd. The installer handles all of that.

---

# Part 1 — AWS setup (15 min)

You'll create one IAM user with limited S3 permissions, one S3 bucket with backup protection, and one EC2 instance.

### 1.1 Create the S3 backup bucket

1. AWS Console → **S3** → **Create bucket**.
2. **Bucket name**: pick something globally unique, e.g. `mthydra-yourname-state`. Write this down — you'll need it later.
3. **AWS Region**: pick the same region you'll launch EC2 in. **Recommend `eu-west-1` (Ireland)** or `eu-central-1` (Frankfurt). Write this down too.
4. **Object Ownership**: leave as "Bucket owner enforced".
5. **Block Public Access**: keep all four boxes checked (default).
6. **Bucket Versioning**: **Enable**.
7. Scroll down — **Object Lock**: **Enable**. (You **must** do this at bucket creation; it cannot be turned on later.) Read and tick the acknowledgement.
8. Click **Create bucket**.

After creation, click the bucket → **Properties** → **Object Lock** → **Edit** the default retention:
- Mode: **Compliance**
- Retention period: **30 days**
- Save.

This makes backups un-deletable for 30 days even by you under coercion. Important.

> **Do not pick S3 Glacier or Glacier Deep Archive.** They have multi-hour retrieval times and will break restore. S3 Standard is correct. If you want to save money later, set up a Lifecycle Rule that moves objects to Glacier Instant Retrieval after 30 days (still millisecond reads, ~1/4 the cost) — but not for the first month.

### 1.2 Create the IAM user for the controller

1. AWS Console → **IAM** → **Users** → **Create user**.
2. **User name**: `mthydra-controller`.
3. Do **not** check "Provide user access to the AWS Management Console" — programmatic only.
4. **Next** → **Attach policies directly** → **Create policy**.
5. Click the **JSON** tab and paste this (replace `mthydra-yourname-state` with your bucket name from 1.1):
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "MthydraBucketLevel",
         "Effect": "Allow",
         "Action": [
           "s3:ListBucket",
           "s3:GetBucketObjectLockConfiguration"
         ],
         "Resource": "arn:aws:s3:::mthydra-yourname-state"
       },
       {
         "Sid": "MthydraObjectLevel",
         "Effect": "Allow",
         "Action": [
           "s3:PutObject",
           "s3:PutObjectRetention",
           "s3:PutObjectLegalHold",
           "s3:GetObject"
         ],
         "Resource": "arn:aws:s3:::mthydra-yourname-state/*"
       }
     ]
   }
   ```
   > **Why these specific actions?** The bucket uses Object Lock COMPLIANCE,
   > so every PutObject sets a retention timestamp — that requires
   > `s3:PutObjectRetention` (and `s3:PutObjectLegalHold`) in addition to
   > the obvious `s3:PutObject`. `s3:GetBucketObjectLockConfiguration`
   > lets boto3 confirm the bucket is lock-enabled before the first write.
   > There is no `s3:DeleteObject` because Object Lock COMPLIANCE forbids
   > deletion until retention expires anyway — granting it would be a lie.
   > If you copy a stale policy that lists `s3:GetObjectRetention` (read-only)
   > instead, the first `backup-now` will fail with `AccessDenied`.
6. **Next** → name it `mthydra-s3-rw` → **Create policy**.
7. Back on the user-creation tab, refresh, attach `mthydra-s3-rw`, **Next** → **Create user**.
8. Click the user → **Security credentials** → **Create access key** → **Other** → **Next** → **Create access key**.
9. **Copy both**:
   - **Access key ID** (looks like `AKIA...`)
   - **Secret access key** (long random string — shown ONCE, you cannot retrieve it later)

Paste both into a note you'll delete after the install (or use your password manager). The secret is what gives the controller permission to write backups.

### 1.3 Launch the EU controller EC2 instance

1. AWS Console → **EC2** → **Launch instance**.
2. **Name**: `mthydra-eu-1`.
3. **AMI**: search "Ubuntu" → pick **Ubuntu Server 24.04 LTS (HVM), SSD Volume Type** (free-tier eligible if you're new to AWS) — and select **64-bit (Arm)** architecture (cheaper, fine for our workload).
4. **Instance type**: **t4g.small** (2 vCPU / 2 GiB RAM, Arm). Plenty.
5. **Key pair**: **Create new key pair** → name it `mthydra-eu-1` → type **ED25519** → format **.pem** → **Create**. The browser downloads `mthydra-eu-1.pem`. Move it now to `~/.ssh/` on your laptop and `chmod 600 ~/.ssh/mthydra-eu-1.pem`.
6. **Network settings** → **Edit**:
   - Create new security group, name `mthydra-eu-1-sg`.
   - Inbound rule: **SSH** (port 22) from **My IP** (AWS auto-detects your laptop's IP). That's the only inbound rule.
7. **Configure storage**: 20 GiB gp3 (default is fine).
8. **Launch instance**.
9. Once it shows "Running", click the instance and copy the **Public IPv4 address** (and the **Public IPv4 DNS** like `ec2-XX-XX-XX-XX.eu-west-1.compute.amazonaws.com`).

Test SSH from your laptop:
```bash
ssh -i ~/.ssh/mthydra-eu-1.pem ubuntu@<PUBLIC_IPv4>
```
You should land in a shell. `exit` back to your laptop.

---

# Part 2 — Operator laptop setup (20 min)

These steps stay on your laptop — never on the EU host.

### 2.1 Generate the operator age key

This key encrypts every backup. It must **never** live on the EU host or anywhere remote. If you lose it, you lose the ability to restore.

```bash
mkdir -p ~/.config/mthydra
age-keygen -o ~/.config/mthydra/operator.age
chmod 600 ~/.config/mthydra/operator.age
grep '# public key:' ~/.config/mthydra/operator.age
```

The last line prints something like `# public key: age1abc...xyz`. **Copy the `age1...xyz` part** (without `# public key: `) — you'll paste it into the install config in a few minutes.

**Back the key up**:
1. Copy `~/.config/mthydra/operator.age` to a USB stick. Put the USB in a desk drawer.
2. Copy it to a second USB stick. Give it to a trusted friend in a different building, or put it in a safe-deposit box.

Do **not** put this file in Dropbox / iCloud / Google Drive. It's a single point of total failure.

### 2.2 Create the operator-alert Telegram bot

This bot pages you when something is wrong. It is **different** from the user-facing distribution bot you'll make in 2.3.

1. Open Telegram, search `@BotFather`, start chat.
2. Send `/newbot`.
3. Name: `mthydra-yourname-ops`.
4. Username: must end in `bot`, e.g. `mthydra_yourname_ops_bot`.
5. BotFather replies with **`HTTP API token`** — looks like `123456789:AAAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`. **Copy and save** as `OPS_BOT_TOKEN`.
6. Open a DM with the bot you just created (search its username in Telegram, tap it, hit "Start" / send "hi").
7. In your laptop browser, open `https://api.telegram.org/bot<OPS_BOT_TOKEN>/getUpdates` (paste the token after `bot`).
8. Find a JSON field `"chat":{"id":<NUMBER>` — that's your **chat ID**. **Copy and save** as `OPS_CHAT_ID`.

### 2.3 Create the user-distribution Telegram bot

This bot sends proxy links to your circle. It is a **separate** bot from 2.2 so a leak of one doesn't compromise the other.

Repeat 2.2 with a different name (e.g. `mthydra_yourname_dist_bot`). Save the token as `DIST_BOT_TOKEN`. You don't need a chat ID for this one (each user gives you their own when you onboard them).

### 2.4 Create an email "app password" for the controller

The controller emails you a **daily heartbeat** + a hard-fail alert when something is wrong. We use your existing mailbox via SMTP with an app password (not your real password). One email per day is quiet enough that you'll actually read it — the heartbeat body lists which obligations are due so you don't need to log in to know.

**Gmail:**
1. https://myaccount.google.com/security → 2-Step Verification must be ON.
2. https://myaccount.google.com/apppasswords → "App name": `mthydra-eu-1` → **Create**.
3. Copy the 16-character password. Save as `SMTP_PASSWORD`.
4. Save SMTP details: host=`smtp.gmail.com`, port=`587`, username=`youremail@gmail.com`.

**Outlook / Hotmail:**
1. https://account.microsoft.com/security → 2-step verification ON.
2. https://account.microsoft.com/security/additional-security → App passwords → create.
3. Save SMTP details: host=`smtp.office365.com`, port=`587`, username=`youremail@outlook.com`.

You'll reuse the same SMTP for the user distribution channel — that's fine.

---

# Part 3 — Install the EU controller (10 min)

Everything you collected above goes into one config file, then one command.

### 3.1 Build the install.ini on your laptop

On your laptop, create a file `~/install.ini` with this template — fill in every `<...>` placeholder:

```ini
[install]
git_url      = https://github.com/KoRORland/mthydra.git
git_ref      = main
src_dir      = /opt/mthydra/src
venv_dir     = /opt/mthydra/venv
scheduler    = systemd
assume_sinks = false

[node]
hostname = <EC2_PUBLIC_DNS>           ; from step 1.3, e.g. ec2-3-250-...amazonaws.com

[age]
recipient = <AGE_PUBLIC_KEY>          ; from step 2.1, the age1... part only

[backup]
; AWS S3 — endpoint is regional. Replace eu-west-1 with your region.
endpoint        = https://s3.eu-west-1.amazonaws.com
bucket          = mthydra-yourname-state    ; your bucket from 1.1
key_id          = <AWS_ACCESS_KEY_ID>       ; from 1.2, the AKIA... value
application_key =                            ; leave BLANK — passed via env var instead

[observability.telegram]
bot_token = <OPS_BOT_TOKEN>           ; from 2.2
chat_id   = <OPS_CHAT_ID>             ; from 2.2

[observability.email]
smtp_host = smtp.gmail.com            ; or smtp.office365.com
smtp_port = 587
from_addr = youremail@gmail.com
to_addr   = youremail@gmail.com       ; YOU — where alerts land
username  = youremail@gmail.com
password  = <SMTP_PASSWORD>           ; the 16-char app password from 2.4

[distribution.telegram]
bot_token = <DIST_BOT_TOKEN>          ; from 2.3 (DIFFERENT bot)

[distribution.email]
smtp_host = smtp.gmail.com
smtp_port = 587
from_addr = youremail@gmail.com
username  = youremail@gmail.com
password  = <SMTP_PASSWORD>
```

> **Why is `application_key` blank?** The AWS secret access key is a real secret — we pass it via an environment variable so it never lands in the install log or on any process command line. You'll set it in the next step.

### 3.2 Copy install files to the EC2 host

From your laptop:
```bash
scp -i ~/.ssh/mthydra-eu-1.pem ~/install.ini ubuntu@<EC2_PUBLIC_IPv4>:/tmp/
```

You also need `scripts/install.sh`. Either clone the repo and copy:
```bash
git clone https://github.com/KoRORland/mthydra.git /tmp/mthydra
scp -i ~/.ssh/mthydra-eu-1.pem /tmp/mthydra/scripts/install.sh ubuntu@<EC2_PUBLIC_IPv4>:/tmp/
```

Or just download the single file with curl on the EC2 host (next step).

### 3.3 Run the installer on the EC2 host

SSH into the host and become root:
```bash
ssh -i ~/.ssh/mthydra-eu-1.pem ubuntu@<EC2_PUBLIC_IPv4>
sudo -i              # become root
```

Set the AWS secret access key as an environment variable (this keeps it out of process listings and the install log):
```bash
export B2_APPLICATION_KEY='<AWS_SECRET_ACCESS_KEY>'         # from step 1.2
```

> **Note the variable name.** The installer was originally built for Backblaze B2 and the env var name is `B2_APPLICATION_KEY` regardless of whether you're using B2 or AWS S3. The value is just "a secret to pair with the access key id". Confusing but harmless.

Run the installer:
```bash
sh /tmp/install.sh --config /tmp/install.ini --verbose
```

The script will:
1. `apt update && apt install` Python 3.12, git, age, build tools — about 1 minute.
2. Clone the mthydra source to `/opt/mthydra/src` — about 10 seconds.
3. Build a Python venv at `/opt/mthydra/venv` — about 30 seconds.
4. Hand off to the Python orchestrator, which runs 9 phases:
   - `preconditions` (sanity checks)
   - `setup-host` (creates the `mthydra` user + `/etc/mthydra` + `/var/lib/mthydra`)
   - `verify-install` (confirms `mthydra-controller --help` works)
   - `bootstrap` (creates the SQLite DB, migrates the credential authority, writes `/etc/mthydra/controller.toml`)
   - `preflight` (sends a CRIT-severity test alert + a heartbeat email) — **stops here for your confirmation**
   - `service` (installs and starts the systemd service)
   - `first-descriptor` (signs the first endpoint descriptor)
   - `maintenance-timers` (sets up daily / weekly / monthly cron-equivalents)
   - `summary`

You'll see `[5/9] preflight` and a prompt:
```
Did the crit test arrive in BOTH Telegram AND email? [y/N]
```

**Do not type `y` until you have actually checked both:**
- Open the Telegram DM with your ops bot — there should be a new message containing "deploy-time crit test from <hostname>".
- Check your email inbox (and **spam folder** — if it's there, whitelist the From address before continuing).

Only when BOTH have arrived, type `y` and press Enter. If one or both are missing, type `n` — the installer aborts cleanly. Then:
- Re-check your Telegram bot token / chat ID / SMTP password in `~/install.ini`
- `scp` the corrected ini to `/tmp/install.ini` again
- Rerun `sh /tmp/install.sh --config /tmp/install.ini --verbose` — it's idempotent, skips what's done, re-runs preflight.

When you confirm the gate, the installer continues. After about 30 seconds you'll see the final summary:
```
done. Remaining OUT-OF-BAND steps:
  1. Confirm §1.8 sinks if you skipped the gate.
  2. Back up the operator age key to two non-cloud locations ...
  3. Stand up a warm standby ... [skip for MVP]
  4. RU image build and RU-node provisioning are SEPARATE automation ...
```

### 3.4 Confirm the first backup actually landed (1 min)

The installer ends with a forced `backup-now`. In your S3 bucket (AWS console → S3 → your-bucket-name) you should now see:
- `gen-0000000001.age` — the first encrypted state snapshot
- `index.json` — the manifest

If those are missing OR the installer reported `backup FAILED`, the cause is almost always one of:

- **`SignatureDoesNotMatch`** → the AWS secret access key is wrong (typo at install time). Rotate it: `mthydra-controller rotate-provider-credential b2 --credential-file /tmp/.b2-cred --db-path /var/lib/mthydra/state.sqlite` (file must be readable by the `mthydra` user — put it in `/tmp`, not `/root`).
- **`AccessDenied`** → the IAM policy is missing `s3:PutObjectRetention`. Recheck step 1.2; the JSON block must include `s3:PutObjectRetention`, `s3:PutObjectLegalHold`, and `s3:GetBucketObjectLockConfiguration`. The old quickstart's `s3:GetObjectRetention` (read-only) is the wrong action.
- **`NoSuchBucket`** → bucket name typo, or you created the bucket in a different region than the one in `install.ini`'s `b2_endpoint`.

Re-run the install (idempotent) after fixing.

---

# Part 4 — Verify it works (5 min)

Still on the EC2 host as root:

```bash
systemctl status mthydra-controller
```
Should show **`active (running)`** and a recent start time.

```bash
journalctl -u mthydra-controller -n 30 --no-pager
```
Should show a line like `serve: backup orchestrator + descriptor rotator + ... armed`.

Wait 5–10 minutes, then check your email — you should receive an automated **heartbeat** email from the controller. (One arrives every 24 hours after that. See the daily-check on §9.2 for the rhythm.)

If you see all three (systemd active, log line, heartbeat email), the EU controller is **live and self-monitoring**. Take a breath.

---

# Part 5 — First probe vantage (15 min)

Before you can safely provision an RU box, you need at least one **probe vantage** — a small VPS that pings the future RU box and confirms it looks legitimate (its TLS handshake matches the cover site, port :443 is the only thing open, etc.). The controller emits a CRIT alert if no probe has been recorded for any live RU box in the past 6 hours.

> For MVP, **one vantage is enough to get started** (the design recommends 2+ for production). You can add a second vantage later.

### 5.1 Provision a TimeWeb vantage VPS

1. TimeWeb dashboard → **Cloud** → **Create server**.
2. Smallest plan (`Apollo`, $3–4/mo).
3. OS: **Ubuntu 22.04**.
4. Region: pick **Moscow** or **St. Petersburg**.
5. SSH key: upload your `~/.ssh/mthydra-eu-1.pem.pub` (or generate a new key).
6. Create. After ~1 minute, copy the assigned IPv4.

SSH in, install age + curl + openssl (already there usually):
```bash
ssh root@<VANTAGE_IPv4>
apt update && apt install -y age curl openssl jq
exit
```

That's it — the vantage doesn't run any mthydra software. It's just a host from which you (the operator) execute probe commands manually.

### 5.2 Register the vantage with the EU controller

SSH back into the EU EC2 host as the `mthydra` user:
```bash
ssh -i ~/.ssh/mthydra-eu-1.pem ubuntu@<EC2_PUBLIC_IPv4>
sudo -u mthydra -i        # become the mthydra user — mthydra-controller and mthydra-ops are on PATH
```

Pick a short label for your vantage — e.g. `ru-msk-1`. Register it:
```bash
mthydra-controller vantage-add ru-msk-1 \
    --label ru-msk-1 \
    --source-kind cloud-cis \
    --region-hint "RU-moscow" \
    --notes "TimeWeb Moscow; provisioned $(date -u -I)" \
    --db-path /var/lib/mthydra/state.sqlite
```

Then confirm it sees what a real Russian user sees. From the vantage VPS, fetch a known-good public site:
```bash
ssh root@<VANTAGE_IPv4> 'openssl s_client -connect mail.ru:443 -servername mail.ru < /dev/null 2>&1 | head -20'
```
You should see a valid TLS handshake with a `*.mail.ru` certificate (this proves the vantage's egress is plausibly Russian). Save that output to a file or just note "OK".

Now attest the vantage active:
```bash
mthydra-controller vantage-attest-active ru-msk-1 \
    --evidence "openssl s_client to mail.ru shows expected cert chain $(date -u -I)" \
    --db-path /var/lib/mthydra/state.sqlite
```

---

# Part 6 — First cover domain (15 min)

A **cover domain** is the website name your RU box pretends to be hosting. To the outside (and to RKN's automated probes), the box looks like a fronting proxy for that domain. Pick one that:

- Is a **major CDN-fronted** site (Akamai, CloudFront, Fastly) — they're commonly unblocked in Russia and have lots of legitimate traffic.
- Is **unrelated to you** — never your employer's site, your school's, etc.
- **Resolves and works from Russia** (test from your vantage).

A few examples that have historically worked: `www.cloudflare.com`, `discord.com`, `assets.example-cdn.com`. Avoid anything political, anything blocked in Russia, and anything you have any organizational connection to.

### 6.1 Pick + test a candidate

From your vantage VPS, confirm the candidate behaves normally:
```bash
ssh root@<VANTAGE_IPv4> 'curl -sIL https://www.cloudflare.com | head -5'
ssh root@<VANTAGE_IPv4> 'openssl s_client -connect www.cloudflare.com:443 -servername www.cloudflare.com < /dev/null 2>&1 | grep "Verify return code"'
```
You want HTTP 2xx and `Verify return code: 0 (ok)`.

### 6.2 Register the candidate

Back on the EU host as the mthydra user:
```bash
mthydra-controller cover-add www.cloudflare.com \
    --notes "MVP candidate; tested from ru-msk-1 $(date -u -I)" \
    --db-path /var/lib/mthydra/state.sqlite
```

### 6.3 Attest it verified

```bash
mthydra-controller cover-attest-verified www.cloudflare.com \
    --vantage ru-msk-1 \
    --evidence "openssl s_client + curl -IL OK from ru-msk-1 $(date -u -I)" \
    --db-path /var/lib/mthydra/state.sqlite
```

Confirm the pool now has at least one verified cover:
```bash
mthydra-controller cover-pool-stats --json --db-path /var/lib/mthydra/state.sqlite
```
The `verified_count` field should be `1`.

---

# Part 7 — First RU box (15 min)

Spec P put all the controller-side busywork inside `ru-bringup`. It is **one command** — it ensures the mtg image is promoted, publishes the ru-agent tarball, publishes + presigns the descriptor-refresh URL, then mints the box. Your only RU-side touchpoint is pasting the cloud-init bundle into the TimeWeb console and reporting the public IP.

### 7.1 Bring up the RU box (one command)

On the EU host as the `mthydra` user:
```bash
mthydra-ops ru-bringup --provider timeweb --region ru-msk-1
```

That's it — no image-prepare, no agent-publish, no descriptor-publish-now, no presigned URL to copy. The wizard:
1. **Image** — ensures a promoted mtg image exists; if none, runs `image-prepare` automatically (resolves the latest `9seconds/mtg` release, downloads + sha-verifies, uploads to S3, promotes). Already promoted → skipped.
2. **Agent** — reads `/var/lib/mthydra/agent.json`; (re)publishes the `mthydra/ru_agent` + `mthydra/descriptor` tarball to S3 if missing or expiring within 24h.
3. **Descriptor** — publishes the latest signed descriptor to `s3://<bucket>/descriptors/current` and presigns it (you never paste the URL).
4. **Mint** — claims a cover domain + image + onward credential, requests a 24h image-download URL, writes a cloud-init bundle to `/tmp/ru-cloud-init-<box>.yaml` (mode 0600), and prompts: `Public IP when VM is up (Ctrl-C to defer):`.

Overrides, if you ever need them:
- Pin a specific image first with `mthydra-ops image-prepare --release v2.2.8 --arch linux-amd64 --yes`.
- Supply your own descriptor URL with `--descriptor-refresh-url '<url>'` (single-quote it — presigned URLs contain `&`).
- The individual commands (`image-prepare`, `agent-publish`, `descriptor-publish-now`) still exist for manual/cron use; `ru-bringup` just calls the same logic so you don't have to.

### 7.2 Boot the TimeWeb VM with that cloud-init

1. `cat /tmp/ru-cloud-init-*.yaml` — copy the entire content to your clipboard.
2. TimeWeb dashboard → **Cloud** → **Create server**:
   - Smallest plan.
   - OS: **Ubuntu 24.04**.
   - Region: **Moscow**.
   - SSH key: optional (you won't SSH in).
   - **Cloud-init / user-data** field (sometimes hidden under "Advanced"): paste the YAML.
3. Create. Wait ~2 minutes for boot + agent download + start.
4. Copy the assigned **public IPv4**.

### 7.3 Give the wizard the IP

Paste the IPv4 into the wizard prompt + Enter. The wizard:
- Connects to `<IP>:443` and attempts a TLS handshake.
- On success: marks the box live.
- Prints `done: box b-... live @ <IP>; CANARY — next: §3.4 soak ...` (the "soak" note now means "the probe runner will pick this box up automatically on its next tick" — no manual `probe-record` needed).

> **Shard placement:** the box auto-binds to `default_shard` at provisioning. Pass `provision-seed --shard <id>` to place it in a dedicated shard instead.

If the wizard times out, open the TimeWeb web console for the VM and run `journalctl -u cloud-final -n 80 --no-pager` to see what cloud-init did. Most often the VM has no outbound internet (check TimeWeb firewall) or the agent presigned URL expired (`mthydra-ops agent-publish` again to refresh).

### 7.4 Wire your vantage for automatic probes (one command)

The spec-P probe runner SSHes into each registered vantage every 30 minutes and runs `tls_fall_through` / `cover_domain_consistency` / `surface_scan` for every live box. To get a vantage onto that loop, you need (a) a probe SSH key on the EU host, (b) a `probe` user with that key on the vantage, (c) `openssl` + `ncat` on the vantage, (d) the vantage's host key pinned in known_hosts, (e) the SSH config registered with the controller. **One command does all five:**

```bash
sudo -u mthydra mthydra-ops vantage-setup \
    --vantage-id ru-msk-1 \
    --vantage-host <VANTAGE_IPv4> \
    --root-key /root/.ssh/timeweb-root.pem
```

(The `--root-key` is the SSH key with root access on the vantage VPS — the one TimeWeb gave you, or your hand-injected key. It's used once during setup and not stored.)

The wizard:
1. Ensures `/var/lib/mthydra/ssh` exists (0700).
2. Generates an ed25519 keypair at `/var/lib/mthydra/ssh/ru-msk-1.key` if absent.
3. SSHes to the vantage as root in a single session:
   - creates user `probe` (idempotent)
   - installs the EU-side pubkey to `/home/probe/.ssh/authorized_keys`
   - `apt-get install -y openssl ncat`
4. `ssh-keyscan` the vantage, append to `known_hosts`.
5. Calls `mthydra-controller vantage-set-ssh ru-msk-1 ...`.

Within 30 minutes (or restart `mthydra-controller` to force the next tick), `mthydra-controller probe-due --json` should show recent probes for your box. From this point forward, `probe_coverage_pending` will stay green automatically.

> **Older quickstart had 7 manual commands across two hosts** for this step. The wizard collapses them. Run with `--ssh-dir <path>` if you keep your probe-runner keys somewhere other than `/var/lib/mthydra/ssh/`.

---

# Part 8 — First user (10 min)

You're going to add yourself first as a smoke test, then your real circle members.

### 8.1 Add yourself as a test user

On the EU host as the `mthydra` user:

```bash
mthydra-controller user-onboard me \
    --display-name "Me (test)" \
    --email youremail@gmail.com \
    --db-path /var/lib/mthydra/state.sqlite \
    --config /etc/mthydra/controller.toml
```

Copy the printed `https://t.me/<bot>?start=…` link, open it on your phone, tap **Start**. Within ~30 seconds the controller captures your Telegram chat and sends your first proxy delta to Telegram + email.

Verify it's working: turn off WiFi (force mobile data), open Telegram, send yourself a message. If it sends quickly, your proxy works.

### 8.2 Add a real user

For each person in your trusted circle:

1. Meet them in person or on a Signal/WhatsApp call. Confirm:
   - They have Telegram on a phone they actually use.
   - They have an email they read daily (not Yandex / not Mail.ru).
   - They understand: "if Telegram stops working for you, contact me on [Signal/etc] and I'll switch you."
2. Tell them to enable **Telegram Passcode Lock** (Settings → Privacy and Security → Passcode Lock) AND **Two-Step Verification** with a recovery email. Important — these protect them if their phone is grabbed at a border check.
3. Run:
   ```bash
   mthydra-controller user-onboard <their-name> \
       --display-name "Their Name" \
       --email theiremail@gmail.com \
       --out-of-band-channel "signal:<their phone>" \
       --db-path /var/lib/mthydra/state.sqlite \
       --config /etc/mthydra/controller.toml
   # add --shard s-hi-risk to isolate a higher-risk contact in their own shard
   ```
   Send them the printed link out-of-band (Signal/in person). They tap it, tap **Start** — that's their whole job.
4. Ask them out-of-band (NOT in Telegram) whether the proxy config arrived. If yes — they're set up.

---

# Part 9 — Day-2 routine

### 9.1 What the controller does automatically (you don't have to)

`serve` arms a set of background sweeps. Knowing they exist saves operator time — when you see a related alert, you know what was watching for the problem.

| Sweep | Cadence | What it does |
|---|---|---|
| **descriptor rotator** | hourly | re-signs the endpoint descriptor; calls `descriptor-publish-now` on next sweep tick |
| **cover-pool TTL reverify** | hourly | downgrades `candidate_verified` rows past TTL → `candidate_unverified` |
| **cover-pool auto-reverify** | hourly | TLS-handshake smell test on every `candidate_verified` + `in_use` domain; on drift, **auto-burns** the domain (if pool has slack above `freeze_threshold`) or raises `cover_pool_reverify_drift_pending::<domain>` (if not) |
| **cover-pool rotation** | hourly | flags domains past `rotation_ttl_days` for operator-driven rotation |
| **shard reshuffle wheel** | hourly | reshuffles shards past their TTL; failed per-shard attempts land as `shard_overdue_pending::<sid>` with the exception in details_json |
| **probe runner** | 30 min | SSHes into each vantage, runs `tls_fall_through` / `cover_domain_consistency` / `surface_scan` for every live box. Per-vantage pre-flight: a dead vantage = one `probe_vantage_unreachable::<vantage>` alert, not N box-level soft_fails |
| **probe audit wheel** | 5 min | detects probe coverage gaps + raises `probe_kill_pending` on N-of-M soft fails |
| **obs alerter** | 2 min | turns overdue obligations / anti-obligations into Telegram + email alerts |
| **obs heartbeat** | daily | dead-man's-switch email; subject = `mthydra heartbeat @ ... — <host> v<version>`; body enumerates any overdue obligations + the remediation step for each (W-3); on N consecutive failures, raises `obs_dead_mans_switch_breach` with SMTP-smoke verdict + recent error strings in details (U-D4) |
| **backup orchestrator** | continuous | takes encrypted state snapshot every 24 h floor / on-change debounce |
| **backup integrity smoke** | weekly | downloads a random recent gen from S3, re-hashes, compares to recorded sha256; catches silent corruption nothing else surfaces |
| **standby heartbeat poller** | 5 min | pulls the active's heartbeat from S3 (only when this node is `--role standby`) |
| **upstream release tracker** | 7 days | polls GitHub for new mtg releases; flags `t4_upstream_release_available::<tag>` |
| **dist publisher** | 5 min | per-user delta publish (spec K) |
| **dist user heartbeat** | 24 h | per-user dead-man's-switch (spec K) |

Plus the three systemd-timer-driven jobs the installer wires:

| When | What runs |
|---|---|
| Daily at 06:17 UTC | `mthydra-ops daily-check` — exits nonzero if any safety obligation is overdue; failure shows in `journalctl` |
| Weekly (Mon 07:00) | `mthydra-ops alert-summary` — surfaces silent alert-delivery failures |
| Monthly (1st 03:00) | `mthydra-ops monthly-compact` — purges audit-log rows older than 30 days |

To confirm the timers and the sweeps are alive:
```bash
sudo systemctl list-timers 'mthydra-*'
journalctl -u mthydra-controller -n 5 --no-pager | grep "armed"
```

### 9.2 What you must do manually, ongoing

**Every day (~30 seconds):**
- Glance at your inbox. Did the daily heartbeat arrive? If yes, you're good — the body tells you whether anything is overdue and what to do. The subject identifies the running version + host: `mthydra heartbeat @ 2026-06-01T07:00:00Z — eu-1 v0.0.6`. If you didn't see one in the last 36h — `systemctl status mthydra-controller`.

**Weekly (~2 minutes):**
- Open the latest `mthydra-ops daily-check` log: `journalctl -u mthydra-daily-check.service -n 100 --no-pager`. Look for any `overdue` or `anti_obligation` lines. The automations in §9.1 should keep them empty in normal operation — recurring entries mean something is wedged.

**Ad-hoc, when you want to verify by hand:**
- `mthydra-controller cover-reverify-now --db-path /var/lib/mthydra/state.sqlite --config /etc/mthydra/controller.toml` — run the cover-domain smell test immediately instead of waiting for the next hourly tick. Prints `PASS`/`FAIL`/`BURN` per domain.
- `mthydra-controller backup-integrity-now --db-path /var/lib/mthydra/state.sqlite --config /etc/mthydra/controller.toml` — re-hash a random recent backup gen now instead of waiting for the weekly tick. Add `--generation N` to test a specific gen (e.g. after fixing an earlier `backup_integrity_failed` alert).
- `sudo -u mthydra -i -c 'mthydra-ops daily-check --db-path /var/lib/mthydra/state.sqlite'` — same JSON snapshot the daily timer runs, on demand.

**When the operator-alert Telegram bot pings you:**
- Read the message. The `dedupe_key` says which kind of problem. Common alerts and what they mean:
  - `obs_dead_mans_switch_breach` — heartbeat email hasn't gone out in N attempts. The alert body now carries the SMTP smoke verdict (host:port reachable? EHLO response?) and the last 3 distinct error strings. Operator usually fixes by rotating the email app password (see §2.4) and the next tick clears it.
  - `cover_pool_reverify_drift_pending::<domain>` — auto-reverify detected drift AND the pool is too tight to auto-burn, OR the drift is on an `in_use` domain. For pool-tight: add another cover domain (Part 6 with a fresh candidate). For in_use drift: investigate; if the domain is unfit, the box using it needs replacement via `ru-bringup` + `ru-box-terminate`.
  - `backup_integrity_failed::<generation>` — V-2 sweep found a sha256 mismatch on a stored gen. **Take seriously**: download the gen via `aws s3 cp s3://<bucket>/gen-NNNNNNNNNN.age -`, re-hash, confirm against the controller's recorded sha256 (`sqlite3 /var/lib/mthydra/state.sqlite "SELECT sha256 FROM backup_log WHERE generation=N"`). If the mismatch is real, do NOT trust later restore from that gen; force `backup-now` to produce a fresh one.
  - `credential_rotation_proven::<provider>` overdue — V-3 reminder. Mint a new credential at the provider (Gmail app password, AWS access key, B2 app key), then `mthydra-controller rotate-provider-credential <provider> --credential-file /tmp/.cred --db-path /var/lib/mthydra/state.sqlite`. The reminder resets after rotation.
  - `probe_vantage_unreachable::<vantage>` — controller couldn't SSH to the vantage at the last pre-flight. Check that the vantage VPS is up and the SSH key still works; the next pre-flight that succeeds clears the alert.
  - `probe_coverage_pending::<box>` — probes failing from ALL active vantages for this box (rare; the per-vantage failover absorbs single-vantage issues). The box may genuinely be down or behaving badly.
  - `cover_pool_rotation_frozen` — your cover pool has too few verified domains. Add and attest another one (Part 6 with a new domain).
  - Anything containing `probe_kill_pending` — the box's probe results look bad. Take it seriously: read the alert, decide whether the box is compromised, run `mthydra-controller ru-box-terminate <box> --reason compromise` if so.

---

# When things go wrong

### The installer's preflight gate failed (Telegram or email didn't arrive)

Most common causes, in order:
- **Email in spam folder**: whitelist the From address, re-run preflight.
- **SMTP password expired / wrong app password**: Gmail invalidates app passwords every ~90 days. Make a new one in 2.4, update `install.ini`, re-scp, re-run.
- **Wrong Telegram bot token**: BotFather can show it again — `/mybots` → pick → API Token.
- **Wrong chat ID**: rerun the `getUpdates` URL after sending a fresh "hi" to the bot.

### Heartbeat emails stop arriving

1. **Check the obs_dead_mans_switch_breach details first**: after 3 consecutive failures the controller raises this anti-obligation with a self-diagnosis embedded — SMTP smoke verdict (host:port reachable? EHLO succeeded?) plus the last 3 distinct error strings from the alert log. Triage from the alert body before SSHing:
   ```bash
   sqlite3 /var/lib/mthydra/state.sqlite \
       "SELECT details FROM obligation_clocks WHERE obligation_id='obs_dead_mans_switch_breach';" | jq .
   ```
2. SSH into EC2: `systemctl status mthydra-controller` — should be `active (running)`. If not: `systemctl start mthydra-controller`.
3. Force one heartbeat manually: `sudo -u mthydra -i -c 'mthydra-controller obs-heartbeat-now --db-path /var/lib/mthydra/state.sqlite --config /etc/mthydra/controller.toml'`. If this errors, the SMTP creds are stale — go to 2.4.

### Backup integrity alert (`backup_integrity_failed::<gen>`)

The V-2 weekly sweep re-hashes a random recent backup blob from S3 and compares to the sha256 we recorded at write time. A mismatch alert means one of:
- Silent S3 corruption / bit-rot — rare but real, especially after multi-month storage.
- The bucket got pointed at the wrong place (config drift across hosts).
- Someone (or something) mutated the blob after upload.

Verify by hand:
```bash
# What the controller recorded at write time:
sqlite3 /var/lib/mthydra/state.sqlite \
    "SELECT sha256, size_bytes FROM backup_log WHERE generation=<N>;"
# What's actually in the bucket right now:
aws s3 cp s3://<your-bucket>/gen-<NNNNNNNNNN>.age - | sha256sum
```
If the on-disk hash matches what `backup_log` said, the alert was transient (re-run `backup-integrity-now --generation <N>` to clear). If they differ, force a fresh `backup-now` and consider what could have mutated the prior gen.

### Telegram users can't connect through the proxy

1. Test from your vantage that the RU box accepts connections:
   ```bash
   ssh root@<VANTAGE_IPv4> "openssl s_client -connect <RU_BOX_IP>:443 -servername www.cloudflare.com < /dev/null 2>&1 | grep 'Verify\|Cipher'"
   ```
   Expect a successful handshake.
2. If that fails, the RU VM is probably down. TimeWeb dashboard → check VM status, reboot if needed. If it doesn't come back, terminate it and run `ru-bringup` again for a fresh one.
3. If the handshake works but the user still can't connect, ask them to **toggle the proxy off and on** in Telegram Settings → Data and Storage → Proxy.

### The EU controller's disk fills up

`du -sh /var/log/mthydra/` — if it's >1GB, run `mthydra-ops monthly-compact --no-dry-run --evidence "manual cleanup"` to purge old audit/probe rows.

### You've lost the EU host entirely (compromise, AWS account locked, etc.)

You have backups in S3, encrypted with your age key:
1. Launch a new EC2 instance (Part 1.3).
2. Run the installer (Part 3) — but instead of `install`, run `install-standby --promote --case B`.
3. After it promotes, rotate the credential authority + the descriptor signing key (the installer prints the exact commands).
4. Existing RU boxes keep working until they age out, then are replaced via `ru-bringup` from the new active.

---

# Part 10 — Upgrading mthydra

When a new version is tagged on GitHub:

```bash
# On the EU host — as root or as the mthydra user:
sudo -u mthydra -i -c 'mthydra-ops upgrade'
```

That single command runs the full 8-phase upgrade:
1. Preflight (sanity-check the source tree + read current HEAD)
2. Resolve target ref (latest tag; override with `--ref vX.Y.Z` for a specific version)
3. Forced `backup-now` — pre-upgrade recovery floor
4. `git fetch` + `git reset --hard` to the target ref
5. `pip install -e .` (rebuilds the venv against the new source)
6. Stop `mthydra-controller`
7. Start + verify (`systemctl is-active` + `startup-check` + `obs-heartbeat-now`)
8. Summary (prints old → new version + the backup generation as the recovery target)

**If health-check fails after restart**, the tool automatically rolls back to
the prior SHA and re-verifies. Disable that with `--no-auto-rollback` if you
want to investigate the failed state in place.

**If the upgrade crosses a SCHEMA_VERSION boundary** (the controller's
on-disk DB needs migrating to a newer version), the tool refuses to proceed
unless you pass `--allow-schema-migration`. The acknowledgement matters
because schema migrations are forward-only — if a post-migration verify fails,
auto-rollback can restore the old code, but the DB is permanently advanced.
The pre-upgrade backup (phase 3) is your recovery floor for that case.

**Confirm the upgrade landed:**
- Watch for the heartbeat email arriving within an hour. The subject now
  identifies the running version, e.g. `mthydra heartbeat @ ... — eu-host v0.0.4`.
- `mthydra-controller obs-status --json | jq '.summary_line'` shows no
  overdue obligations.

**Tag-shipping repos without GitHub Releases** (default behaviour for this
project): `mthydra-ops upgrade` with no `--ref` works from 0.0.4 onwards —
it falls back to `git ls-remote --tags` automatically. On 0.0.2/0.0.3 you
need `--ref vX.Y.Z` explicitly.

See **runbook §12** for the manual upgrade procedure (used to bootstrap 0.0.1 →
0.0.2, or when you need to recover from a broken upgrade by hand) and the
pre-0.0.4 host migration steps (chown + polkit rule).

---

# What this MVP intentionally doesn't have

- A warm standby (run `install-standby` on a second EC2 instance whenever you're ready — it polls the active's heartbeat).
- A real captured profile per image (`image-prepare --yes` writes a minimal placeholder; capture and pin a real one after the first canary soaks if you intend to rely on the harder probe verdicts).
- The two operator-driven `mtg`-release-cycle probers (`valid_path_liveness`, `latency_loss`, `behavioural_identity`) — the three MVP probers (`tls_fall_through`, `cover_domain_consistency`, `surface_scan`) run automatically; the other three need a circle-member relay and stay manual.
- Multiple vantages / multiple cover domains (recommended for production: ≥2 of each, rotated every 14–30 days).

When you're ready to grow past MVP, the relevant runbook sections cover those motions. For now, **stop here**. A small private fleet that works reliably is better than a large one that drifts.

---

*If anything in this guide is wrong or out of date, that's a bug — please send a corrected version back.*
