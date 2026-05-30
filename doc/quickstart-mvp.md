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
         "Effect": "Allow",
         "Action": [
           "s3:GetObject", "s3:PutObject", "s3:ListBucket",
           "s3:GetBucketLocation", "s3:GetObjectRetention"
         ],
         "Resource": [
           "arn:aws:s3:::mthydra-yourname-state",
           "arn:aws:s3:::mthydra-yourname-state/*"
         ]
       }
     ]
   }
   ```
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

The controller emails you a hourly heartbeat + a hard-fail alert when something is wrong. We use your existing mailbox via SMTP with an app password (not your real password).

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
sh /tmp/install.sh \
    --git-url https://github.com/KoRORland/mthydra.git \
    --config /tmp/install.ini --verbose
```

> **Note on `--git-url`**: recent versions of `install.sh` auto-read it from
> the `[install] git_url` line in your ini, so you can omit `--git-url` if you
> like. The explicit `--git-url` works either way and is safer if your local
> copy of `install.sh` is from before this fix.

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

Wait 5–10 minutes, then check your email — you should receive an automated **heartbeat** email from the controller. (One arrives every hour after that.)

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
sudo -u mthydra -i        # become the mthydra user
source /opt/mthydra/venv/bin/activate
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

# Part 7 — First RU box (20 min)

Now you can use the per-box bring-up wizard. It mints a provisioning seed, you boot a TimeWeb VM with that seed as cloud-init, you give the wizard the resulting IP, and it marks the box live.

### 7.1 Get the mtg release URL and SHA256

The RU box needs to download the `mtg` proxy binary at boot. For MVP, host it on your S3 bucket so the URL is signed and you control it:

1. Download the latest mtg release to your laptop: https://github.com/9seconds/mtg/releases — pick the `linux-amd64.tar.gz` (or `linux-arm64.tar.gz` if you'll use Arm RU boxes).
2. Compute the sha256: `sha256sum mtg-2.1.7-linux-amd64.tar.gz` — save this. Call it `MTG_SHA256`.
3. Upload to S3: AWS Console → your bucket → **Upload** → drop the file. Note the object key (e.g. `mtg-2.1.7-linux-amd64.tar.gz`).
4. Create a **presigned URL** so the RU box can download it without AWS credentials:
   ```bash
   # On your laptop with aws cli installed:
   aws s3 presign s3://mthydra-yourname-state/mtg-2.1.7-linux-amd64.tar.gz --expires-in 86400 --region eu-west-1
   ```
   Save the URL. Call it `AGENT_SOURCE_URL`.
5. Similarly, the RU box needs a descriptor-refresh URL — a small file in S3 that the controller updates whenever it signs a new descriptor. For MVP, point at `s3://your-bucket/descriptors/current` (the controller writes here automatically once running). Presign it:
   ```bash
   aws s3 presign s3://mthydra-yourname-state/descriptors/current --expires-in 2592000 --region eu-west-1
   ```
   30-day expiry. Save as `DESCRIPTOR_REFRESH_URL`.

### 7.2 Run the bring-up wizard

On the EU host as the mthydra user:
```bash
mthydra-ops ru-bringup \
    --provider timeweb --region ru-moscow-1 \
    --agent-source-url "<AGENT_SOURCE_URL>" \
    --agent-source-sha256 "<MTG_SHA256>" \
    --descriptor-refresh-url "<DESCRIPTOR_REFRESH_URL>"
```

The wizard:
1. Mints a provision-seed (assigns your cover domain, generates an onward credential, etc.) — outputs a `box_id` like `b-7f3a...`.
2. Writes a cloud-init file at `/tmp/ru-cloud-init-b-...yaml`.
3. Prints "Paste the cloud-init file as user-data in your provider's console, boot the VM, then come back with the public IP."
4. Prompts: `Public IP when VM is up (Ctrl-C to defer):`.

### 7.3 Boot the TimeWeb VM with that cloud-init

1. `cat /tmp/ru-cloud-init-*.yaml` — copy the entire content to your clipboard.
2. TimeWeb dashboard → **Cloud** → **Create server**:
   - Smallest plan
   - OS: **Ubuntu 24.04**
   - Region: **Moscow**
   - SSH key: optional (you won't SSH in)
   - **Cloud-init / user-data** field (might be hidden under "Advanced"): paste the cloud-init YAML.
3. Create. Wait ~2 minutes for boot + the agent to download the binary + start.
4. Copy the assigned **public IPv4**.

### 7.4 Give the wizard the IP

Paste the public IPv4 into the wizard prompt + press Enter. The wizard now:
- Connects to `<IP>:443` and attempts a TLS handshake (the proxy's Fake-TLS layer).
- On success: marks the box live in the controller's DB.
- Prints `done: box b-... live @ <IP>; CANARY — next: §3.4 soak (submit probe-record ...)`.

If the wizard times out: SSH into the TimeWeb console for the new VM (via TimeWeb's web console — the VM has no SSH key by default) and run `journalctl -u cloud-final -n 50 --no-pager` to see what cloud-init did. Most often, the issue is the VM having no outbound internet (check TimeWeb firewall) or the presigned URL having expired.

### 7.5 Record at least one probe from your vantage

The controller will fire a CRIT alert in 6 hours if no probe has been recorded for this box. From your vantage:
```bash
ssh root@<VANTAGE_IPv4> "openssl s_client -connect <RU_BOX_IP>:443 -servername www.cloudflare.com < /dev/null 2>&1 | head -5"
```
You should see a TLS handshake. Save the output / a one-line summary.

Back on the EU host as the mthydra user, record the probe:
```bash
mthydra-controller probe-record \
    --box-id <b-... from step 7.2> \
    --vantage ru-msk-1 \
    --check tls_fall_through \
    --status pass \
    --cycle-at "$(date -u -Iseconds | sed 's/+00:00/Z/')" \
    --evidence "openssl s_client to <RU_BOX_IP>:443 OK with cover sni www.cloudflare.com" \
    --db-path /var/lib/mthydra/state.sqlite
```

Repeat this once a day for the first week — eventually we'll automate it (the design has a probe-runner spec deferred for later).

---

# Part 8 — First user (10 min)

You're going to add yourself first as a smoke test, then your real circle members.

### 8.1 Add yourself as a test user

Open a DM with your **distribution** bot (from 2.3) in Telegram, send "hi". Then open `https://api.telegram.org/bot<DIST_BOT_TOKEN>/getUpdates` in a browser, find your chat ID. Save as `MY_CHAT_ID`.

On the EU host as mthydra user:
```bash
mthydra-controller user-add me \
    --out-of-band-channel "phone:+1234567890" \
    --display-name "Me (test)" \
    --db-path /var/lib/mthydra/state.sqlite

mthydra-controller user-channels-set me \
    --telegram <MY_CHAT_ID> \
    --email youremail@gmail.com \
    --db-path /var/lib/mthydra/state.sqlite

mthydra-controller shard-create s-test --members me \
    --db-path /var/lib/mthydra/state.sqlite \
    --config /etc/mthydra/controller.toml

mthydra-controller dist-test --user-id me \
    --db-path /var/lib/mthydra/state.sqlite \
    --config /etc/mthydra/controller.toml
```

You should now have **one test message in Telegram** (via the distribution bot, with a `tg://proxy?...` link) **and one in email**. Tap the Telegram link on your phone — Telegram opens a "Connect Proxy" dialog with the RU box's IP filled in. Tap **Connect**. Telegram is now routing through your RU box.

Verify it's working: turn off WiFi (force mobile data), open Telegram, send yourself a message. If it sends quickly, your proxy works.

### 8.2 Add a real user

For each person in your trusted circle:

1. Meet them in person or on a Signal/WhatsApp call. Confirm:
   - They have Telegram on a phone they actually use.
   - They have an email they read daily (not Yandex / not Mail.ru).
   - They understand: "if Telegram stops working for you, contact me on [Signal/etc] and I'll switch you."
2. Have them open a DM with your distribution bot and send "hi". Get their chat ID via `getUpdates` (you'll see a new chat entry).
3. Tell them to enable **Telegram Passcode Lock** (Settings → Privacy and Security → Passcode Lock) AND **Two-Step Verification** with a recovery email. Important — these protect them if their phone is grabbed at a border check.
4. Run:
   ```bash
   mthydra-controller user-add <their-name> \
       --out-of-band-channel "signal:<their phone>" \
       --display-name "Their Name" \
       --db-path /var/lib/mthydra/state.sqlite

   mthydra-controller user-channels-set <their-name> \
       --telegram <THEIR_CHAT_ID> \
       --email theiremail@gmail.com \
       --db-path /var/lib/mthydra/state.sqlite

   mthydra-controller shard-assign-box <their-name> --auto \
       --db-path /var/lib/mthydra/state.sqlite

   mthydra-controller dist-test --user-id <their-name> \
       --db-path /var/lib/mthydra/state.sqlite \
       --config /etc/mthydra/controller.toml
   ```
5. Ask them out-of-band (NOT in Telegram) whether the test message arrived in both channels. If yes — they're set up.

---

# Part 9 — Day-2 routine

The controller runs three automatic checks for you, set up by the installer:

| When | What runs | What it does |
|---|---|---|
| Daily at 06:17 UTC | `mthydra-ops daily-check` | Exits nonzero if any safety obligation is overdue. Failure shows in `journalctl`. |
| Weekly (Mon 07:00) | `mthydra-ops alert-summary` | Surfaces silent alert-delivery failures. |
| Monthly (1st 03:00) | `mthydra-ops monthly-compact` | Purges audit-log rows older than 30 days. |

You don't need to do anything to enable them. To see if they ran:
```bash
sudo systemctl list-timers 'mthydra-*'
```

### What you must do manually, ongoing

**Every day (~30 seconds):**
- Glance at your inbox. Did the hourly heartbeat arrive overnight? If yes, you're good. If you didn't see one in the last 2 hours — check `systemctl status mthydra-controller`.

**Weekly (~5 minutes):**
- Run one probe from your vantage against each live RU box (see step 7.5) and submit `probe-record`. Without this, the box silently shows "no recent probe coverage" and after 6h alerts fire.

**When the operator-alert Telegram bot pings you:**
- Read the message. The `dedupe_key` says which kind of problem. Most common at MVP scale:
  - `probe_coverage_pending::<box>` — you forgot to submit a probe-record. Do step 7.5.
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

1. SSH into EC2: `systemctl status mthydra-controller` — should be `active (running)`. If not: `systemctl start mthydra-controller`.
2. Force one heartbeat manually: `sudo -u mthydra /opt/mthydra/venv/bin/mthydra-controller obs-heartbeat-now --db-path /var/lib/mthydra/state.sqlite --config /etc/mthydra/controller.toml`. If this errors, the SMTP creds are stale — go to 2.4.

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

# What this MVP intentionally doesn't have

- A warm standby (run `install-standby` on a second EC2 instance whenever you're ready — it polls the active's heartbeat).
- A new-image promotion pipeline (`ru-image-cycle` exists for when you need to roll out a new mtg release with canary soaking — for MVP, the binary you uploaded in 7.1 is fine).
- Automated probe execution (you submit `probe-record` by hand from a vantage — there's a deferred spec for an automated probe runner).
- Multiple vantages / multiple cover domains (recommended for production: ≥2 of each, rotated every 14–30 days).

When you're ready to grow past MVP, the relevant runbook sections cover those motions. For now, **stop here**. A small private fleet that works reliably is better than a large one that drifts.

---

*If anything in this guide is wrong or out of date, that's a bug — please send a corrected version back.*
