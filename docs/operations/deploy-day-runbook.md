# Deploy-day runbook — the Gated demo

The checklist for standing up the FDI **Gated demo** (see `CONTEXT.md`) on a fresh VM, from an
empty cloud account to a passing go-live smoke test. Follow it top to bottom; every step is a
command to run or a box to tick. You need: this repository, a browser, and a terminal.

What gets deployed is fixed by the repo — `docker/compose.prod.yml` (topology), `docker/Caddyfile`
(the only public surface), ADR-0001 (archived MinIO, mirrored) and ADR-0002 (the JWT gate). This
runbook does not change any of it; if a step here disagrees with those files, the files win and
this runbook has a bug.

Conventions used below:

- `DOMAIN` — the public hostname you buy in §2 (e.g. `fdi.example.com`).
- `VM_IP` — the VM's public IPv4 address.
- **Dev box** — the machine that holds today's corpus (dev compose stack). **VM** — the new host.
- On the VM, everything runs from the clone at `/opt/fdi`, as root (via `sudo`), because `.env`
  is root-owned (§3). Define this once per shell:

  ```bash
  cd /opt/fdi
  dc() { sudo docker compose -f docker/compose.prod.yml --env-file .env --profile graph "$@"; }
  ```

---

## 0. Before deploy day

- [ ] The commit you will deploy is merged to `main` and CI is green on it, **including the image
      publish jobs** (T9). Note its short SHA — CI tags images `sha-<short>` and `latest`.
- [ ] Both images exist for that tag (run anywhere logged in to GHCR, see §4):
      ```bash
      docker manifest inspect ghcr.io/jaimin-ptl07/fdi-backend:sha-<short>
      docker manifest inspect ghcr.io/jaimin-ptl07/fdi-frontend:sha-<short>
      ```
- [ ] You have, ready to paste: Azure Document Intelligence endpoint/key, Azure OpenAI
      endpoint/key/deployment, an optional Sentry DSN, and the email addresses of the Invitees.
- [ ] Pick one **Public filing** PDF that is **not** in the dev corpus, for the fresh-document
      validation (§8). Public filings only — the Gated demo has no handling for private documents.

---

## 1. VM: size, vendor, provision

### Sizing constraint (why ~16 GB, and why not Azure)

The demo ships the **full stack**, graph/vector tier included — it is the headline. The memory
ceilings in `docker/compose.prod.yml` add up to:

| Service | mem limit | Service | mem limit |
|---|---|---|---|
| worker | 3 G | neo4j | 2 G |
| milvus | 3 G | postgres | 1 G |
| api (BGE in-process) | 2 G | minio, rabbitmq, frontend | 512 M each |
| redis, etcd, edge | 256 M each | **total** | **≈ 13.3 G** |

Plus the OS and Docker itself ⇒ **16 GB RAM is the floor** for the full stack. Budget is
**≤ $30/month**.

| Option | RAM | Approx. monthly | Verdict |
|---|---|---|---|
| Azure B2s | 4 GB | ~$30 | **Too small** — cannot hold even Milvus + worker + api. |
| Azure B4ms / D4s-class | 16 GB | ~$120+ | Fits, but 4× the budget. |
| **Hetzner-class x86 (e.g. CX42 / CPX41 tier)** | 16 GB, 8 vCPU | ~€15–30 | **Chosen class.** Fits RAM and budget. |
| Hetzner ARM (CAX31) | 16 GB | cheaper | **Not usable**: the mirrored MinIO image (ADR-0001) is `amd64` only. |

The OCR and LLM work stays on Azure (Document Intelligence + OpenAI) regardless of where the VM
lives — that is a network call, not a reason to host on Azure. Prices drift; re-check on the day,
but the constraint is the table above: **x86_64, ≥ 16 GB RAM, ≥ 80 GB disk, ≤ $30**.

### Provision

- [ ] Create the VM: **Ubuntu 24.04 LTS, x86_64, 16 GB RAM, ≥ 80 GB disk**, your SSH key, a
      region near the audience. Note `VM_IP`.
- [ ] Cloud firewall (the provider's, not `ufw` — Docker's published ports bypass `ufw`):
      inbound **22/tcp** (ideally your IP only), **80/tcp**, **443/tcp**, **443/udp**. Nothing else.
- [ ] SSH in and install Docker Engine + the compose plugin:
      ```bash
      sudo apt-get update && sudo apt-get -y upgrade
      curl -fsSL https://get.docker.com | sudo sh
      sudo docker compose version   # must print v2.24+ (compose.prod.yml uses !reset)
      ```
- [ ] Nothing else gets installed on the VM: every tool below runs inside a stack container
      (`pg_restore` in `postgres`, `mc` in the mirrored MinIO image, `fdi-admin` / `fdi-mirror`
      in `api`).
- [ ] Clone the repo at the deploy commit:
      ```bash
      sudo git clone https://github.com/jaimin-ptl07/FinancialDocumentIntelligence.git /opt/fdi
      cd /opt/fdi && sudo git checkout <short-sha>
      ```

---

## 2. Domain and DNS

- [ ] Buy the domain (any registrar). Decide the hostname: `DOMAIN`.
- [ ] Create DNS records: `A DOMAIN → VM_IP` (and `AAAA` if the VM has IPv6 and the firewall
      allows it). TTL 300 for deploy day.
- [ ] Wait until it resolves from outside **before** first boot — Caddy requests the certificate
      on start, and repeated failures hit the CA's rate limits:
      ```bash
      dig +short DOMAIN        # must print VM_IP
      ```

---

## 3. Secrets: `.env` placement

One `.env`, on the host, read by Compose at runtime, never baked into an image, never committed.

- [ ] Create it from the template and lock it down:
      ```bash
      cd /opt/fdi
      sudo cp .env.example .env
      sudo chown root:root .env && sudo chmod 600 .env
      sudo ls -l .env           # -rw------- 1 root root
      sudoedit .env
      ```
- [ ] Set, in `.env` (everything else may keep the template value):

  | Variable | Production value |
  |---|---|
  | `EDGE_DOMAIN` | `DOMAIN` |
  | `IMAGE_TAG` | `sha-<short>` from §0 (pin it; do not deploy `latest`) |
  | `POSTGRES_PASSWORD` | new random value; **same** value inside `FDI_DATABASE_URL` |
  | `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | new random values; **same** values in `FDI_S3_ACCESS_KEY` / `FDI_S3_SECRET_KEY` |
  | `RABBITMQ_DEFAULT_USER` / `RABBITMQ_DEFAULT_PASS` | new random values; **same** values inside `FDI_BROKER_URL` |
  | `FDI_NEO4J_PASSWORD` | new random value (bootstraps Neo4j on first boot — set it **before** the first `up`) |
  | `FDI_AUTH_SECRET` | `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
  | `FDI_AZURE_DI_ENDPOINT` / `FDI_AZURE_DI_KEY` | from Azure |
  | `FDI_GPT_ENDPOINT` / `FDI_GPT_KEY` / `FDI_GPT_DEPLOYMENT` / `FDI_GPT_MODEL` | from Azure |
  | `FDI_SENTRY_DSN` | the Sentry project DSN (optional; blank = off) |
  | `COMPOSE_DATA_ROOT` | leave as is — production uses plain named volumes and ignores it (deleting it only adds warnings) |

  Generate random values with `openssl rand -hex 24`. Use hex (no `@ : / %`) so they can sit
  inside the connection URLs without escaping. `FDI_S3_PUBLIC_ENDPOINT`, `FDI_S3_PUBLIC_SECURE`,
  `FDI_AUTH_COOKIE_PATH` and `FDI_CORS_ORIGINS` are pinned by `compose.prod.yml` — what `.env` says
  for them is overridden.
- [ ] Validate the file renders (catches typos and missing required vars without starting anything):
      ```bash
      dc config --quiet && echo OK
      ```
      `OK` is the pass signal. Warnings that an optional variable "is not set" are expected;
      an error (e.g. `EDGE_DOMAIN is required`) is not.

---

## 4. Registry login and image pull

The backend, frontend and mirrored MinIO images live in GHCR under `jaimin-ptl07`. If the
packages are private, the VM needs a read-only token.

- [ ] On GitHub: *Settings → Developer settings → Personal access tokens (classic)* → a token with
      **only** `read:packages`, expiry ≈ the demo's lifetime.
- [ ] Log in on the VM (root's Docker config, since compose runs as root) and pull:
      ```bash
      echo '<token>' | sudo docker login ghcr.io -u jaimin-ptl07 --password-stdin
      dc pull
      ```
      `dc pull` must finish without errors for every service. If a backend/frontend tag is
      missing, CI has not published that commit — fix that (§0); building on the VM
      (`dc build`) works but is slow and is not the plan.

---

## 5. Data mirror

The demo ships the dev corpus instead of re-paying Azure to re-extract it. Three parts:
**database dump**, **object-store sync**, then **rebuild the graph/vector tier** from what was
copied (Milvus and Neo4j are not copied — they are derived). Finish with the **consistency check**.

### 5a. Dev box: quiesce and export

Run from the repo root on the dev box, with the dev stack up. (`dcd` = the dev compose; drop the
override file if you do not use one.)

```bash
dcd() { docker compose -f docker/compose.yml -f docker/compose.override.yml --env-file .env "$@"; }
mkdir -p mirror/artifacts
```

- [ ] Stop the worker so no stage is mid-write while you copy:
      ```bash
      dcd stop worker
      ```
- [ ] Make sure the dev `api` runs this commit's image (`fdi-mirror` ships with it):
      `dcd up -d --build api`.
- [ ] Confirm the dev corpus is itself consistent — a gap here would only be copied across:
      ```bash
      dcd exec api fdi-mirror check        # must end "0 missing"
      ```
- [ ] Dump Postgres (custom format):
      ```bash
      dcd exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > mirror/fdi.dump
      ```
- [ ] Export the artifact bucket with `mc`, which ships inside the mirrored MinIO image. The bucket
      is `FDI_S3_BUCKET` (default `artifacts`). Only that bucket — Milvus's own bucket is rebuilt,
      not copied. (On Windows run this from Git Bash with `MSYS_NO_PATHCONV=1` prefixed, or from
      PowerShell with `${PWD}`.)
      ```bash
      MINIO_IMAGE=$(dcd config --images | grep /minio:)
      docker run --rm --network fdi_default --env-file .env \
        -v "$PWD/mirror/artifacts:/export" --entrypoint sh "$MINIO_IMAGE" -c \
        'mc alias set src http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" &&
         mc mirror --overwrite "src/$FDI_S3_BUCKET" /export'
      ```
- [ ] Note the object count for later: `find mirror/artifacts -type f | wc -l`.
- [ ] Restart the dev worker if you still need it: `dcd start worker`.
- [ ] Copy to the VM (the bundle contains filings and extracted data; keep it off shared storage):
      ```bash
      tar -czf fdi-mirror.tgz -C mirror .
      scp fdi-mirror.tgz <ssh-user>@VM_IP:/tmp/      # Hetzner's default user is root; Ubuntu cloud images use `ubuntu`
      ```

### 5b. VM: import

- [ ] Unpack and start **only** the stores:
      ```bash
      cd /opt/fdi
      sudo mv /tmp/fdi-mirror.tgz . && sudo mkdir -p mirror && sudo tar -xzf fdi-mirror.tgz -C mirror
      dc up -d postgres minio
      ```
- [ ] Restore the database into the fresh Postgres (`--no-owner`: the VM's DB user differs from
      dev's). Then let the one-shot `migrate` apply anything newer than the dump:
      ```bash
      dc exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' < mirror/fdi.dump
      dc run --rm migrate
      ```
- [ ] Import the artifact bucket (MinIO publishes no port, so `mc` joins the compose network):
      ```bash
      MINIO_IMAGE=$(dc config --images | grep /minio:)
      sudo docker run --rm --network fdi_default --env-file .env \
        -v "/opt/fdi/mirror/artifacts:/import:ro" --entrypoint sh "$MINIO_IMAGE" -c \
        'mc alias set dst http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" &&
         mc mb --ignore-existing "dst/$FDI_S3_BUCKET" &&
         mc mirror --overwrite /import "dst/$FDI_S3_BUCKET" &&
         mc ls --recursive "dst/$FDI_S3_BUCKET" | wc -l'
      ```
      The printed count must equal the dev count from 5a.
- [ ] Bring up the rest of the stack:
      ```bash
      dc up -d
      dc ps          # every service running/healthy; `migrate` exited 0
      ```

### 5c. Stage-run / artifact consistency check

Every `done` stage-run row names the object its stage wrote; every document's source PDF sits at
its checksum key. This verifies every one of those objects made it across:

- [ ] ```bash
      dc exec api fdi-mirror check
      ```
      Must end `0 missing` (exit 0). Any `MISSING <doc_id> <stage|source> <key>` line means the
      sync is incomplete: re-run the import `mc mirror` in 5b (it is idempotent) and check again. If
      the key is missing on the dev box too, it was never there — fix it on dev, re-export.

### 5d. Rebuild the graph and vector tier

The restored rows say `index` and `graph_load` are done, and their receipts are in the bucket —
so simply re-dispatching them would be a cache hit that writes nothing into the new, empty Milvus
and Neo4j. `fdi-mirror replay` deletes just those receipts and re-dispatches the two stages,
reusing the mirrored embeddings and graphs (no Azure cost). Those two are the only stages that
write outside the bucket, and nothing in the DAG runs after them, so re-running `embedding` /
`graph` themselves is unnecessary. Replay is safe to repeat: if it stops partway (e.g. the broker
is unreachable), `check` reports the receipts it already deleted as `MISSING` — run `replay`
again and it re-dispatches every one of them.

- [ ] Preview, then run:
      ```bash
      dc exec api fdi-mirror replay --dry-run     # one line per <doc_id> <stage>
      dc exec api fdi-mirror replay               # prints a correlation_id
      ```
- [ ] Wait for the worker to drain (`dc logs -f worker`, filter on the printed correlation_id),
      then confirm nothing failed and the receipts are back:
      ```bash
      dc exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
        "select stage_name, status, count(*), max(updated_at) from stage_run group by 1, 2 order by 1, 2"'
      dc exec api fdi-mirror check                # back to "0 missing"
      ```
      Look at the `index` and `graph_load` rows: a `failed` group whose `max` is after the replay
      means a rebuild failed — `select doc_id, error from stage_run where status = 'failed' order by
      updated_at desc limit 5` (in `psql`) says why.
- [ ] Remove the bundle from the VM once 5c and 5d pass: `sudo rm -rf mirror fdi-mirror.tgz`.

---

## 6. Accounts

The dump carried over the dev box's accounts.

- [ ] Review and deactivate any that should not exist here:
      ```bash
      dc exec api fdi-admin list-users
      dc exec api fdi-admin deactivate-user --email <dev-account>
      ```
- [ ] Create your own operator account and each Invitee (omit `--password` to generate one; it
      is printed **once** — paste it straight into the invitation):
      ```bash
      dc exec api fdi-admin create-user --email you@example.com
      dc exec api fdi-admin create-user --email invitee@example.org
      ```

---

## 7. Edge and health

- [ ] `https://DOMAIN` loads in a browser with a valid certificate (padlock, issuer Let's Encrypt
      or ZeroSSL). If not: `dc logs edge` — ACME errors almost always mean DNS (§2) or the firewall (§1).
- [ ] Probes answer without a token:
      ```bash
      curl -fsS https://DOMAIN/api/health      # {"status":"ok"}
      curl -fsS https://DOMAIN/api/ready       # 200; a 503 names the failing dependency
      ```
- [ ] The gate is closed to anonymous callers:
      ```bash
      curl -s -o /dev/null -w '%{http_code}\n' https://DOMAIN/api/documents   # 401
      ```
- [ ] Nothing but the edge is reachable from outside (run from your laptop, not the VM):
      ```bash
      for p in 5432 6379 5672 9000 9001 7474 7687 19530 8000 3000; do
        nc -z -w 3 VM_IP $p && echo "OPEN $p  <-- wrong"; done; echo done
      ```

---

## 8. Fresh-document validation

Proves the deployed pipeline end to end on the new host — real Azure calls, real worker, the new
Milvus and Neo4j — rather than only serving mirrored results.

- [ ] Log in at `https://DOMAIN` with your operator account.
- [ ] Upload the Public filing chosen in §0 (Ingest page). Watch its stages progress until every
      stage — including `ocr`, `semantic`, `embedding`, `index` and `graph_load` — shows done.
      A failed stage shows its error; the correlation id it carries finds the same failure in
      `dc logs worker` and (if configured) in Sentry.
- [ ] Open the document: the PDF preview loads, and an artifact/provenance view loads (these are
      the presigned URLs travelling through the edge to MinIO — ADR-0001 amendment).
- [ ] On the new document's report view, click **Read figures** (the extraction job): the job goes queued → running →
      succeeded and fields fill in with page citations.
- [ ] Ask the corpus a question through the API (the UI has no Ask page; this is `POST /ask`,
      which retrieves over Milvus and Neo4j — so an answer citing a **mirrored** filing proves 5d
      rebuilt them). On any machine with `curl` and `python3`:
      ```bash
      D=https://DOMAIN; read -rp 'email: ' EM; read -rsp 'password: ' PW; echo
      T=$(curl -fsS "$D/api/auth/login" -H 'content-type: application/json'         -d "{\"email\":\"$EM\",\"password\":\"$PW\"}" |
        python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
      J=$(curl -fsS "$D/api/ask" -H "authorization: Bearer $T" -H 'content-type: application/json'         -d '{"question":"What was <mirrored company> total revenue in <year>?"}' |
        python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')
      curl -fsS "$D/api/jobs/$J" -H "authorization: Bearer $T"          # repeat until "succeeded"
      curl -fsS "$D/api/jobs/$J/result" -H "authorization: Bearer $T"   # answer + citations
      ```
      Ask one question about the mirrored filing and one about the new one; both answers must cite
      the right document. (The token lasts ~15 minutes; log in again if a call returns 401.)
- [ ] `dc exec api fdi-mirror check` still ends `0 missing`.

---

## 9. Snapshots

- [ ] Take a provider snapshot of the VM **now** — the known-good, mirrored, validated state.
      Label it `fdi-golive-<date>-<short-sha>`.
- [ ] Enable the provider's automatic backups (Hetzner: ~20 % of the server price) or schedule a
      weekly snapshot. A snapshot of a running VM can catch Postgres mid-write; for a guaranteed
      clean point, `dc stop` first, snapshot, `dc up -d`.
- [ ] Optional belt-and-braces: a nightly database dump kept off the VM:
      ```bash
      dc exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > fdi-$(date +%F).dump
      ```

---

## 10. Uptime check

- [ ] In an external monitor (UptimeRobot, Better Stack, or similar — free tier is enough), add an
      HTTPS keyword check on `https://DOMAIN/api/health` expecting `ok`, every 5 minutes, alerting
      your email.
- [ ] Add a second check on `https://DOMAIN/api/ready` expecting HTTP 200 — it fails when Postgres,
      MinIO or RabbitMQ is down even if the API process is up.
- [ ] Confirm both show "up", and that the certificate-expiry alert (if the tool offers one) is on.

---

## 11. Go-live smoke checklist

Run it as an **Invitee** would, in a private browser window, after everything above:

- [ ] **Login** — `https://DOMAIN` redirects to the login page; a wrong password is refused; the
      Invitee credentials get in.
- [ ] **Upload** — upload a small Public filing PDF; it appears in the document list.
- [ ] **Job completes** — its pipeline stages reach done; **Read figures** on its report view moves
      queued → running → succeeded.
- [ ] **Preview loads** — the PDF preview and an artifact view open without errors.
- [ ] **Session** — leave the tab idle past 15 minutes, click around: still signed in (silent
      refresh). **Sign out** returns to the login page, and reloading any page asks for login again.
- [ ] `dc logs --since 30m api worker | grep -i error` shows nothing unexpected; Sentry (if
      configured) has no new issues.

When every box is ticked, send the invitations.

---

## Day-2 reference

| Task | Command |
|---|---|
| Status | `dc ps` |
| Logs (rotated 5×10 MB per container) | `dc logs -f --since 1h api worker` |
| Upgrade to a new commit | `git fetch && git checkout <sha>`, set `IMAGE_TAG=sha-<short>` in `.env`, `dc pull && dc up -d` |
| Revoke an Invitee | `dc exec api fdi-admin deactivate-user --email <x>` (refresh refused at once; access token expires ≤ 15 min) |
| Re-verify storage | `dc exec api fdi-mirror check` |
| Roll back | restore the §9 snapshot, or check out the previous SHA and set its `IMAGE_TAG` |
