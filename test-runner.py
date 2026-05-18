#!/usr/bin/env python3
"""
SENSE Ansible test runner

Authors:
  Justas Balcas jbalcas (at) caltech.edu

Date: 2022/04/14

TODO:
    1. Log to file
    2. Log all ansible events
    3. Allow to test vlan and IP assignment

"""
import os
import json
import pprint
import yaml
import ansible_runner


def getInventory(inventoryFile):
    """Get inventory as dict"""
    with open(inventoryFile, 'r', encoding='utf-8') as fd:
        out = yaml.safe_load(fd.read())
    return out

def runAnsible(playbookFile):
    """Run Ansible Playbook"""
    ansRunner = ansible_runner.run(private_data_dir='/opt/siterm/config/ansible/sense',
                                   inventory=getInventory('/opt/siterm/config/ansible/sense/inventory/inventory.yaml'),
                                   playbook=playbookFile, verbosity=1000)
    return ansRunner

def saveOutput(inJson, filename, outputdir):
    """Save output json and config to file"""
    try:
        os.makedirs(os.path.join(outputdir, 'json/'), exist_ok = True)
        os.makedirs(os.path.join(outputdir, 'config/'), exist_ok = True)
    except OSError as ex:
        print(f"Directory '{outputdir}' can not be created")
        raise Exception(ex) from OSError
    # Dump Json
    fname = os.path.join(outputdir, "json/", f"{filename}.json" )
    with open(fname, 'w', encoding='utf-8') as fd:
        json.dump(inJson, fd)
    # Save config in txt format
    fname = os.path.join(outputdir, "config/", f"{filename}.config")
    with open(fname, 'w', encoding='utf-8') as fd:
        fd.writelines(inJson['ansible_net_config'])


outputdir="/opt/siterm/config/ansible/sense"
playbooks = ['getfacts.yaml', 'applyconfig.yaml']
for playbook in playbooks:
    print(f"RUNNING PLAYBOOK: {playbook}")
    r = runAnsible(playbook)
    for host, _ in r.stats['failures'].items():
        for host_events in r.host_events(host):
            if host_events['event'] != 'runner_on_failed':
                continue
            pprint.pprint(host_events)
    for host, _ in r.stats['ok'].items():
        print(f"HOSTNAME: {host}")
        print('-'*100)
        for host_events in r.host_events(host):
            if host_events['event'] != 'runner_on_ok':
                continue
            action = host_events['event_data']['task_action']
            print(action)
            if 'stdout_lines' in host_events['event_data']['res']:
                for line in host_events['event_data']['res']['stdout_lines']:
                    print(line)
            elif 'ansible_facts' in host_events['event_data']['res'] and  \
                 'ansible_net_interfaces' in host_events['event_data']['res']['ansible_facts']:
                pprint.pprint(host_events['event_data']['res']['ansible_facts'])
            elif 'ansible_facts_file' in host_events['event_data']['res'] and \
                 'file' in host_events['event_data']['res']['ansible_facts_file']:
                fname = host_events['event_data']['res']['ansible_facts_file']['file']
                if os.path.exists(fname):
                    try:
                        with open(fname, "r", encoding="utf-8") as f:
                            facts_data = json.load(f)
                            pprint.pprint(facts_data)
                    except json.JSONDecodeError as e:
                        print(f"[ERROR] Could not parse JSON from {fname}: {e}")
                else:
                    print(f"[WARNING] Facts file not found: {fname}")
            else:
                pprint.pprint(host_events)
            if playbook == 'getfacts.yaml':
                saveOutput(host_events['event_data']['res']['ansible_facts'], host, outputdir)
        print('-'*100)
