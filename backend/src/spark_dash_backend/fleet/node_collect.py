# Runs ON THE SPARK, piped to `python3 -` over ssh. Stdlib only, nothing written,
# nothing installed. Prints one JSON document with every fact the controller
# needs. Unprivileged apart from the two commands NVIDIA's firmware scoring
# requires (dmidecode, mstflint) and reading the Dashboard's settings file;
# those use `sudo -n` and come back empty when not permitted.
#
# The controller prepends a line `OPTIONS = {...}` before piping.
import glob
import hashlib
import json
import os
import subprocess

try:
    OPTIONS
except NameError:
    OPTIONS = {}

out = {"errors": {}}

def sh(name, argv, timeout=60, sudo=False, ok_rc=(0,)):
    if sudo:
        argv = ["sudo", "-n"] + argv
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if p.returncode not in ok_rc:
            out["errors"][name] = f"rc={p.returncode} {p.stderr.strip()[:200]}"
        return p.stdout
    except (OSError, subprocess.SubprocessError) as e:
        out["errors"][name] = str(e)[:200]
        return ""

def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""

out["hostname"] = read("/etc/hostname").strip()
out["boot_id"] = read("/proc/sys/kernel/random/boot_id").strip()
out["board_vendor"] = read("/sys/class/dmi/id/board_vendor").strip()
# which vendor bundle the platform firmware is on: the BIOS version string carries
# it on partner boards (ASUS: GX10DGX.0105.…), and all three are world-readable
out["product_name"] = read("/sys/class/dmi/id/product_name").strip()
out["bios_version"] = read("/sys/class/dmi/id/bios_version").strip()
out["bios_date"] = read("/sys/class/dmi/id/bios_date").strip()
out["kernel"] = sh("uname", ["uname", "-r"]).strip()
drv = ""
for line in sh("nvidia-smi", ["nvidia-smi", "-q"], timeout=30).splitlines():
    if "Driver Version" in line:
        drv = line.split(":")[-1].strip(); break
out["driver"] = drv
out["dgx_release"] = read("/etc/dgx-release")

out["packages"] = sh("dpkg-query", ["dpkg-query", "-W", "-f", "${Package},${Version}\n"])
out["fwupd_devices"] = sh("fwupd-devices", ["fwupdmgr", "get-devices", "--json"], timeout=60)
# rc 2 = "no updates available", which is an answer, not an error
out["fwupd_updates"] = sh("fwupd-updates", ["fwupdmgr", "get-updates", "--json"], timeout=60, ok_rc=(0, 2))

out["dmidecode_t45"] = sh("dmidecode", ["dmidecode", "-t", "45"], sudo=True)
mlnx = ""
for line in sh("lspci", ["lspci"]).splitlines():
    if "Ethernet controller: Mellanox" in line:
        mlnx = line.split()[0]; break
out["lspci_mlnx"] = mlnx
out["mstflint"] = sh("mstflint", ["mstflint", "-d", mlnx, "q"], sudo=True) if mlnx else ""

out["apt_check"] = sh("apt-check", ["/usr/lib/update-notifier/apt-check"]).strip()
# apt-check prints to stderr on some versions; try that too
if not out["apt_check"]:
    try:
        p = subprocess.run(["/usr/lib/update-notifier/apt-check"], capture_output=True, text=True, timeout=60)
        out["apt_check"] = (p.stdout or p.stderr).strip()
    except Exception as e:
        out["errors"]["apt-check"] = str(e)[:200]
try:
    out["updates_available_mtime"] = int(os.stat("/var/lib/update-notifier/updates-available").st_mtime)
except OSError:
    out["updates_available_mtime"] = None

out["reboot_required"] = os.path.exists("/run/reboot-required")
out["reboot_required_pkgs"] = [l for l in read("/run/reboot-required.pkgs").splitlines() if l.strip()]

# what full-upgrade would do; a simulation needs no lock and no root
out["apt_sim"] = sh("apt-sim", ["apt-get", "-s", "-o", "Debug::NoLocking=1", "full-upgrade"], timeout=120)

# Packages a person pinned by hand. They do not appear in the simulation at
# all -- apt leaves a held package out of the plan and mentions it under "kept
# back" if anything asked for it -- so without this the record cannot tell
# "nothing to install" from "nothing it is allowed to install".
_holds = sh("apt-holds", ["apt-mark", "showhold"]).split()
out["apt_holds"] = sorted(_holds)

out["dbus_snapshot"] = sh("dbus", ["busctl", "call", "com.nvidia.dgx.dashboard.admin1",
                                   "/com/nvidia/dgx/dashboard/admin", "com.nvidia.dgx.dashboard.admin1",
                                   "GetOTAAvailabilitySnapshot"], timeout=20).strip()
out["dashboard_admin_active"] = sh("admin-active", ["systemctl", "is-active", "dgx-dashboard-admin.service"], ok_rc=(0, 3)).strip() == "active"
settings = sh("settings", ["cat", "/opt/nvidia/dgx-dashboard/settings.json"], ok_rc=(0, 1))
if not settings.strip():
    settings = sh("settings", ["cat", "/opt/nvidia/dgx-dashboard/settings.json"], sudo=True, ok_rc=(0, 1))
out["errors"].pop("settings", None)
# privilege posture: can the controller act unattended, and could it read firmware?
out["sudo_passwordless"] = subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0
out["firmware_readable"] = bool(out["dmidecode_t45"].strip())
auto = True
try:
    d = json.loads(settings) if settings.strip() else {}
    if isinstance(d, dict) and isinstance(d.get("update"), dict) and d["update"].get("enabled") is False:
        auto = False
except ValueError:
    pass
out["dashboard_auto_update"] = auto

out["inhibitors"] = sh("inhibit", ["systemd-inhibit", "--list", "--no-pager", "--no-legend"], ok_rc=(0, 1))
try:
    st = os.statvfs("/")
    out["disk_free_bytes"] = st.f_bavail * st.f_frsize
except OSError:
    out["disk_free_bytes"] = None
out["dpkg_lock_held"] = subprocess.run(["fuser", "/var/lib/dpkg/lock-frontend"], capture_output=True).returncode == 0

recipes = {}
for p in sorted(glob.glob("/opt/nvidia/spark-ota-check/metadata/spark-ota-*.json")):
    recipes[os.path.basename(p)] = hashlib.md5(read(p).encode()).hexdigest()
out["recipes"] = recipes
if OPTIONS.get("recipes"):
    out["recipe_files"] = {os.path.basename(p): read(p)
                           for p in glob.glob("/opt/nvidia/spark-ota-check/metadata/spark-ota-*.json")}
# the fabric: ConnectX-7 ports, who they see over LLDP, and the ARP table as a
# fallback — enough to detect which Sparks are cabled to each other
fabric = {"macs": [], "ports": [], "lldp": [], "neigh": []}
for n in sorted(glob.glob("/sys/class/net/*")):
    ifn = os.path.basename(n)
    mac = read(n + "/address").strip()
    if mac and mac != "00:00:00:00:00:00":
        fabric["macs"].append(mac)
    if read(n + "/device/vendor").strip() == "0x15b3":
        try:
            speed = int(read(n + "/speed").strip() or 0)
        except ValueError:
            speed = 0
        fabric["ports"].append({"ifname": ifn, "mac": mac, "carrier": read(n + "/carrier").strip() == "1",
                                "operstate": read(n + "/operstate").strip(), "speed_mbps": max(speed, 0)})
cx_ifs = {p_["ifname"] for p_ in fabric["ports"]}
try:
    lldp = json.loads(sh("lldp", ["lldpcli", "show", "neighbors", "-f", "json"], timeout=20, ok_rc=(0, 1)) or "{}")
    ifaces = lldp.get("lldp", {}).get("interface", [])
    if isinstance(ifaces, dict):
        ifaces = [ifaces]
    for entry in ifaces:
        for ifn, v in entry.items():
            if ifn not in cx_ifs:
                continue
            ch = v.get("chassis", {}) or {}
            cname = next(iter(ch), "")
            c = ch.get(cname, {}) if cname else {}
            mgmt = c.get("mgmt-ip", [])
            fabric["lldp"].append({"ifname": ifn, "chassis_name": cname,
                                   "chassis_id": (c.get("id") or {}).get("value", ""),
                                   "port_id": (v.get("port", {}).get("id") or {}).get("value", ""),
                                   "port_descr": v.get("port", {}).get("descr", ""),
                                   "mgmt_ip": mgmt if isinstance(mgmt, list) else [mgmt]})
except (ValueError, AttributeError):
    out["errors"]["lldp"] = "unparseable"
out["errors"].pop("lldp", None) if not fabric["lldp"] and "lldp" in out["errors"] else None
for ifn in sorted(cx_ifs):
    try:
        for e in json.loads(sh("neigh", ["ip", "-j", "neigh", "show", "dev", ifn], timeout=10) or "[]"):
            if e.get("lladdr"):
                fabric["neigh"].append({"ifname": ifn, "lladdr": e["lladdr"], "dst": e.get("dst", ""),
                                        "state": (e.get("state") or [""])[0]})
    except ValueError:
        pass
out["fabric"] = fabric

out["ota_check_version"] = ""
for line in out["packages"].splitlines():
    if line.startswith("nvidia-spark-ota-check,"):
        out["ota_check_version"] = line.split(",", 1)[1]; break

print(json.dumps(out))
