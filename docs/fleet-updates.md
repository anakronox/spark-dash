# Updating a fleet of GB10 systems: how NVIDIA does it

What NVIDIA actually ships and recommends for keeping DGX Spark (GB10) systems
current, read off running hardware rather than inferred from marketing pages,
and written so that a separate project can build a fleet front-end on it
without redoing the investigation.

**Provenance.** Everything below was established on 2026-09-04 and 2026-09-05
against three live GB10 nodes (`sparky`, `sparketa`, `sparkjr`) and NVIDIA's
published material. Where a claim comes from reading a binary's strings, a
script's source, or a live command, it says so. Where NVIDIA's documentation
and NVIDIA's shipped code disagree, both are stated and the disagreement is
called out rather than smoothed over.

Versions observed:

| component | version | where |
|---|---|---|
| DGX OS base image | `DGX_SWBUILD_VERSION="7.2.3"`, `DGX_OTA_VERSION="7.5.0"` | `/etc/dgx-release` |
| Ubuntu | 24.04 (noble), arm64 | apt sources |
| `dgx-dashboard` | 0.29.1 | `dpkg -l` |
| `nvidia-spark-ota-check` | 1.0.16-1 | `dpkg -l` |
| `dgx-spark-ota-update-meta` | 26.04.1 | `dpkg -l` |
| fwupd | 2.0.20 | `fwupdmgr --version` |
| Enterprise Lifecycle Integration Scripts | zip dated 2026-05-20, tools at 1.1.0 | NVIDIA docs download |
| Enterprise Manageability Guide (PDF) | dated 2026-01 in its own text | NVIDIA docs |

---

## 1. The short version

- **NVIDIA's fleet answer is not the DGX Dashboard.** The Dashboard is the
  single-machine experience, bound to `127.0.0.1` with a code-level check that
  refuses any other host. For more than one Spark, NVIDIA publishes an
  "Enterprise Manageability" framework: **agentless SSH execution, one JSON
  document on stdout per tool, driven from a platform you already run.**
  Canonical Landscape is the primary recommended platform; Ansible, Puppet,
  Chef and Tanium are named as alternatives.
- **Under everything is apt plus fwupd.** A metapackage,
  `dgx-spark-ota-update-meta`, pulls in each release's dependencies; firmware
  comes through fwupd from LVFS and a vendor directory. `apt full-upgrade` and
  `fwupdmgr upgrade` over SSH *is* the official mechanism. The Dashboard is a
  button on top of it.
- **"An update is available" is release-level, not a package count.** NVIDIA
  ships OTA "recipes" (JSON manifests of package, firmware and kernel versions
  per release) and a checker, `nvidia-spark-ota-check`, that scores the
  installed system against them. The Dashboard prompts on "July 2026 is
  available", never on "210 packages".
- **The documentation is ahead of the code.** The framework's user guide
  describes `spark_updatectl.py` 2.0.0 with `update check`, `update now`,
  `repo set` and update policy. The package NVIDIA actually publishes ships
  1.1.0, which has reboot coordination, kernel rollback and firmware
  *reporting* only. Section 6 lists every such gap.
- **Everything needed to build a fleet view is already on every node and
  readable without root**, with one exception (the firmware GUID check inside
  the OTA scorer). Section 7 gives the per-field recipe.

---

## 2. Three layers

NVIDIA's update story has three layers, and confusing them is what makes it
look like there is no fleet answer.

```
┌───────────────────────────────────────────────────────────────────────┐
│ Layer 3 — Enterprise Manageability framework (fleet)                  │
│   agentless SSH · JSON stdout · Landscape / Ansible / Tanium · rings  │
│   tools: spark_updatectl.py, spark_diagctl.py, *_reporter.py          │
├───────────────────────────────────────────────────────────────────────┤
│ Layer 2 — DGX Dashboard (one box)                                     │
│   dgx-dashboard.service :11000 (unprivileged, JWT, localhost only)    │
│   dgx-dashboard-admin.service (root, D-Bus com.nvidia.dgx.dashboard.admin1) │
├───────────────────────────────────────────────────────────────────────┤
│ Layer 1 — the substrate (every box)                                   │
│   apt repos + pins · dgx-spark-ota-update-meta · nvidia-spark-ota-check │
│   OTA recipes in /opt/nvidia/spark-ota-check/metadata · fwupd + LVFS   │
└───────────────────────────────────────────────────────────────────────┘
```

Layer 3 drives Layer 1 directly and ignores Layer 2. A fleet front-end should
do the same, and can borrow Layer 1's checker and Layer 2's read-only D-Bus
call for its data.

---

## 3. Layer 1 — the substrate on every node

### 3.1 apt repositories and pins

Read from `/etc/apt/sources.list.d/` on `sparky`:

| source file | URI | suites |
|---|---|---|
| `ubuntu.sources` | `http://ports.ubuntu.com/ubuntu-ports/` | noble, noble-updates, noble-backports, noble-security |
| `spark.sources` | `https://repo.download.nvidia.com/spark/ubuntu/arm64/` | — |
| `dgx.sources` | `https://repo.download.nvidia.com/baseos/ubuntu/noble/arm64/` | — |
| `cuda-compute-repo.sources` | `https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/sbsa/` | / |
| `nvhpc.sources` | `https://developer.download.nvidia.com/hpc-sdk/ubuntu/arm64/` | / |
| `canonical-nvidia-ubuntu-nvidia-desktop-edge-noble.sources` | `https://snapshot.ppa.launchpadcontent.net/canonical-nvidia/nvidia-desktop-edge/ubuntu/` | noble |
| `nv-vulkan-desktop-ppa.sources` | `https://snapshot.ppa.launchpadcontent.net/canonical-nvidia/vulkan-packages-nv-desktop/ubuntu/` | noble |
| `ubuntu-esm-apps.sources`, `ubuntu-esm-infra.sources` | `https://esm.ubuntu.com/{apps,infra}/ubuntu` | noble-*-security, noble-*-updates |
| `ai-workbench-desktop.sources` | `https://workbench.download.nvidia.com/stable/linux/debian` | default |

The CUDA and HPC SDK repos are deliberately **pinned down** (`Pin-Priority:
-1`) for the driver family and several packages, via
`/etc/apt/preferences.d/cuda-compute-repo-lowpri*`, `hpc-sdk-repo`,
`nvidia-dgx` and `nvidia-spark-repo`. The 580 driver, fabricmanager, imex,
persistenced and nsight packages must come from NVIDIA's Spark/DGX repos, not
from the generic CUDA repo. A fleet tool that runs plain `apt full-upgrade`
inherits these pins; one that constructs its own apt calls must not override
them.

### 3.2 The OTA metapackage

`dgx-spark-ota-update-meta` is how a release "pulls new dependencies into the
DGX Spark" (its own description). Its `Depends:` line grows with each release,
which is how a plain `apt full-upgrade` acquires a release's new components.
Versions visible in the apt cache on `sparky`, oldest to newest:

| version | notable new dependencies |
|---|---|
| 25.06.1 | (debconf only) |
| 25.10.2 | `nvidia-lvfs-config`, `nvidia-spark-run-apt-upgrade`, fstab/baud/grub config |
| 25.10.5 | `nvidia-desktop-default-snaps`, gnome-software removal |
| 26.02.1 | `dgx-spark-mlnx-hotplug`, `nvidia-spark-initcall-bl`, `nvidia-enable-bt-profiles` |
| 26.03.1 | `lldpd`, `nvidia-spark-avahi-conf`, **`nvidia-spark-repo`** |
| 26.04.1 | **`nvidia-spark-ota-check`**, `nvidia-spark-limits` |

Two of those dependencies matter for automation:

- **`nvidia-spark-run-apt-upgrade`** installs a one-shot unit,
  `nvidia-spark-run-apt-upgrade-once.service`, which runs early in boot
  (`Before=multi-user.target gdm.service`) exactly once, gated on a done-flag
  in `/var/lib/nvidia-spark-run-apt-upgrade-once/`. It exists to finish a
  driver/kernel module realignment after a repo pin change. A fleet tool
  should expect one extra apt run on the first boot after certain upgrades,
  and should not fight it for the dpkg lock.
- **`nvidia-spark-ota-check`** is the release checker, below.

### 3.3 The OTA checker: `nvidia-spark-ota-check`

Installed at `/opt/nvidia/spark-ota-check/`. Pure Python, stdlib only, invoked
as `python3 /opt/nvidia/spark-ota-check/check_ota_status.py <command>`. It is
what the Dashboard calls, and it is the closest thing to an official "is this
node current?" oracle.

**Requires root.** `main()` calls `_require_root()` before anything else.
Reason: the firmware half of the check reads fwupd device GUIDs. Everything
else it does (`dpkg-query`, `dpkg --compare-versions`) would work unprivileged.

**Recipes.** `metadata/spark-ota-*.json`, one per release. Observed files:
`spark-ota-0.json`, `0.1`, `1`, `1.1`, `2`, `2.1`, `2.2`, `2604`,
`2604.Computex`, `2604-ebeta`, `2607`. Structure (from `spark-ota-2607.json`):

```json
{
  "metadata": {
    "name": "OTA2607", "external_name": "July 2026",
    "description": "This update includes a driver update ...",
    "priority": ..., "releaseNotesUrl": "https://docs.nvidia.com/dgx/dgx-spark/release-notes.html",
    "releaseDate": "2026-07-15T00:00:00.000Z",
    "required_match": ["EC", "EC Unfused"]
  },
  "package":  [ {"name": "cuda-cccl-13-0", "version": "13.0.85-1"}, ... ],   // 147 entries
  "firmware": [ {"name": "SOCFW", "version": "2.155.11", "guid": "b488217b-..."} ... ],  // 5
  "connectx": [ {"name": "CX7", "version": "28.45.4028"} ],
  "software": [ {"name": "kernel", "version": "6.17.0-1022-nvidia"}, ... ]  // 2
}
```

Recipes containing `ebeta` in the name are treated as pre-release and excluded
from "latest" (`_is_ebeta`).

**Scoring** (`ota_checker.py`). For every recipe, every package, software and
firmware entry becomes a `CheckResult` (pass/fail). Packages pass when
`installed >= expected` **and** `installed < ceiling`, where the ceiling is the
same package's version in the next newer recipe that raised it. That range
check is what lets the scorer say *which* release a box is on rather than
merely "at least this old". Per recipe it computes:

- `match_pct` and per-component `match_pkg_pct` / `match_sw_pct` / `match_fw_pct`
- `torn_pct` = failed checks / total checks, overall and per component
- `detection_rank` = 0.35·fw + 0.25·sw + 0.40·pkg (the weights are constants
  `DETECTION_FW_WEIGHT`, `DETECTION_SW_WEIGHT`, `DETECTION_PKG_WEIGHT`)
- `required_satisfied`: every name in `metadata.required_match` passed

The **detected** OTA is the recipe with the highest `detection_rank` whose
required matches are satisfied. **Available** means the detected stable recipe
is not the newest stable recipe by release date.

**Commands and their JSON.** All emit JSON on stdout, exit 1 with
`{"error": "..."}` on failure.

| command | output (observed on `sparky`, 2026-09-05) |
|---|---|
| `is-ota-available` | `{"available": true, "name": "July 2026", "description": "...", "releaseNotesUrl": "...", "releaseDate": "2026-07-15T00:00:00.000Z", "metadataFilePath": "/opt/nvidia/spark-ota-check/metadata/spark-ota-2607.json", "recommendedActions": []}` |
| `installed-name` | `{"name": "OTA2.2", "releaseDate": "2026-03-31T00:00:00.000Z"}` |
| `torn-score` | `{"name": "OTA2.2", "releaseDate": "...", "torn": 3}` |
| `summary` | `{"detected_ota": "OTA2.2", "releaseDate": ..., "torn": 3.3, "scores": [ {"ota", "releaseDate", "detection_rank", "match", "torn", "total_checks", "passed_checks", "failed": [...], "required_match": [...], "required_satisfied"} , ...]}` |
| `installed-versions` | `{"packages": [ {"name", "installedVersion"}, ... ]}` |
| `ota-versions` | `{"packages": [ {"name", "minRequiredVersion"}, ... ]}` |
| `--version` | `nvidia-spark-ota-check 1.0.16-1` |
| `-v` on any | prints per-check lines (`- name match: ver`, `ERROR: name version too low: ...`) before the JSON |

`recommendedActions` is populated only when a component is more than 70%
torn (`TORN_THRESHOLD_PCT = 70`) and maps straight to the manual commands:

```python
_COMPONENT_ACTIONS = {
    "firmware": "fwupdmgr refresh ; fwupdmgr upgrade",
    "software": "apt update ; apt full-upgrade",
    "packages": "apt update ; apt full-upgrade",
}
```

**Self-update.** `is-ota-available` first runs `self_update.py`, which
`apt-get install`s a newer `nvidia-spark-ota-check` if the apt cache has one —
**unless** `dgx-dashboard-admin.service` is enabled and active and the
Dashboard's `settings.json` does not have `update.enabled: false`, in which
case it assumes the Dashboard's own timer will do it. So a node without the
Dashboard running will upgrade this one package as a side effect of asking
whether updates exist. A fleet tool calling this command must expect that, or
disable the Dashboard's updates, or pre-install the checker itself.

### 3.4 Firmware

fwupd 2.0.20 with three remotes: `vendor-directory` (enabled), `lvfs`
(enabled, `https://cdn.fwupd.org/downloads/firmware.xml.zst`), `lvfs-testing`
(disabled). `nvidia-lvfs-config` from the metapackage configures this. The OTA
recipes name five firmware components by GUID (SOCFW, EC, EC Unfused, TPM and
one more) plus a ConnectX-7 NIC firmware version checked separately. The
manual path is `fwupdmgr refresh` then `fwupdmgr upgrade`, followed by a
reboot; the Dashboard's binary carries the string `Firmware phase polling timed
out after 25 minutes`, which is the budget it allows that phase.

### 3.5 The Ubuntu side, which NVIDIA leans on

- `update-notifier` writes `/var/lib/update-notifier/updates-available`
  (world-readable text) and `/usr/lib/update-notifier/apt-check` prints
  `total;security` (`210;133` on `sparky`). The file's wording varies between
  nodes — `sparky` has an extra "ESM Apps" line — so parse by regex, not by
  line number. It is refreshed by `apt-daily.timer`, roughly daily, so its
  mtime is the honest "as of" for any count read from it.
- `/run/reboot-required` and `/run/reboot-required.pkgs` are the reboot flag
  and its reasons. `/var/run` is a compatibility symlink to `/run`.
- Ubuntu Pro is attached on all three nodes with `esm-apps` and `esm-infra`
  enabled and `landscape` disabled; `landscape-common` is installed,
  `landscape-client` is inactive.

### 3.6 The manual procedure NVIDIA publishes

From the [OS and Component Update guide](https://docs.nvidia.com/dgx/dgx-spark/os-and-component-update.html):

```
sudo apt update
sudo apt dist-upgrade
sudo fwupdmgr refresh
sudo fwupdmgr upgrade
sudo reboot
```

The checker's own `recommendedActions` say `full-upgrade` rather than
`dist-upgrade`; on modern apt they are the same operation.

---

## 4. Layer 2 — the DGX Dashboard, and what it really does

Two systemd units from the `dgx-dashboard` package, split by privilege:

| unit | runs as | binary | notes |
|---|---|---|---|
| `dgx-dashboard.service` | `dgx-dashboard-service-user` | `/opt/nvidia/dgx-dashboard-service/dashboard-service -port ${DGX_DASHBOARD_PORT} serve` | Go; `DGX_DASHBOARD_PORT=11000` from `ports.env`; listens on `127.0.0.1:11000`; JWT via `GenerateJWT`/`ValidateJWT`; the binary validates `host must be localhost, got %q` |
| `dgx-dashboard-admin.service` | `root` | `/opt/nvidia/dgx-dashboard/dashboard-admin` | `Type=dbus`, `BusName=com.nvidia.dgx.dashboard.admin1`, object path `/com/nvidia/dgx/dashboard/admin` |

Logs: `/var/log/dgx-dashboard-service.log`, `.err.log`,
`/var/log/dgx-dashboard-admin.log`, `.err.log`, and
`/var/log/dgx-dashboard-reboot.log`. Settings: `/opt/nvidia/dgx-dashboard/settings.json`
(absent on `sparky`, meaning defaults; `update.enabled` is the one key the
checker reads). `/usr/bin/dgx-dashboard` is a shell wrapper that opens the
browser on the port.

### 4.1 HTTP routes (from the binary's strings)

```
GET  /ota/availability
GET  /updates/available
GET  /updates/list
POST /updates/available        (toggles automatic updates; matches settings.json update.enabled)
GET  /update_reboot/status
POST /update_reboot
GET  /hostname          POST /hostname
GET  /jupyterlab        POST /jupyterlab        POST /jupyterlab/stop     GET /jupyterlab/stream
GET  /chrome/available
POST /telemetry
```

Every route redirects to `/` without a valid JWT, and the service is bound to
loopback, so **none of this is reachable from a fleet front-end**. The web UI
is a SPA in `frontend/assets/`.

### 4.2 The D-Bus interface (`com.nvidia.dgx.dashboard.admin1`)

Method names from the binary's strings:

```
Authenticate                    GenerateJWT                 InValidateJWT
GetOTAAvailabilitySnapshot      GetUpdatesAvailableWithOTAInfo
GetUpdatesList                  StatusUpdateCache
UpdateAndReboot                 IsUpdateRebootRunning       GetFWPhase
ChangeHostnameAndReboot
ActivateJupyterlab              StopJupyterlab              GetJupyterlabInfo   GetJupyterlabLog
GetChromeAvailable
```

Policy (`/etc/dbus-1/system.d/com.nvidia.dgx.dashboard.admin1.conf`), which
is the part that matters for anyone else's tooling:

- only `root` may own the name;
- members of `dgx-dashboard-service-group` may call anything on it;
- **the default context (any local user) may call exactly one method:
  `GetOTAAvailabilitySnapshot`.**

That one method is genuinely useful and was verified unprivileged on all three
nodes:

```
$ busctl call com.nvidia.dgx.dashboard.admin1 /com/nvidia/dgx/dashboard/admin \
    com.nvidia.dgx.dashboard.admin1 GetOTAAvailabilitySnapshot
ssbbs "ready" "2026-09-05T10:33:15+03:00" true true "July 2026"
```

Signature `ssbbs`: a state string (`ready`; the binary also carries
`cache-pending`, `updates-disabled`, `no-ota-available`, `missing-ota-name`,
`already-prompted`), the snapshot timestamp, two booleans, and the OTA's
external name. Read on all three nodes at once:

| node | reply | meaning |
|---|---|---|
| `sparky` (on OTA2.2) | `"ready" "…" true true "July 2026"` | OTA available |
| `sparkjr` (behind) | `"ready" "…" true true "July 2026"` | OTA available |
| `sparketa` (current) | `"ready" "…" true false ""` | nothing available |

So the **second boolean is "an OTA is available"** and the final string is its
name, empty when there is none. The first boolean was `true` everywhere, with
automatic updates enabled everywhere, and is most plausibly that setting; a
node with updates disabled would confirm it. The object does not implement
`Introspectable` even for root, so the signature had to be read from live
calls rather than from introspection.

The remaining methods are root-or-dashboard-group only. `UpdateAndReboot` is
the trigger; `GetFWPhase` and `IsUpdateRebootRunning` are its progress
surface; `StatusUpdateCache` and `GetUpdatesList` back the "updates
available" page.

### 4.3 What an update through the Dashboard actually runs

Assembled from the admin binary's strings and the reboot helper:

1. A periodic `refreshUpdatesCache` runs `check_ota_status.py
   is-ota-available` and caches the result (`refreshUpdatesCache: completed`).
   It also **auto-upgrades the OTA metapackage** (`Auto-upgrading OTA
   metapackage`), which is why the checker's `self_update` defers to it.
2. On `UpdateAndReboot` the admin drives apt through the system
   `org.debian.apt` D-Bus transaction API (aptdaemon; string
   `org.debian.apt.transaction.Run`), then runs `fwupdmgr upgrade` (`Failed to
   run fwupdmgr upgrade` on error), reporting firmware progress through
   `GetFWPhase` with a 25-minute ceiling.
3. It then runs `/opt/nvidia/dgx-dashboard/dgx-dashboard-reboot.sh`, which is
   in its entirety: log a line, `sleep 10`, `systemctl reboot`.

So the Dashboard is a UI with a root helper around the same apt and fwupd
calls the manual procedure uses, plus a cache and a JWT.

---

## 5. Layer 3 — the Enterprise Manageability framework

Published at [Enterprise Manageability](https://docs.nvidia.com/dgx/dgx-spark/enterprise-manageability.html)
and [Enterprise Lifecycle Integration](https://docs.nvidia.com/dgx/dgx-spark/enterprise-fleet-lifecycle.html),
with a PDF [Enterprise Manageability Guide](https://docs.nvidia.com/pdf/enterprise-manageability-guide-dgx-spark.pdf)
and a downloadable "Enterprise Lifecycle Integration Scripts" zip. NVIDIA's
own summary: *"agentless SSH execution, and standardized JSON outputs for
seamless integration into monitoring and management pipelines"*, covering
*"procurement, provisioning, monitoring, maintenance, incident response, and
end-of-life"*, for *"both internet-connected and fully air-gapped
deployments"*.

### 5.1 The model

Quoted from the guide, section 1.5:

> Remote execution: SSH · Output contract: JSON on stdout (small, bounded) ·
> Deep evidence: artifacts referenced by JSON and pulled only when needed.
> This model intentionally avoids requiring a resident management agent on the
> DGX Spark endpoint.

The universal loop (section 2.1): select targets (static groups, **rings**, or
dynamic inventory) → execute over SSH → capture stdout, stderr, exit code →
parse stdout JSON off-box → ingest into CMDB / monitoring / ITSM → optionally
retrieve artifacts. Two explicit design rules recur throughout: **separate
read-only collectors from state-changing controllers**, and **parse off-box,
never on the device**.

The guide is deliberately non-prescriptive about connectivity and lists three
patterns with trade-offs: direct per-device SSH (labs), bastion / jump host
(segmented networks), and brokered execution from a central tier (platforms).
Operational requirements it names: stable addressing or an authoritative
inventory, non-interactive auth, *"predictable sudo permissions for tools"*.

### 5.2 The lifecycle backbone, and the update stage inside it

Six stages (section 1.4): Procurement → Provisioning → Ongoing Monitoring →
**Maintenance Windows** → Incident Response → End-of-Life. The maintenance
stage is defined as:

> Coordinate controlled updates and reboots within change windows. Validate
> update outcomes and preserve rollback safety. Enforce staged rollouts
> (rings and waves) to reduce fleet risk.

Its tool mapping (section 3.6.4):

| phase | tools | evidence |
|---|---|---|
| Pre-check | `os_build_identity.py`, `spark_diagctl.py` (health) | stdout JSON |
| Update control | `spark_updatectl.py` | stdout JSON + optional artifact |
| Post-check | build identity + health again | stdout JSON |

The blog's phrasing of the rollout is *"pilot → waves → broad"*, and the
Ansible section says host groups **are** the rings: *"Use groups for rings or
waves (such as pilot, wave1, wave2, or broad)"*, *"Use become for controllers;
keep collectors unprivileged when possible"*.

### 5.3 The tools

Installed to `DGX_spark_management/bin/` by `bash install.sh` (which installs
`common/` first — the tools import it). Stdlib-only Python, no agent, no
daemon. **Not preinstalled on any DGX Spark**; verified absent on all three
nodes.

| tool | purpose | privilege |
|---|---|---|
| `device_identity.py` | stable identity from DMI/SMBIOS (serial, UUID) | none |
| `hardware_config.py` | CPU, memory, storage, NICs, GPUs, PCI | none |
| `firmware_reporter.py` | BIOS/UEFI, fwupd devices, NIC/NVMe/GPU firmware | none |
| `os_build_identity.py` | Ubuntu release, kernel, DGX build id, **baseline package fingerprint** | none |
| `driver_inventory_reporter.py` | device→driver bindings | none |
| `software_inventory_reporter.py` | dpkg, snap, pip, docker inventory with hashes | none |
| `NVAIAread` / `NVAIAwrite` | UEFI-backed asset tags | write needs root |
| `spark_updatectl.py` | reboot coordination, kernel rollback, firmware rollback *report* (v1.1.0); update policy and apply (documented v2, not shipped) | read-only without root; controllers need root |
| `spark_diagctl.py` | `status`, `health`, `logs`, `hwfw-events`, `crash status/configure`, `gpu`, `collect-all` | mostly none |
| `reset_reason_reporter.py` | why the last reboot happened (journal, wtmp, pstore, EFI) | none |

Plus "Landscape reference scripts" — deliberately thinner examples meant to be
pasted into Landscape's remote-script execution: APT signing verification,
verified boot integrity, recovery/backup levels, factory reset with
re-provisioning, health watchdogs, collect-package support bundle,
retrieve-logs-to-stdout, encryption-at-rest reporting.

### 5.4 The JSON contract

Every production tool prints exactly one JSON document to stdout, built by
`common/output.py`:

```json
{
  "ok": true,
  "data": { ... tool-specific ... },
  "errors": [ {"code": "...", "message": "...", "detail": "", "hint": "", "severity": "error|warning"} ],
  "meta": {
    "tool": "spark_updatectl",
    "version": "1.1.0",
    "collected_at_utc": "2026-09-05T07:33:15Z",
    "artifacts": ["..."],          // only when produced
    "truncation": { ... }          // only when output was bounded
  }
}
```

`ok: false` sets `data: null`. `errors[]` on a success envelope carries
non-fatal warnings. Exit codes: 0 ok, 1 failed (see `errors`), 2 invalid
arguments, 3 permission denied. `--human` switches any tool to a readable
summary. Some tools also write their JSON on-device to
`/var/lib/dgx_spark_management/{functional_area}/{tool}/` and log to
`/var/log/dgx_spark/{functional_area}/{tool}/`.

The guide asks the **orchestrator** to wrap each run in its own record
(section 2.4), and this is the shape a fleet front-end should store per
invocation:

| field | purpose |
|---|---|
| `tool` | stable identifier of the command |
| `ts` | UTC timestamp |
| `host` | hostname or asset id used by the orchestrator |
| `status` | `ok` or a failure class |
| `rc` | exit code |
| `duration_ms` | runtime, for SLOs |
| `summary` | small bounded summary for indexing |
| `warnings` | optional |
| `artifacts` | optional pointers |

And its failure taxonomy, worth adopting verbatim: **transport/auth**
(cannot connect), **privilege** (needs sudo), **tool execution** (missing
dependency or internal error), **endpoint degraded** (ran fine, reported a
bad health signal).

### 5.5 `spark_updatectl.py` in detail

**As documented** (`docs/user_guides/update_control_plane.md`, "v2.0.0"):

| command | root | does |
|---|---|---|
| `status` | no | unified system status |
| `updates status` | no | whether automatic updates are enabled; apt and fwupd timer states |
| `updates enable` / `disable` | yes | start/stop the timers |
| `update check` | no | `apt-get update` + `apt-get upgrade --dry-run` + `fwupdmgr get-updates` |
| `update now` | yes | `apt-get upgrade -y` then `fwupdmgr update -y` |
| `repo set --mirror-url URL` / `repo status` / `repo reset` | yes | point at a local apt mirror (`dgx-spark-local-mirror.list`) |
| `internet deny` / `allow` / `status` | yes | disable/enable internet `.list` sources for air-gap |
| `policy status` / `set k=v` / `reset` | set/reset yes | unified policy in `state.json` |
| `--self-test` | no | |

State file: `/var/lib/dgx_spark_management/controlled_sw_fw_updates/update_control_plane/state.json`.

**As shipped** (the 2026-05-20 zip, `VERSION = "1.1.0"`):

| command | root | does |
|---|---|---|
| `status` | no | kernel, uptime, boot id, pending reboot (`/run/reboot-required` + `.pkgs`), systemd inhibitors, timeouts, reboot history, GRUB kernels and env, fwupd summary |
| `reboot plan [--reason] [--delay-sec]` | no | readiness: blocked/`safe_to_proceed`, block reasons, inhibitors |
| `reboot now [--force]` | yes | reboot honouring inhibitors |
| `reboot schedule --at / --in-minutes` | yes | transient systemd timer `spark-updatectl-reboot.timer` |
| `reboot cancel` / `schedule-status` | yes / no | |
| `rollback kernel-list` | no | GRUB menuentries |
| `rollback kernel-set-next --kernel` / `kernel-clear-next` | yes | one-time next-boot kernel via `grub-reboot` semantics |
| `rollback os-backup-status [--export-manifest]` | no | snapshot capability (btrfs/zfs/tools) — detection only |
| `fw rollback-report [--query-releases]` | no | fwupd devices, version floors, upgrade-only flags — **report only, never executes a downgrade** |

The `updates`, `update`, `repo`, `internet` and `policy` groups **do not
exist in the shipped file**, nor do the `apt_policy.py`, `fwupd_policy.py`,
`internet_policy.py`, `repo_manager.py` modules the guide lists. The
production document for the tool is honest about this: its "Out of Scope"
lists ring management, firmware rollback execution and package rollback.

The `status` document is the most useful shipped piece for a fleet view:

```json
{ "ok": true, "data": {
    "timestamp": "...",
    "kernel": {"current_kernel": "6.14.0-1015-nvidia", "uname_all": "..."},
    "uptime": {"uptime_seconds": 432000, "uptime_human": "5 days, 0:00:00", "boot_id": "..."},
    "pending_reboot": {"pending": false, "indicators": [], "packages": []},
    "inhibitors": {"inhibitors": [{"what","who","why","mode"}], "blocking_count": 0, "delay_count": 1},
    "systemd_timeouts": {"DefaultTimeoutStopUSec": "1min 30s"},
    "last_reboot_history": [...],
    "available_kernels": [{"title","menuentry_id","kernel_version"}],
    "grub_env": {"saved_entry": "0", "next_entry": ""},
    "firmware_rollback_summary": {"fwupd_available": true, "device_count": 8}
}, "errors": [] }
```

### 5.6 Platforms

**Canonical Landscape** — named first everywhere. Included with Ubuntu Pro,
and Pro's free personal tier covers up to five machines (fifty for Ubuntu
members), so a three-node lab qualifies at no cost. Enrollment is Appendix B
of the guide, in full:

1. Ubuntu One account; create a Landscape organization at
   `https://landscape.canonical.com`; note the account name.
2. `sudo pro status`; if not attached, `sudo pro attach <TOKEN>` from
   `https://ubuntu.com/pro`.
3. `sudo pro enable landscape` — interactive: self-hosted? No; computer
   title; account name. Installs and starts `landscape-client`.
4. Approve the machine under **Pending Machines** in the portal.
5. `sudo systemctl status landscape-client`; logs at
   `/var/log/landscape/client.log`.

After enrollment Landscape gives, in NVIDIA's list: tags, remote script
execution, **scheduled updates**, package and security status per machine,
access groups, update and compliance policies. This is the "one screen for
the fleet's pending updates and one action to upgrade a selected set" that
the Dashboard lacks. All three nodes here already have Pro attached and
`landscape-common` installed; only `pro enable landscape` and the portal
approval are missing.

**Ansible** — SSH-native, so it is the guide's worked example. Inventory
groups are rings; collectors run unprivileged with `changed_when: false` and
their stdout is copied per host to `./out/{{ inventory_hostname }}_*.json`;
controllers use `become`; artifacts are `fetch`ed by reading the pointer out
of the JSON.

**Tanium** — a package wraps the SSH payload, stdout JSON becomes the
question result, bundles become attachments.

**Puppet / Chef** — for ensuring the tool directory exists, running collectors
on a cadence, and drift detection against baselines; *"Controllers: use
sparingly. Prefer change-window orchestration platforms for risky actions."*

**Air-gapped / local mirror** — the release notes describe *"USB and Local
Repository Support for Installations and Updates"*; the framework's
(documented, unshipped) `repo set --mirror-url` and `internet deny` are the
tooling for it. On the apt side this is a standard mirror of the URIs in
section 3.1 plus LVFS metadata mirroring for fwupd.

### 5.7 Provisioning and re-provisioning

Appendix C covers cloud-init on DGX Spark with a NoCloud seed for first-boot
provisioning; the [custom installation page](https://docs.nvidia.com/dgx/dgx-spark/enterprise-custom-install.html)
is the online version. Relevant to a fleet tool only insofar as a
re-imaged node comes back at the image's OTA level (`DGX_SWBUILD_VERSION`)
and immediately reads as "available" until its first update.

---

## 6. Where the documentation and the shipped code disagree

Listed so a front-end is built on what exists, not on what is described.

| documented | reality (2026-09-05) |
|---|---|
| `spark_updatectl.py` v2.0.0 with `update check`, `update now`, `updates`, `repo`, `internet`, `policy` | shipped file is v1.1.0 with `status`, `reboot`, `rollback`, `fw` only |
| `bin/apt_policy.py`, `fwupd_policy.py`, `internet_policy.py`, `repo_manager.py` | absent from the zip; `install.sh` references them and skips |
| FAQ: `sudo dgx-check-updates`, `dgx-apply-updates --interactive`, `dgx-config --show-maintenance`, `dgx-self-update`, `dgx-rollback-update`, `dgx-update-history` | none of these commands exist on a node or in the package |
| Deployment guide: `sudo apt install dgx-spark-management`, `releases.example.com` tarball | placeholders; no such apt package |
| Guide's Ansible examples invoke `identity.json`, `reset_reason.json`, `diag_collect.json` as commands | typos for the tools' names; the JSON files are their outputs |
| Guide section 2.2 example path `/usr/local/bin/dgx-mgmt/` | install script puts tools in the repo's own `bin/`; no fixed system path |
| `spark_updatectl.py` "exposes current update status as JSON, including packages that need updating, firmware updates that are applicable" (blog) | true of the documented v2 `update check`; the shipped `status` reports pending *reboot* and fwupd device count, not pending packages |

None of this invalidates the framework; it means the **apply** half is apt
and fwupd invoked directly, and the **pending packages** figure comes from
apt itself until NVIDIA ships the v2 tool.

---

## 7. What a fleet front-end can build on today

### 7.1 Per-node facts, and exactly where each comes from

Every row below was exercised on the live nodes. "none" in the privilege
column means it works as an ordinary SSH user.

| fact | source | privilege | notes |
|---|---|---|---|
| Detected OTA (what release the box is on) | `check_ota_status.py installed-name` | root | `{"name": "OTA2.2", "releaseDate": ...}` |
| Latest OTA and whether it applies | `check_ota_status.py is-ota-available` | root | release-level verdict; triggers self-update unless the Dashboard is enabled |
| Same, cheaply and unprivileged | D-Bus `GetOTAAvailabilitySnapshot` | **none** | local call only; needs the Dashboard admin service running, which it is by default |
| How torn a box is, and which components | `check_ota_status.py summary` | root | per-recipe `failed` list names the exact packages/firmware |
| Target versions for the latest release | `metadata/spark-ota-*.json` | none | world-readable; parse the newest non-ebeta recipe by `releaseDate` |
| Pending package count and security count | `/usr/lib/update-notifier/apt-check` → `total;security` | none | or parse `/var/lib/update-notifier/updates-available`; age = its mtime |
| Pending package list | `apt list --upgradable` or `apt-get -s full-upgrade` | none (dry-run) | counts differ from apt-check by the ESM set |
| Firmware updates applicable | `fwupdmgr get-updates` | none | needs network to LVFS; empty output means current |
| Reboot required, and why | `/run/reboot-required`, `/run/reboot-required.pkgs` | none | |
| Reboot safety (inhibitors) | `spark_updatectl.py reboot plan` | none | `safe_to_proceed`, `block_reasons` |
| OS build identity and package fingerprint | `os_build_identity.py` | none | the drift anchor NVIDIA recommends |
| Firmware inventory | `firmware_reporter.py` | none | |
| Why the last reboot happened | `reset_reason_reporter.py` | none | post-update validation |
| Image build and OTA stamp | `/etc/dgx-release` | none | `DGX_SWBUILD_VERSION`, `DGX_OTA_VERSION`, serial |
| Whether Landscape is enrolled | `systemctl is-active landscape-client`, `pro status` | none | |

### 7.2 Per-node actions, and their privilege

| action | command | privilege |
|---|---|---|
| Refresh indexes | `apt-get update`, `fwupdmgr refresh` | root |
| Apply the release | `DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y` | root; respects NVIDIA's pins |
| Apply firmware | `fwupdmgr upgrade -y` | root; may take up to 25 min; reboot after |
| Reboot with guardrails | `spark_updatectl.py reboot now --reason ...` or `reboot schedule --in-minutes N` | root |
| Roll back a bad kernel | `spark_updatectl.py rollback kernel-set-next --kernel ...` then reboot | root |
| Upgrade the checker itself first | `apt-get install nvidia-spark-ota-check` | root; otherwise `is-ota-available` does it for you |

Things a controller must respect on these nodes: the dpkg lock is contended
by the Dashboard's own cache refresh and auto-upgrade of the metapackage, by
`apt-daily`/`unattended-upgrades`, and once by
`nvidia-spark-run-apt-upgrade-once` after certain upgrades. Serialize on
`/var/lib/dpkg/lock-frontend` the way `self_update.py` does, or disable the
Dashboard's updates (`POST /updates/available` locally, or `settings.json`
`update.enabled: false`) on managed nodes.

### 7.3 The workflow NVIDIA describes, as a state machine

```
select ring (pilot | wave-N | broad)
  └─ for each node in ring, in parallel:
       precheck   os_build_identity.py · spark_diagctl.py health · reboot plan
                  gate: ok:true, no blocking inhibitors, disk headroom
       apply      apt-get update && apt-get full-upgrade -y
                  fwupdmgr refresh && fwupdmgr upgrade -y
       reboot     spark_updatectl.py reboot now (or schedule into the window)
       postcheck  is-ota-available → available:false and name == target
                  os_build_identity.py fingerprint changed as expected
                  reset_reason_reporter.py shows a clean, intended reboot
                  spark_diagctl.py health ok
  └─ ring gate: all nodes postcheck-clean → next ring; any failure → hold,
     attach evidence, human decision
```

Store per run: the orchestrator envelope from section 5.4, the raw stdout
JSON of every step, and the checker's `summary` before and after — that pair
is the audit trail that says exactly which packages moved.

### 7.4 Design consequences

- **Target of record is the newest stable OTA recipe**, not "no packages
  pending". A node can have hundreds of pending Ubuntu packages and be on the
  current NVIDIA release; the two questions are different and both belong on
  the screen.
- **Rings are inventory groups.** Reuse Ansible's or Landscape's notion rather
  than inventing one; the guide says as much.
- **Collectors unprivileged, controllers root, and nothing resident.** A
  front-end that installs an agent on the Spark is departing from NVIDIA's
  model; one that fans out SSH from a central host is following it.
- **The Dashboard cannot be embedded, linked or proxied.** Loopback binding
  plus a hostname check in code. Its one useful export is the world-callable
  D-Bus snapshot, and only from on the box.
- **Landscape already does the boring half.** For a small fleet on the free
  Pro tier it provides pending-package views, scheduled upgrades and remote
  scripts today. A front-end's distinct value is the NVIDIA-specific half:
  OTA release detection, torn-score drilldown, firmware phase, ring gating
  with the checker as the post-condition.
- **Expect apt output, not an API.** Until the v2 tool ships, "what would
  change" is `apt-get -s full-upgrade` parsed off-box, exactly as the
  framework prescribes.

---

## 8. Measured state of this cluster

2026-09-04, all three nodes, unprivileged reads plus the root checker:

| node | apt-check total | security | reboot required | detected OTA | verdict |
|---|---|---|---|---|---|
| `sparky` | 210 | 133 | no | OTA2.2 (2026-03-31) | **July 2026 available** |
| `sparketa` | 152 | 117 | no | — | current |
| `sparkjr` | 149 | 116 | no | — | **July 2026 available** |

The pair that pools memory for distributed inference is split across two OTA
levels, which is exactly the drift a fleet view exists to make visible.

---

## 9. Sources

- [DGX Spark User Guide — DGX Dashboard](https://docs.nvidia.com/dgx/dgx-spark/dgx-dashboard.html)
- [DGX Spark User Guide — OS and Component Update](https://docs.nvidia.com/dgx/dgx-spark/os-and-component-update.html)
- [DGX Spark User Guide — Enterprise Manageability](https://docs.nvidia.com/dgx/dgx-spark/enterprise-manageability.html)
- [DGX Spark User Guide — Enterprise Lifecycle Integration](https://docs.nvidia.com/dgx/dgx-spark/enterprise-fleet-lifecycle.html)
- [DGX Spark Enterprise Manageability Guide (PDF)](https://docs.nvidia.com/pdf/enterprise-manageability-guide-dgx-spark.pdf)
- [Enterprise Lifecycle Integration Scripts (zip, 2026-05-20)](https://docscontent.nvidia.com/dc/04/5167e1c14532bac843d48d29bf36/enterprise-lifecycle-integration-scripts-20260520-1602.zip)
- [NVIDIA Technical Blog — Delivering Lifecycle Control for AI Infrastructure at Scale with DGX Spark Enterprise Manageability](https://developer.nvidia.com/blog/delivering-lifecycle-control-for-ai-infrastructure-at-scale-with-nvidia-dgx-spark-enterprise-manageability)
- [DGX Spark Release Notes](https://docs.nvidia.com/dgx/dgx-spark/release-notes.html)
- [Landscape docs — create a SaaS account and register your first client](https://ubuntu.com/landscape/docs/how-to-guides/landscape-installation-and-set-up/create-saas-account/)
- [Canonical — free personal Ubuntu Pro subscriptions for up to five machines](https://canonical.com/blog/ubuntu-pro-beta-release)
- On-node sources read directly: `/opt/nvidia/spark-ota-check/*.py` and `metadata/*.json`, `/etc/systemd/system/dgx-dashboard*.service`, `/etc/dbus-1/system.d/com.nvidia.dgx.dashboard.admin1.conf`, strings of `/opt/nvidia/dgx-dashboard-service/dashboard-service` and `/opt/nvidia/dgx-dashboard/dashboard-admin`, `/etc/apt/sources.list.d/`, `/etc/apt/preferences.d/`, `/etc/dgx-release`, `/usr/sbin/nvidia-spark-run-apt-upgrade-once.sh`.
