#!/usr/bin/env python3
"""
SENSE Ansible test runner

Modes:
  test-runner.py                                   Get facts/config from ALL devices (default)
  test-runner.py get [--host H] [--dry-run]         Get facts/config, optionally scoped to one host
  test-runner.py create-vlan --host H --vlan V
                  --members M [--members M2 ...]
                  [--description TEXT] [--dry-run]  Add a VLAN (+ optional tagged members) on one device
  test-runner.py delete-vlan --host H --vlan V
                  --members M [--members M2 ...]
                  [--dry-run]                       Remove a VLAN (+ optional tagged members) from one device

For create-vlan/delete-vlan, the script prepares `inventory_singleapply/`
(inventory.yaml + host_vars/<host>.yaml) from the host's current main
host_vars, with `interface` overridden to just the requested VLAN change,
then runs `applyconfig.yaml` against it -- unless --dry-run is given, in
which case only the Jinja2 template is rendered locally (no device contact,
no config pushed) and printed.

Authors:
  Justas Balcas jbalcas (at) caltech.edu

Date: 2022/04/14, rewritten 2026/08/19 for create-vlan/delete-vlan/--dry-run

TODO:
    1. Log to file
    2. Log all ansible events
"""
import argparse
import os
import pprint
import sys

import ansible_runner
import yaml
from jinja2 import Environment, FileSystemLoader

# ansible-runner's `verbosity` is passed straight through as an equivalent
# count of `-v` flags; ansible-core's own -v/--verbose argument
# (ansible/cli/arguments/option_helpers.py) is an argparse `count` action
# with no built-in ceiling, so there's no hard "max" defined by Ansible
# itself. 1000 is what this script has always used internally to mean
# "give me everything" and reliably surfaces every debug level Ansible
# and its connection plugins support, so it's used here as the practical
# max rather than an arbitrary small number.
DEFAULT_VERBOSITY = 0
MAX_VERBOSITY = 1000

ROOTDIR = "/opt/siterm/config/ansible/sense"
MAIN_INVENTORY = os.path.join(ROOTDIR, "inventory", "inventory.yaml")
MAIN_HOSTVARS_DIR = os.path.join(ROOTDIR, "inventory", "host_vars")
SINGLEAPPLY_DIR = os.path.join(ROOTDIR, "inventory_singleapply")
SINGLEAPPLY_INVENTORY = os.path.join(SINGLEAPPLY_DIR, "inventory.yaml")
SINGLEAPPLY_HOSTVARS_DIR = os.path.join(SINGLEAPPLY_DIR, "host_vars")
TEMPLATES_DIR = os.path.join(ROOTDIR, "project", "templates")
OUTPUTDIR = ROOTDIR


def readYaml(filename):
    """Read yaml file, return dict (empty dict if file missing/empty)"""
    if not os.path.isfile(filename):
        return {}
    with open(filename, "r", encoding="utf-8") as fd:
        return yaml.safe_load(fd.read()) or {}


def writeYaml(filename, data):
    """Write dict as yaml file"""
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    with open(filename, "w", encoding="utf-8") as fd:
        yaml.dump(data, fd, allow_unicode=True, default_flow_style=False,
                  explicit_start=True, width=1000)


def getMainInventory():
    """Load the main (all-hosts) inventory dict: {host: {ansible_host: ip, ...}}"""
    out = readYaml(MAIN_INVENTORY)
    return out.get("sense", {}).get("hosts", {})


def getHostVars(host):
    """Load current main host_vars for a host"""
    fname = os.path.join(MAIN_HOSTVARS_DIR, f"{host}.yaml")
    hostvars = readYaml(fname)
    if not hostvars:
        raise SystemExit(f"ERROR: no host_vars found for host '{host}' ({fname})")
    return hostvars


def vlanInterfaceName(hostvars, vlanid):
    """Compute the interface dict key/name for a VLAN on this host.

    Matches SiteFE's SiteFE/ProvisioningService/modules/VirtualSwitchingService.py
    ``_getdefaultVlan``: Junos MX/PTX virtual-switch mode uses `VLAN-<id>`,
    everything else uses `Vlan<id>`.
    """
    ansparams = hostvars.get("ansparams", {}) or {}
    network_os = hostvars.get("ansible_network_os", "")
    vlanmode = (ansparams.get("vlanmode") or "standard").lower()
    if network_os == "sense.junos.junos" and vlanmode in ("mx", "ptx"):
        return f"VLAN-{vlanid}"
    return f"Vlan{vlanid}"


def buildVlanInterfaceEntry(hostvars, vlanid, members, state, description):
    """Build the single `interface` dict entry for a create/delete-vlan request"""
    name = vlanInterfaceName(hostvars, vlanid)
    entry = {
        "name": name,
        "vlanid": vlanid,
        "description": description,
        "state": state,
    }
    if members:
        entry["tagged_members"] = {member: state for member in members}
    return name, entry


def prepareSingleApply(host, hostvars):
    """Write inventory_singleapply/{inventory.yaml,host_vars/<host>.yaml} for one host.

    Mirrors SiteFE/ProvisioningService/provisioningService.py's applyIndvConfig,
    which writes the same single-host inventory + host_vars pair before an
    on-demand apply.
    """
    mainInventory = getMainInventory()
    if host not in mainInventory:
        raise SystemExit(f"ERROR: host '{host}' not found in {MAIN_INVENTORY}")
    # Clear any stale per-host files from a previous run so nothing leaks in.
    if os.path.isdir(SINGLEAPPLY_HOSTVARS_DIR):
        for fname in os.listdir(SINGLEAPPLY_HOSTVARS_DIR):
            if fname.endswith(".yaml") and fname != "README.MD":
                os.remove(os.path.join(SINGLEAPPLY_HOSTVARS_DIR, fname))
    writeYaml(SINGLEAPPLY_INVENTORY, {"sense": {"hosts": {host: mainInventory[host]}}})
    writeYaml(os.path.join(SINGLEAPPLY_HOSTVARS_DIR, f"{host}.yaml"), hostvars)


def renderDryRun(hostvars, templateKey):
    """Render the host's Jinja2 template locally with hostvars, no device contact"""
    templateName = hostvars.get(templateKey, "")
    if not templateName:
        return f"[no {templateKey} defined for this host, nothing to render]"
    templatePath = os.path.join(TEMPLATES_DIR, templateName)
    with open(templatePath, "r", encoding="utf-8") as fd:
        content = fd.read()
    # Ansible's `template` module strips a leading `#jinja2: ...` override line
    # before rendering; replicate that so dry-run output matches the real push.
    lines = content.splitlines(keepends=True)
    if lines and lines[0].startswith("#jinja2:"):
        content = "".join(lines[1:])
    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), trim_blocks=True, lstrip_blocks=True)
    template = env.from_string(content)
    return template.render(**hostvars).strip("\n")


def saveOutput(inJson, filename, outputdir):
    """Save output json and config to file"""
    try:
        os.makedirs(os.path.join(outputdir, "json/"), exist_ok=True)
        os.makedirs(os.path.join(outputdir, "config/"), exist_ok=True)
    except OSError as ex:
        print(f"Directory '{outputdir}' can not be created")
        raise Exception(ex) from OSError
    fname = os.path.join(outputdir, "json/", f"{filename}.json")
    with open(fname, "w", encoding="utf-8") as fd:
        import json
        json.dump(inJson, fd)
    fname = os.path.join(outputdir, "config/", f"{filename}.config")
    with open(fname, "w", encoding="utf-8") as fd:
        fd.writelines(inJson.get("ansible_net_config", ""))


def runAnsible(playbook, inventory, host=None, saveFacts=False, verbosity=DEFAULT_VERBOSITY):
    """Run an Ansible Playbook against `inventory` (path or dict), optionally --limit host"""
    kwargs = {
        "private_data_dir": ROOTDIR,
        "inventory": inventory,
        "playbook": playbook,
        "verbosity": verbosity,
    }
    if host:
        kwargs["limit"] = host
    r = ansible_runner.run(**kwargs)
    for evtHost, _ in r.stats.get("failures", {}).items():
        for host_events in r.host_events(evtHost):
            if host_events["event"] != "runner_on_failed":
                continue
            pprint.pprint(host_events)
    for evtHost, _ in r.stats.get("ok", {}).items():
        print(f"HOSTNAME: {evtHost}")
        print("-" * 100)
        for host_events in r.host_events(evtHost):
            if host_events["event"] != "runner_on_ok":
                continue
            action = host_events["event_data"]["task_action"]
            print(action)
            res = host_events["event_data"]["res"]
            if "stdout_lines" in res:
                for line in res["stdout_lines"]:
                    print(line)
            elif "ansible_facts" in res and "ansible_net_interfaces" in res["ansible_facts"]:
                pprint.pprint(res["ansible_facts"])
                if saveFacts:
                    saveOutput(res["ansible_facts"], evtHost, OUTPUTDIR)
            elif "ansible_facts_file" in res and "file" in res["ansible_facts_file"]:
                fname = res["ansible_facts_file"]["file"]
                if os.path.exists(fname):
                    try:
                        with open(fname, "r", encoding="utf-8") as f:
                            import json
                            pprint.pprint(json.load(f))
                    except Exception as ex:
                        print(f"[ERROR] Could not parse JSON from {fname}: {ex}")
                else:
                    print(f"[WARNING] Facts file not found: {fname}")
            else:
                pprint.pprint(host_events)
        print("-" * 100)
    return r


def doGet(host, dryRun, verbosity):
    """Get configuration/facts. Default (no --host): all devices."""
    if dryRun:
        hosts = [host] if host else list(getMainInventory().keys())
        print(f"[DRY-RUN] Would run getfacts.yaml against: {', '.join(hosts)}")
        for h in hosts:
            hostvars = getHostVars(h)
            print(f"--- {h} (network_os={hostvars.get('ansible_network_os')}, "
                  f"ansparams={hostvars.get('ansparams')}) ---")
        return
    runAnsible("getfacts.yaml", MAIN_INVENTORY, host=host, saveFacts=True, verbosity=verbosity)


def doVlan(action, host, vlanid, members, description, dryRun, verbosity):
    """create-vlan / delete-vlan"""
    state = "present" if action == "create-vlan" else "absent"
    hostvars = getHostVars(host)
    name, entry = buildVlanInterfaceEntry(hostvars, vlanid, members, state, description)
    hostvars = dict(hostvars)
    hostvars["interface"] = {name: entry}

    print(f"{action}: host={host} vlan={vlanid} name={name} members={members or '[]'}")

    if dryRun:
        rendered = renderDryRun(hostvars, "template_name")
        print("[DRY-RUN] Rendered config (NOT pushed to device):")
        print(rendered)
        return

    prepareSingleApply(host, hostvars)
    runAnsible("applyconfig.yaml", SINGLEAPPLY_INVENTORY, host=host, verbosity=verbosity)


def verbosityType(value):
    """argparse type= for --verbosity: int, clamped to [0, MAX_VERBOSITY]"""
    try:
        ivalue = int(value)
    except ValueError as ex:
        raise argparse.ArgumentTypeError(f"'{value}' is not an integer") from ex
    if ivalue < 0 or ivalue > MAX_VERBOSITY:
        raise argparse.ArgumentTypeError(
            f"--verbosity must be between 0 and {MAX_VERBOSITY} (got {ivalue})"
        )
    return ivalue


def addVerbosityArg(subparser):
    subparser.add_argument(
        "--verbosity", type=verbosityType, default=DEFAULT_VERBOSITY,
        help=f"Ansible verbosity, like repeating -v (0=normal default, up to {MAX_VERBOSITY})",
    )


def parseArgs():
    parser = argparse.ArgumentParser(description="SENSE Ansible test runner")
    sub = parser.add_subparsers(dest="action")

    getP = sub.add_parser("get", help="Get configuration/facts from device(s) [default]")
    getP.add_argument("--host", default=None, help="Limit to a single host (default: all hosts)")
    getP.add_argument("--dry-run", action="store_true", dest="dry_run",
                       help="Do not contact devices; just show what would run")
    addVerbosityArg(getP)

    createP = sub.add_parser("create-vlan", help="Create/add a VLAN on one device")
    createP.add_argument("--host", required=True)
    createP.add_argument("--vlan", required=True, type=int, dest="vlanid")
    createP.add_argument("--members", action="append", default=[],
                          help="Tagged member interface to add (repeatable)")
    createP.add_argument("--description", default="SENSE-TEST-VLAN")
    createP.add_argument("--dry-run", action="store_true", dest="dry_run",
                          help="Render the config only; do not push to the device")
    addVerbosityArg(createP)

    deleteP = sub.add_parser("delete-vlan", help="Delete/remove a VLAN from one device")
    deleteP.add_argument("--host", required=True)
    deleteP.add_argument("--vlan", required=True, type=int, dest="vlanid")
    deleteP.add_argument("--members", action="append", default=[],
                          help="Tagged member interface to remove (repeatable)")
    deleteP.add_argument("--dry-run", action="store_true", dest="dry_run",
                          help="Render the config only; do not push to the device")
    addVerbosityArg(deleteP)

    args = parser.parse_args()
    if args.action is None:
        # Default: get configuration of all devices.
        args.action = "get"
        args.host = None
        args.dry_run = False
        args.verbosity = DEFAULT_VERBOSITY
    return args


def main():
    args = parseArgs()
    if args.action == "get":
        doGet(args.host, args.dry_run, args.verbosity)
    elif args.action in ("create-vlan", "delete-vlan"):
        doVlan(args.action, args.host, args.vlanid, args.members,
               getattr(args, "description", ""), args.dry_run, args.verbosity)
    else:
        print(f"Unknown action: {args.action}")
        sys.exit(1)


if __name__ == "__main__":
    main()
