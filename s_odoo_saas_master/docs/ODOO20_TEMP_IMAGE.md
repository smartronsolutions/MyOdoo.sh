# Odoo 20 — temporary Docker image (until the official `odoo:20.0` exists)

> **STATUS: TEMPORARY.** Created 2026-09-27. Remove everything in the
> "To remove on switch" section once Docker Hub publishes `odoo:20.0`.

## 1. Why this exists

* Odoo 20 is **not released yet**, and Docker Hub has **no `odoo:20.0` tag**:
  `docker manifest inspect odoo:20.0` → `no such manifest: docker.io/library/odoo:20.0`.
  (`odoo:master`, `odoo:nightly`, `odoo:20.0-slim` do not exist either.)
* Odoo **does** publish 20.0 nightly Debian packages:
  <https://nightly.odoo.com/20.0/nightly/deb/> (e.g. `odoo_20.0.20260927_all.deb`).
* Consequence before this fix: creating an Odoo 20.0 instance failed with
  `Instance …: containers are not running (…). Deployment aborted, instance stays in draft.`
  because `docker compose up -d` could not pull the image. The real error
  (`failed to resolve reference … not found`) was swallowed — it is now surfaced
  (see `PServer._docker_compose_up()` / `_check_docker_images_available()`).

## 2. What was added

| Item | Where | Permanent? |
|---|---|---|
| Local image `odoo:20.0` (alias `odoo:20.0-temp-20260927`) | Docker on the server(s) | **Temporary** |
| Build context (Dockerfile + 3 files) | `/opt/odoo18/custom-images/odoo20-community/` | **Temporary** |
| `http_interface = 0.0.0.0` for the 20.0 version config | `s_odoo_saas_master/data/config_20_data.xml` | **Permanent — keep it** |

### The image

Built from the **official Odoo 20 nightly package**, following the recipe of the
official `odoo:19.0` image (read with `docker history`), so the runtime contract
is identical and no SaaS compose file has to change:

* base `ubuntu:noble`, `odoo 20.0.20260927` (sha1 `1869223f8700b3bc445426dd5464ceb701d233c3`)
* `wkhtmltopdf 0.12.6.1-3.jammy` (patched Qt, same file the official image uses)
* `ENTRYPOINT ["/entrypoint.sh"]`, `CMD ["odoo"]`, `ENV ODOO_RC=/etc/odoo/odoo.conf`
  — `entrypoint.sh`, `odoo.conf` and `wait-for-psql.py` were **copied from the
  official `odoo:19.0` image** to guarantee the same behaviour
* marked with labels so a temporary build is instantly recognisable:
  ```bash
  docker image inspect odoo:20.0 --format '{{.Config.Labels}}'
  # saas.image.source="temporary-local-build" saas.image.nightly_release="20260927" …
  ```

Rebuild (e.g. for a newer nightly — update `ARG ODOO_RELEASE` / `ARG ODOO_SHA`
from the `Packages` file of the nightly repo first):

```bash
cd /opt/odoo18/custom-images/odoo20-community
docker build -t odoo:20.0 -t odoo:20.0-temp-<release> .
```

> The image must be built/tagged on **every** Docker server that hosts 20.0
> instances (prod **and** the DR/Contabo box). `docker save`/`docker load` works
> to transfer it.

### `http_interface = 0.0.0.0` (must stay forever)

Odoo 20 changed the default HTTP bind address from `0.0.0.0` to **`127.0.0.1`**
(verified in `odoo/tools/config.py`, branch `20.0`: `my_default='127.0.0.1'`;
branch `19.0` still had `0.0.0.0` and even logged *"will change to 127.0.0.1 in
20.0"*). Inside a container that makes the published port unreachable, so the
value is part of the 20.0 version config in the SaaS module.

**Do not remove it when switching images** — it is needed for the official
`odoo:20.0` too.

Note: the config is written for *every* port of the instance (`-i <modules>`
runs override the image `CMD`), so the value has to live in `odoo.conf` and not
only in a Docker `CMD`. That is why it is a version config key.

## 3. Switch to the official image — the exact trigger

The owner will say:

> **"Official odoo:20.0 aa gaya hai, switch kar do"**

Then:

1. **Verify** the tag really exists:
   ```bash
   docker manifest inspect odoo:20.0
   ```
2. **Pull** it on every Docker server:
   ```bash
   docker pull odoo:20.0
   ```
   This replaces the temporary local tag.
3. **Confirm the local tag is the official build** (the label must be gone):
   ```bash
   docker image inspect odoo:20.0 --format '{{.Config.Labels}} {{.RepoDigests}}'
   ```
4. **Recreate the containers** of every 20.0 instance (~1 min downtime each,
   ask for confirmation first):
   ```bash
   cd /home/<instance_technical_name> && docker compose up -d
   docker ps --format '{{.Names}} {{.Image}} {{.Status}}' | grep <instance>
   ```
   then check the instance state/health in the SaaS app.
5. **Remove the temporary leftovers:**
   ```bash
   docker rmi odoo:20.0-temp-20260927
   rm -rf /opt/odoo18/custom-images/odoo20-community
   rm s_odoo_saas_master/docs/ODOO20_TEMP_IMAGE.md   # this file
   ```
6. **Keep** `http_interface = 0.0.0.0` in `config_20_data.xml`.

## 4. Validation done on 2026-09-27 (proof the setup works)

```
docker run --rm --entrypoint sh odoo:20.0 -c "wkhtmltopdf --version; odoo --version"
   → wkhtmltopdf 0.12.6.1 (with patched qt)
   → Odoo Server 20.0-20260927
```

A real instance (`odoo20test`, version 20.0, Enterprise server) was deployed from
the SaaS and came up:

```
docker ps  → odoo_odoo20test_…  image odoo:20.0   0.0.0.0:9012->8069/tcp
             psql_odoo20test_…  image postgres:17
GET /web/login on the exposed port → 200
container log → "HTTP service running on 0.0.0.0:8069"
                "Loading module web_enterprise (19/22)" … "Module web_enterprise loaded"
                addons paths: [..., '/mnt/standard-extra-addons', ...]   ← enterprise mount OK
```

`postgres:17` is absent locally but was pulled automatically by
`docker compose up` — the preflight check correctly did **not** block it.

Harmless warnings the nightly 20.0 prints for the legacy keys still present in
the 20.0 config (`xmlrpc*`, `longpolling_port`, `osv_memory_age_limit`,
`debug_mode`, `… = False` on non-boolean options): Odoo 20 keeps unknown config
keys "as-is" and only warns (`config.py::_load_file_options`). A future cleanup
of `data/config_20_data.xml` may drop them, but nothing breaks today.

## 5. Caveats

* Databases created while the temporary image was in use were made by a
  **nightly** 20.0 → treat those instances as **test** data. Create real
  customer databases only after the switch, or verify the upgrade path first.
* Keep the temporary image until the official one has been validated — it is the
  rollback (`docker tag odoo:20.0-temp-20260927 odoo:20.0`).
* The official image is expected to be community-only as well: enterprise
  instances keep mounting `/opt/enterprise-source/enterprise20` at
  `/mnt/standard-extra-addons` (unchanged behaviour, same as 17/18/19).
