# Manually testing VLAN add/remove via `test-runner.py` / Ansible

This is a runbook for manually pushing a throwaway VLAN to a real switch/router
through the same code path SENSE FE uses (`host_vars` -> Jinja template ->
`applyconfig.yaml`/`getfacts.yaml`), without going through an actual SENSE
delta request. Useful for validating a new device, a new `vlanmode`, or a
template change before trusting it with production circuits.

**This pushes live, saved config to real hardware.** Always:
- Scope every run to a single host with `--limit <host>` (or `test-runner.py`'s
  own inventory if you truly want all hosts — normally you don't).
- Use an obviously-fake VLAN ID/description (e.g. `SENSE-TEST-DELETE-ME`) that
  won't collide with anything real.
- Render the template first without pushing (see step 2) and read it before
  you commit to a live push.
- Remove the test VLAN and restore `host_vars` to its original content when
  you're done (step 5). Don't leave test config on production devices.

## 0. Where things live

Inside the FE container (`kubectl exec` in), everything is under
`/opt/siterm/config/ansible/sense`:

| Path | What it is |
|---|---|
| `inventory/inventory.yaml` | List of hosts + `ansible_host` IP |
| `inventory/host_vars/<host>.yaml` | Per-host vars: creds, `template_name`, `ansparams`, and the `interface` dict that drives VLAN config |
| `project/getfacts.yaml` | Read-only: gathers current device state (`show ... | display json`) |
| `project/applyconfig.yaml` | Writes: renders `template_name` and pushes/saves it to the device |
| `project/templates/<vendor>.j2` | Jinja templates that turn `interface` into vendor CLI commands |
| `test-runner.py` | Convenience wrapper that runs `getfacts.yaml` then `applyconfig.yaml` against **every** host in the inventory and pretty-prints the result |

`test-runner.py` is fine for a full run, but for manual single-host testing
prefer calling `ansible-playbook` directly with `--limit` — it's the same
playbooks, but scoped and with normal verbose output instead of
`test-runner.py`'s custom pretty-printer.

## 1. Edit `host_vars/<host>.yaml` — add the VLAN

The `interface` dict is keyed by VLAN name, and its shape is the same
regardless of vendor; the template picks the right CLI syntax per
`ansible_network_os` / `ansparams.vlanmode`.

### Standard mode (most vendors, or Junos EX/QFX/SRX without virtual-switch)

```yaml
interface:
  Vlan3919:
    name: Vlan3919
    vlanid: 3919
    description: "SENSE-TEST-DELETE-ME"
    state: present
    # optional — attach a real tagged port (only add this once you're sure
    # the interface is safe to touch, see warning below):
    tagged_members:
      et-0/0/11: present
    # optional — L3 address on the VLAN/IRB interface:
    ipv4_address:
      192.0.2.1/30: present
    ipv6_address:
      "2001:db8::1/64": present
```

### MX / PTX mode (`ansparams.vlanmode: mx` or `ptx`, virtual-switch routing-instance)

Name **must** be `VLAN-<id>` (uppercase, hyphen) in this mode — that's what
the template keys off of:

```yaml
interface:
  VLAN-3919:
    name: VLAN-3919
    vlanid: 3919
    description: "SENSE-TEST-DELETE-ME"
    state: present
    # optional — attach a real interface (creates it as a vlan-bridge
    # sub-unit under the routing-instance):
    tagged_members:
      ae4: present
```

Leaving `tagged_members` out is the safest test: it creates the
bridge-domain/vlan shell inside the routing-instance with **no interface
attached**, so there's zero traffic impact — good enough to prove the
plumbing (host_vars -> ansparams -> template -> push -> facts) works
end-to-end before you risk touching a real port.

To edit the file directly in the running pod:

```bash
kubectl exec -n <namespace> <pod> -- python3 - <<'PYEOF'
import yaml
p = "/opt/siterm/config/ansible/sense/inventory/host_vars/<host>.yaml"
with open(p) as f:
    d = yaml.safe_load(f)
d["interface"] = {
    "VLAN-3919": {
        "name": "VLAN-3919",
        "vlanid": 3919,
        "description": "SENSE-TEST-DELETE-ME",
        "state": "present",
    }
}
with open(p, "w") as f:
    yaml.dump(d, f, default_flow_style=False, explicit_start=True, width=1000)
PYEOF
```

## 2. Render only, no push (sanity check first)

Before touching the device, render the template to `/var/tmp` and read it.
A throwaway single-task playbook does this safely (`connection: local`, no
device contact):

```bash
cat > /tmp/render_only.yaml <<'EOF'
---
- name: Render config template only, no push
  hosts: <host>
  connection: local
  gather_facts: no
  tasks:
    - name: Build Config Template
      template:
        src: "{{ template_name }}"
        dest: /var/tmp/{{ inventory_hostname }}.conf
EOF
kubectl cp /tmp/render_only.yaml <namespace>/<pod>:/opt/siterm/config/ansible/sense/project/render_only.yaml
kubectl exec -n <namespace> <pod> -- sh -c \
  'cd /opt/siterm/config/ansible/sense && ansible-playbook -i inventory/inventory.yaml project/render_only.yaml'
kubectl exec -n <namespace> <pod> -- cat /var/tmp/<host>.conf
kubectl exec -n <namespace> <pod> -- rm -f /opt/siterm/config/ansible/sense/project/render_only.yaml
```

Read the output. For the MX example above you should see exactly:

```
set routing-instances SENSE-Vlans instance-type virtual-switch
set routing-instances SENSE-Vlans bridge-domains VLAN-3919 description "SENSE-TEST-DELETE-ME"
set routing-instances SENSE-Vlans bridge-domains VLAN-3919 vlan-id 3919
```

If that doesn't look right, fix `host_vars`/the template and re-render —
don't push yet.

## 3. Push it for real

Once the rendered config looks correct, run the actual playbook,
**scoped to one host**:

```bash
kubectl exec -n <namespace> <pod> -- sh -c \
  'cd /opt/siterm/config/ansible/sense && ansible-playbook -i inventory/inventory.yaml project/applyconfig.yaml --limit <host> -v'
```

Look for `"changed": true, "saved": true` in the `Push Juniper Junos Config`
(or equivalent vendor) task result, listing the exact `commands` sent.

Equivalently, `test-runner.py` does this (plus `getfacts.yaml` first) for
**every** host in the inventory — only use it unscoped if that's actually
what you want:

```bash
kubectl exec -n <namespace> <pod> -- python3 /opt/siterm/config/ansible/sense/test-runner.py
```

## 4. Verify it landed — both directions

Don't just trust the push result; confirm the *read* path also sees it
correctly. This exercises the same `getfacts.yaml` / vendor `_facts` module
that SENSE FE relies on for drift detection, so it's the real test of
whether the parser understands this device's actual output shape (not just
whether the push command was accepted):

```bash
kubectl exec -n <namespace> <pod> -- sh -c \
  'cd /opt/siterm/config/ansible/sense && ansible-playbook -i inventory/inventory.yaml project/getfacts.yaml --limit <host> -v' \
  | grep -A3 '"VLAN-3919"'
```

You should see the VLAN show up in `ansible_net_interfaces` (or the
vendor-equivalent fact key), e.g. `"VLAN-3919": {"mtu": 1500}` for MX mode.
If it's missing or the module logs a `failed to parse_vlans` warning, the
push succeeded but the facts parser doesn't understand this device's
response — that's a bug in the `_facts` module's parser, not in the config
push.

## 5. Cancel / remove the test VLAN

Flip `state` to `absent` (keep everything else the same — the template needs
`vlanid`/`name` to know what to delete) and push again:

```bash
kubectl exec -n <namespace> <pod> -- python3 - <<'PYEOF'
import yaml
p = "/opt/siterm/config/ansible/sense/inventory/host_vars/<host>.yaml"
with open(p) as f:
    d = yaml.safe_load(f)
d["interface"] = {
    "VLAN-3919": {
        "name": "VLAN-3919",
        "vlanid": 3919,
        "description": "SENSE-TEST-DELETE-ME",
        "state": "absent",
    }
}
with open(p, "w") as f:
    yaml.dump(d, f, default_flow_style=False, explicit_start=True, width=1000)
PYEOF

kubectl exec -n <namespace> <pod> -- sh -c \
  'cd /opt/siterm/config/ansible/sense && ansible-playbook -i inventory/inventory.yaml project/applyconfig.yaml --limit <host> -v'
```

Look for the `delete ...` command(s) in the task output, then re-run step 4's
`getfacts.yaml` check to confirm the VLAN is gone from facts too.

## 6. Restore `host_vars` to its original state

The static `host_vars/<host>.yaml` on disk isn't meant to carry test data —
real SENSE delta applies overwrite `interface` with the actual active-service
state each time, but if nothing else touches this host in the meantime your
edits will just sit there. Put it back the way you found it (normally
`interface: {}` unless the host genuinely has active SENSE-managed VLANs
right now — check before you overwrite):

```bash
kubectl exec -n <namespace> <pod> -- python3 - <<'PYEOF'
import yaml
p = "/opt/siterm/config/ansible/sense/inventory/host_vars/<host>.yaml"
with open(p) as f:
    d = yaml.safe_load(f)
d["interface"] = {}
with open(p, "w") as f:
    yaml.dump(d, f, default_flow_style=False, explicit_start=True, width=1000)
PYEOF
```

## Common `ansparams` you may need to set for step 1 to matter

These live in `host_vars/<host>.yaml` alongside `interface`, and the Junos
templates/modules read them (`ansparams` undefined -> silently defaults to
`vlanmode: standard`):

```yaml
ansparams:
  vlanmode: mx            # standard | mx | ptx
  vlanip: irb              # vlan | irb (standard mode L3 interface type)
  routing_instance: SENSE-Vlans   # only used when vlanmode is mx/ptx
```

If `ansparams` is missing entirely for a host that should be MX/PTX, check
that:
1. The rm-config site has `ansible_params: {vlanmode: mx}` on that switch.
2. `/etc/ansible-conf.yaml` inside the pod has `ansparams:` for that host
   (FE-generated — if missing here, it's a FE-side bug).
3. `ansible-prepare.py` actually copies `ansparams` from
   `/etc/ansible-conf.yaml` into `host_vars/<host>.yaml` (this is the step
   that silently dropped it until it was fixed in `siterm-startup`).
