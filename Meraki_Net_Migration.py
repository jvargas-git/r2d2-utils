#!/usr/bin/env python3
"""
Meraki Network Template Migration Tool (Interactive)
=======================================================

Interactively moves a single network between Meraki configuration
templates while preserving its MX appliance VLAN configuration.

Why retainConfigs / restore matters
------------------------------------
Unbinding a network from a template can revert it to bare defaults unless
`retainConfigs=True` is passed. Binding to a *new* template can then layer
that template's own VLAN definitions on top. This script backs up the
network's VLANs first, unbinds with retainConfigs=True, binds to the new
template, then reconciles VLANs back to their pre-migration state -
creating any the new template didn't already define, updating any whose
attributes drifted, and reporting any extras the new template added.

JSON backup file
-----------------
Before touching anything, the network's VLAN configuration is written to
a JSON file named after the network (e.g. "Branch-Office-12.json") in the
meraki_vlan_backups/ folder. After a live migration, the script reads
that same file back from disk and compares it against the network's
current VLAN state, to confirm nothing was lost or changed unexpectedly.
The file is left on disk afterward so you can re-check it, or restore
from it by hand, at any point later.

Dry run
-------
Pass --dry-run to walk through org/network/template selection and see
exactly what would be backed up and which API calls would be made,
without unbinding, binding, or touching any VLAN. The backup JSON file
is still written in dry-run mode (it's just a local file read/write, not
an API call), but no live verification is run since nothing changed.
Note: because the network is never actually bound to the new template in
a dry run, the script can't preview which VLANs that template would
itself add - it can only show you what would be preserved from the
current network.

Usage
-----
    export MERAKI_DASHBOARD_API_KEY="your_api_key_here"

    python migrate_network.py              # live run
    python migrate_network.py --dry-run     # preview only, no changes
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

import meraki
from meraki.exceptions import APIError


def print_welcome(dry_run):
    print("=" * 60)
    print("Meraki Network Template Migration Tool")
    print("=" * 60)
    print("This will move one network to a new configuration template")
    print("while preserving its VLAN settings (backed up automatically")
    print(f"to ./{BACKUP_DIR}/<network-name>.json before anything changes).")
    print("\nYou'll be asked to pick an organization, a network, and a")
    print("destination template, then asked to confirm before anything runs.")
    print("Type 'q' at any prompt to quit.")
    print("\nFlags:")
    print("  --dry-run   Preview the migration without making any changes")
    print("  -h, --help  Show full usage and exit")
    if dry_run:
        print("\n[*] DRY-RUN mode: nothing will actually be changed.")
    print("=" * 60)

    
# Fields worth preserving on each VLAN. None values are dropped when
# capturing, so unset attributes don't get force-written back.
VLAN_FIELDS = [
    "name", "subnet", "applianceIp", "groupPolicyId", "dhcpHandling",
    "dhcpRelayServerIps", "dhcpLeaseTime", "dhcpBootOptionsEnabled",
    "dhcpBootNextServer", "dhcpBootFilename", "fixedIpAssignments",
    "reservedIpRanges", "dnsNameservers", "dhcpOptions", "ipv6",
]

MAX_LISTED_SUGGESTIONS = 30
BACKUP_DIR = "meraki_vlan_backups"


def parse_args():
    p = argparse.ArgumentParser(
        description="Interactively migrate a Meraki network between configuration "
                     "templates, preserving its VLAN configuration."
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Walk through selection and show what would happen without making any changes.",
    )
    return p.parse_args()


def get_dashboard():
    api_key = os.environ.get("MERAKI_DASHBOARD_API_KEY")
    if not api_key:
        sys.exit(
            "[!] Set the MERAKI_DASHBOARD_API_KEY environment variable first, e.g.\n"
            "    export MERAKI_DASHBOARD_API_KEY='your_api_key_here'"
        )
    return meraki.DashboardAPI(api_key, suppress_logging=True)


def prompt(msg):
    val = input(msg).strip()
    if val.lower() == "q":
        sys.exit(0)
    return val


def choose_org(dashboard):
    orgs = sorted(dashboard.organizations.getOrganizations(), key=lambda o: o["name"].lower())
    if not orgs:
        sys.exit("[!] No organizations are accessible with this API key.")

    print("\n--- Select an Organization ---")
    for i, org in enumerate(orgs, 1):
        print(f"[{i}] {org['name']}")

    while True:
        choice = prompt("Enter a number (or 'q' to quit): ")
        if choice.isdigit() and 1 <= int(choice) <= len(orgs):
            org = orgs[int(choice) - 1]
            return org["id"], org["name"]
        print(f"[!] Enter a number between 1 and {len(orgs)}.")


def find_by_name(items, name, label):
    """Case-insensitive exact match against a cached list of {'name': ...} dicts."""
    match = next((i for i in items if i["name"].strip().lower() == name.strip().lower()), None)
    if not match:
        names = sorted((i["name"] for i in items), key=str.lower)
        print(f"[!] No {label} named '{name}' found. Available {label}s:")
        for n in names[:MAX_LISTED_SUGGESTIONS]:
            print(f"    - {n}")
        if len(names) > MAX_LISTED_SUGGESTIONS:
            print(f"    ...and {len(names) - MAX_LISTED_SUGGESTIONS} more.")
    return match


def choose_network(dashboard, org_id, templates_by_id):
    networks = dashboard.organizations.getOrganizationNetworks(org_id, total_pages="all")
    while True:
        name = prompt("\nEnter the exact name of the network to migrate (or 'q' to quit): ")
        if not name:
            continue
        net = find_by_name(networks, name, "network")
        if net:
            tmpl_name = templates_by_id.get(net.get("configTemplateId"), "None (independent network)")
            print(f"[+] Found: {net['name']}  |  Current template: {tmpl_name}")
            return net


def choose_template(templates, current_template_id):
    while True:
        name = prompt("\nEnter the name of the destination template (or 'q' to quit): ")
        if not name:
            continue
        tmpl = find_by_name(templates, name, "template")
        if not tmpl:
            continue
        if tmpl["id"] == current_template_id:
            print(f"[!] This network is already bound to '{tmpl['name']}'. Choose a different template.")
            continue
        return tmpl


def backup_vlans(dashboard, network_id):
    """Returns None if VLANs are disabled (single-LAN mode) - nothing to preserve."""
    settings = dashboard.appliance.getNetworkApplianceVlansSettings(network_id)
    if not settings.get("vlansEnabled"):
        return None
    vlans = dashboard.appliance.getNetworkApplianceVlans(network_id)
    return {
        str(v["id"]): {k: v.get(k) for k in VLAN_FIELDS if v.get(k) is not None}
        for v in vlans
    }


def sanitize_filename(name):
    """Keeps a JSON backup filename filesystem-safe while staying readable."""
    cleaned = re.sub(r"[^\w\-. ]", "_", name).strip()
    return cleaned or "network"


def backup_file_path(network_name):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    return os.path.join(BACKUP_DIR, f"{sanitize_filename(network_name)}.json")


def write_backup_file(network, template, vlan_backup):
    """
    Writes the pre-migration VLAN state to disk, named after the network.
    Returns the path written to.
    """
    path = backup_file_path(network["name"])
    data = {
        "networkId": network["id"],
        "networkName": network["name"],
        "previousTemplateId": network.get("configTemplateId"),
        "newTemplateId": template["id"],
        "newTemplateName": template["name"],
        "backedUpAt": datetime.now(timezone.utc).isoformat(),
        "vlansEnabled": vlan_backup is not None,
        "vlans": vlan_backup or {},
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return path


def restore_vlans(dashboard, network_id, backup, dry_run):
    if dry_run:
        print("[dry-run] Would restore the following VLAN(s) to their pre-migration values:")
        for vlan_id, fields in sorted(backup.items(), key=lambda kv: int(kv[0])):
            print(f"    - VLAN {vlan_id} ({fields.get('name')})")
        print(
            "[dry-run] Note: the network is never actually bound to the new template in a "
            "dry run, so VLANs that template would itself add can't be previewed here."
        )
        return

    current = {str(v["id"]): v for v in dashboard.appliance.getNetworkApplianceVlans(network_id)}

    for vlan_id, fields in backup.items():
        try:
            if vlan_id in current:
                dashboard.appliance.updateNetworkApplianceVlan(network_id, vlan_id, **fields)
                print(f"    [+] Restored VLAN {vlan_id} ({fields.get('name')})")
            else:
                dashboard.appliance.createNetworkApplianceVlan(network_id, id=vlan_id, **fields)
                print(f"    [+] Re-created missing VLAN {vlan_id} ({fields.get('name')})")
        except APIError as e:
            print(f"    [-] Failed to restore VLAN {vlan_id}: {e}")

    extras = set(current) - set(backup)
    if extras:
        names = ", ".join(f"{vid} ({current[vid].get('name')})" for vid in sorted(extras, key=int))
        print(f"    [!] New template added VLAN(s) {names} that weren't present before migration; left in place.")


def verify_against_backup_file(dashboard, network_id, path):
    """
    Reads the JSON backup back from disk and compares it against the
    network's current VLAN state, to confirm the migration preserved
    everything that was backed up.
    """
    with open(path) as f:
        backup_data = json.load(f)

    if not backup_data.get("vlansEnabled"):
        print(f"[+] Verification: VLANs were disabled before migration (per {path}); nothing to verify.")
        return

    backup_vlans_data = backup_data["vlans"]
    current = {str(v["id"]): v for v in dashboard.appliance.getNetworkApplianceVlans(network_id)}

    missing, mismatches = [], []
    for vlan_id, fields in backup_vlans_data.items():
        if vlan_id not in current:
            missing.append(vlan_id)
            continue
        for key, expected in fields.items():
            if current[vlan_id].get(key) != expected:
                mismatches.append((vlan_id, key, expected, current[vlan_id].get(key)))

    if not missing and not mismatches:
        print(f"[+] Verification passed: all {len(backup_vlans_data)} VLAN(s) match {path}.")
        return

    if missing:
        print(f"[!] Verification: VLAN(s) missing after migration: {', '.join(missing)}")
    for vlan_id, key, expected, actual in mismatches:
        print(f"[!] Verification: VLAN {vlan_id} field '{key}' expected {expected!r}, got {actual!r}")


def migrate(dashboard, network, template, has_appliance, dry_run):
    network_id, network_name = network["id"], network["name"]
    vlan_backup = None
    backup_path = None

    if has_appliance:
        print(f"\n[*] Backing up VLAN configuration for '{network_name}'...")
        vlan_backup = backup_vlans(dashboard, network_id)
        if vlan_backup is None:
            print("[+] VLANs are disabled on this network (single-LAN mode); nothing to back up.")
        else:
            print(f"[+] Backed up {len(vlan_backup)} VLAN(s).")

        backup_path = write_backup_file(network, template, vlan_backup)
        print(f"[+] Backup saved to {backup_path}")

    if dry_run:
        print("[dry-run] Would unbind from current template (retainConfigs=True).")
    else:
        print("[*] Unbinding from current template...")
        dashboard.networks.unbindNetwork(network_id, retainConfigs=True)

    if dry_run:
        print(f"[dry-run] Would bind to '{template['name']}' (autoBind=False).")
    else:
        print(f"[*] Binding to '{template['name']}'...")
        dashboard.networks.bindNetwork(network_id, template["id"], autoBind=False)

    if vlan_backup:
        if not dry_run:
            print("[*] Restoring VLAN configuration over template defaults...")
        restore_vlans(dashboard, network_id, vlan_backup, dry_run)

    if not dry_run and has_appliance and backup_path:
        print("[*] Verifying migration against backup file...")
        verify_against_backup_file(dashboard, network_id, backup_path)

    if dry_run:
        print(f"\n[DRY RUN COMPLETE] No changes were made. '{network_name}' would be migrated to '{template['name']}'.")
    else:
        print(f"\n[SUCCESS] '{network_name}' migrated to '{template['name']}'.")


def main():
    args = parse_args()
    print_welcome(args.dry_run)
    dashboard = get_dashboard()

    org_id, org_name = choose_org(dashboard)
    print(f"[+] Working in org: {org_name}")

    templates = dashboard.organizations.getOrganizationConfigTemplates(org_id)
    templates_by_id = {t["id"]: t["name"] for t in templates}

    network = choose_network(dashboard, org_id, templates_by_id)
    template = choose_template(templates, network.get("configTemplateId"))

    print("\n" + "=" * 60)
    print("MIGRATION SUMMARY")
    print("=" * 60)
    print(f"Mode:         {'DRY RUN (no changes will be made)' if args.dry_run else 'LIVE'}")
    print(f"Organization: {org_name}")
    print(f"Network:      {network['name']}")
    print(f"From:         {templates_by_id.get(network.get('configTemplateId'), 'None (independent)')}")
    print(f"To:           {template['name']}")
    print("=" * 60)

    confirm_msg = "\nProceed with dry run? (y/N): " if args.dry_run else "\nProceed? (y/N): "
    if prompt(confirm_msg).lower() != "y":
        print("[*] Canceled.")
        return

    has_appliance = "appliance" in network.get("productTypes", [])
    migrate(dashboard, network, template, has_appliance, args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except APIError as e:
        sys.exit(f"\n[FATAL ERROR] Meraki API failed: {e}")
    except KeyboardInterrupt:
        sys.exit("\n[*] Canceled.")
