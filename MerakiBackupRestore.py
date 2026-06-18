import os
import sys
import json
import logging
import re
import argparse
import base64
from datetime import datetime
import urllib.request
import urllib.error
import meraki

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Configuration Constants
API_KEY = os.environ.get("MERAKI_KEY", "your_api_key_here")  # Replace with your actual API key or set as environment variable
BACKUP_DIR = "./meraki_template_backups"

# GitHub Configuration
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")  # Expected format: "owner/repo"
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

def print_welcome_screen():
    """Prints a clear user guide and operational map for interactive execution sessions."""
    banner = "=" * 70
    print(banner)
    print("                 MERAKI DISASTER RECOVERY TOOLKIT                    ")
    print(banner)
    print("This automation script processes, archives, and restores entire Cisco")
    print("Meraki organization templates, global settings, and networks.\n")
    print("OPERATIONAL MODES:")
    print("  [B] Backup Mode  - Extracts global policy objects/groups, template maps,")
    print("                     SSIDs, switch profiles, and bound network overrides.")
    print("                     Saves to timestamped JSON payload archives.")
    print("  [R] Restore Mode - Restores configurations back to live structures.")
    print("                     Automatically calculates ID transformation matrices")
    print("                     and supports granular single-network targeting.\n")
    print("CLI FLAGS & PARAMETERS:")
    print("  -B, --backup     - Triggers the backup routine for templates & networks.")
    print("  -R, --restore    - Triggers the restoration engine from local backups.")
    print("  -A, --all        - Runs automatically across all matching properties.")
    print("                     Bypasses interactive prompts and this welcome screen.")
    print("  -O, --orgname    - Specifies the exact target Meraki Organization Name.")
    print("  -N, --network    - Isolates restoration strictly to a single named network")
    print("                     by parsing all files to auto-locate its backup layer.")
    print("  -G, --github     - Routes backup payloads directly to a remote GitHub")
    print("                     repository instead of local disk storage.\n")
    print("CONFIGURATION & PATHS:")
    print(f"  Target Local Directory   : {os.path.abspath(BACKUP_DIR)}")
    print(f"  API Key Detected         : {'YES (From Environment)' if os.environ.get('MERAKI_KEY') else 'NO (Using code default)'}")
    print(f"  GitHub Export Enabled    : {'READY' if GITHUB_TOKEN and GITHUB_REPO else 'NO (Missing GITHUB_TOKEN or GITHUB_REPO)'}")
    print(banner)
    print("Version 1.1\n")

def sanitize_filename(name):
    """Removes special characters to create a safe filename."""
    return re.sub(r'[^a-zA-Z0-9_-]', '_', name)

def upload_to_github(filename, content_str):
    """Uploads the JSON configuration payload directly to the specified GitHub repository."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        logging.error("GitHub upload failed: GITHUB_TOKEN and GITHUB_REPO environment variables must be set.")
        return False

    logging.info(f"Uploading backup archive '{filename}' to GitHub repo '{GITHUB_REPO}'...")
    
    # Clean up filename for repo path structure if needed
    repo_path = f"backups/{filename}"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{repo_path}"
    
    # Base64 encode file contents as required by GitHub API
    content_bytes = content_str.encode('utf-8')
    base64_content = base64.b64encode(content_bytes).decode('utf-8')
    
    payload = {
        "message": f"Automated Meraki DR Backup - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "content": base64_content,
        "branch": GITHUB_BRANCH
    }
    
    req_data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=req_data, method='PUT')
    req.add_header('Authorization', f'token {GITHUB_TOKEN}')
    req.add_header('Accept', 'application/vnd.github.v3+json')
    req.add_header('Content-Type', 'application/json')
    
    try:
        with urllib.request.urlopen(req) as response:
            if response.status in [200, 201]:
                logging.info(f"SUCCESS: Securely pushed to GitHub branch '{GITHUB_BRANCH}' at path: {repo_path}")
                return True
    except urllib.error.HTTPError as e:
        logging.error(f"GitHub API Error ({e.code}): {e.read().decode('utf-8')}")
    except Exception as e:
        logging.error(f"Failed to connect to GitHub endpoint: {e}")
    return False

def get_org_id_by_name(dashboard, org_name):
    """Resolves an Organization Name to its unique numeric Organization ID."""
    logging.info(f"Resolving Organization ID for name: '{org_name}'...")
    try:
        organizations = dashboard.organizations.getOrganizations()
        matched_orgs = [org for org in organizations if org['name'].lower() == org_name.lower()]
        
        if not matched_orgs:
            logging.error(f"CRITICAL: No organization named '{org_name}' found. Verify your API key permissions.")
            sys.exit(1)
        elif len(matched_orgs) > 1:
            logging.error(f"CRITICAL: Multiple organizations found matching '{org_name}'. Please make sure the name is unique.")
            sys.exit(1)
            
        logging.info(f"Successfully matched '{org_name}' to ID: {matched_orgs[0]['id']}")
        return matched_orgs[0]['id']
        
    except meraki.APIError as e:
        logging.error(f"Failed to query organizations via Meraki API: {e}")
        sys.exit(1)

def run_backup(dashboard, org_id, target_template_id=None, use_github=False):
    """Backs up ALL configuration settings including Global Policy Objects for DR recovery."""
    logging.info(f"Starting worst-case data backup routine for Organization ID: {org_id}")
    if not use_github and not os.path.exists(BACKUP_DIR):
        os.makedirs(BACKUP_DIR)
        
    try:
        templates = dashboard.organizations.getOrganizationConfigTemplates(org_id)
        all_networks = dashboard.organizations.getOrganizationNetworks(org_id)
        
        global_policy_objects = []
        global_policy_groups = []
        logging.info("  -> Archiving Global Organization Policy Objects and Groups catalog...")
        try:
            global_policy_objects = dashboard.organizations.getOrganizationPolicyObjects(org_id, total_pages='all')
        except meraki.APIError as e:
            logging.warning(f"    [!] Could not back up global Policy Objects: {e}")

        try:
            global_policy_groups = dashboard.organizations.getOrganizationPolicyObjectsGroups(org_id, total_pages='all')
        except meraki.APIError as e:
            logging.warning(f"    [!] Could not back up global Policy Object Groups: {e}")
        
        if target_template_id:
            templates = [t for t in templates if t['id'] == target_template_id]
            
        if not templates:
            logging.warning("No templates found matching selection parameters.")
            return
            
        for template in templates:
            template_id = template['id']
            template_name = template['name']
            safe_filename = sanitize_filename(template_name)
            
            date_suffix = datetime.now().strftime("_%b_%d_%Y")
            logging.info(f"Processing total configuration snapshot for template: {template_name} ({template_id})")
            
            template_data = {
                "metadata": {
                    "org_id": org_id,
                    "backup_timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
                },
                "global_policy_objects": global_policy_objects,
                "global_policy_object_groups": global_policy_groups,
                "template": template,
                "network_settings": {},
                "appliance_settings": {},
                "wireless_settings": {},
                "switch_profiles": [],
                "networks": []
            }
            
            try: template_data["network_settings"]["settings"] = dashboard.networks.getNetworkSettings(template_id)
            except meraki.APIError: pass
            try: template_data["network_settings"]["alerts"] = dashboard.networks.getNetworkAlertsSettings(template_id)
            except meraki.APIError: pass
            try: template_data["network_settings"]["snmp"] = dashboard.networks.getNetworkSnmp(template_id)
            except meraki.APIError: pass
            try: template_data["network_settings"]["syslog"] = dashboard.networks.getNetworkSyslogServers(template_id)
            except meraki.APIError: pass

            if "appliance" in template.get("productTypes", []):
                try: template_data["appliance_settings"]["vlans"] = dashboard.appliance.getNetworkApplianceVlans(template_id)
                except meraki.APIError: pass
                try: template_data["appliance_settings"]["static_routes"] = dashboard.appliance.getNetworkApplianceStaticRoutes(template_id)
                except meraki.APIError: pass
                try: template_data["appliance_settings"]["ports"] = dashboard.appliance.getNetworkAppliancePorts(template_id)
                except meraki.APIError: pass
                try: template_data["appliance_settings"]["l3_firewall"] = dashboard.appliance.getNetworkApplianceFirewallL3FirewallRules(template_id)
                except meraki.APIError: pass
                try: template_data["appliance_settings"]["l7_firewall"] = dashboard.appliance.getNetworkApplianceFirewallL7FirewallRules(template_id)
                except meraki.APIError: pass
                try: template_data["appliance_settings"]["vpn_bgp_sdwan_rules"] = dashboard.appliance.getNetworkApplianceTrafficShapingUplinkSelection(template_id)
                except meraki.APIError: pass

            if "wireless" in template.get("productTypes", []):
                try: template_data["wireless_settings"]["settings"] = dashboard.wireless.getNetworkWirelessSettings(template_id)
                except meraki.APIError: pass
                try:
                    ssids = dashboard.wireless.getNetworkWirelessSsids(template_id)
                    template_data["wireless_settings"]["ssids"] = ssids
                    template_data["wireless_settings"]["ssid_details"] = {}
                    for ssid in ssids:
                        num = ssid["number"]
                        template_data["wireless_settings"]["ssid_details"][num] = {}
                        try: template_data["wireless_settings"]["ssid_details"][num]["l3_firewall"] = dashboard.wireless.getNetworkWirelessSsidFirewallL3FirewallRules(template_id, num)
                        except meraki.APIError: pass
                except meraki.APIError: pass

            if "switch" in template.get("productTypes", []):
                try:
                    profiles = dashboard.switch.getOrganizationConfigTemplateSwitchProfiles(org_id, template_id)
                    for p in profiles:
                        p_id = p["switchProfileId"]
                        ports = dashboard.switch.getOrganizationConfigTemplateSwitchProfilePorts(org_id, template_id, p_id)
                        template_data["switch_profiles"].append({"config": p, "ports": ports})
                except meraki.APIError: pass
                
            bound_networks = [net for net in all_networks if net.get('configTemplateId') == template_id]
            for net in bound_networks:
                net_id = net['id']
                logging.info(f"  -> Archiving local deployment and hardware overrides for network: {net['name']}")
                local_overrides = {"devices": [], "wan_uplink_settings": {}, "mx_vlans": [], "site_to_site_vpn": {}}
                try:
                    devices = dashboard.networks.getNetworkDevices(net_id)
                    local_overrides["devices"] = devices
                    for device in devices:
                        if "MX" in device.get("model", "") or "Z" in device.get("model", ""):
                            local_overrides["wan_uplink_settings"][device["serial"]] = dashboard.appliance.getDeviceApplianceUplinksSettings(device["serial"])
                except meraki.APIError: pass
                try: local_overrides["mx_vlans"] = dashboard.appliance.getNetworkApplianceVlans(net_id)
                except meraki.APIError: pass
                try: local_overrides["site_to_site_vpn"] = dashboard.appliance.getNetworkApplianceVpnSiteToSiteVpn(net_id)
                except meraki.APIError: pass
                    
                template_data["networks"].append({
                    "config": net,
                    "local_overrides": local_overrides
                })
                
            filename = f"template_{safe_filename}{date_suffix}.json"
            json_string = json.dumps(template_data, indent=4)
            
            if use_github:
                upload_to_github(filename, json_string)
            else:
                file_path = os.path.join(BACKUP_DIR, filename)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(json_string)
                logging.info(f"SUCCESS: Payloads compiled and saved to {file_path}")
            
    except meraki.APIError as e:
        logging.error(f"Meraki Master Framework API Error: {e}")

def run_restore(dashboard, org_id, target_filename=None, auto_detect_deletion=False, target_network_name=None):
    """Processes comprehensive disaster-recovery restoration mapping with dependency reordering rules."""
    if target_filename:
        files = [target_filename]
    else:
        if not os.path.exists(BACKUP_DIR):
            logging.error("DR Error: Data archive directory could not be located on disk.")
            return
        files = [f for f in os.listdir(BACKUP_DIR) if f.endswith('.json')]
        
    if not files:
        logging.warning("Restoration skipped: Zero target backup JSON payloads found.")
        return

    try:
        org_templates = dashboard.organizations.getOrganizationConfigTemplates(org_id)
        org_networks = dashboard.organizations.getOrganizationNetworks(org_id)
    except meraki.APIError as e:
        logging.error(f"Failed to fetch baseline cloud information maps: {e}")
        return

    for file in files:
        file_path = os.path.join(BACKUP_DIR, file)
        with open(file_path, "r") as f:
            backup_data = json.load(f)
            
        template_name = backup_data["template"]["name"]
        product_types = backup_data["template"]["productTypes"]
        
        is_isolated_net_run = target_network_name is not None
        
        if is_isolated_net_run:
            logging.info(f"[i] RUN SCENARIO: Single Network Isolation active targeting network: '{target_network_name}'")
            backup_data["networks"] = [n for n in backup_data.get("networks", []) if n["config"]["name"].lower() == target_network_name.lower()]
            if not backup_data["networks"]:
                logging.error(f"Abort: Target network '{target_network_name}' is missing inside selected backup archive.")
                continue
            was_deleted = True if not next((n for n in org_networks if n["name"].lower() == target_network_name.lower()), None) else False
        else:
            if auto_detect_deletion:
                was_deleted = False if not next((t for t in org_templates if t["name"] == template_name), None) else True
            else:
                was_deleted_str = input(f"Is this a full template/org disaster rebuild for {template_name}? (yes/no): ").strip().lower()
                was_deleted = True if was_deleted_str in ['y', 'yes'] else False

        # --- RESTORE SEQUENCE LAYER 0: GLOBAL POLICY OBJECTS & GROUPS ---
        logging.info("  -> Validating / Syncing Global Policy Objects...")
        object_id_map = {} 
        try: live_objects = dashboard.organizations.getOrganizationPolicyObjects(org_id, total_pages='all')
        except meraki.APIError: live_objects = []

        for obj in backup_data.get("global_policy_objects", []):
            try:
                match = next((o for o in live_objects if o['name'] == obj['name'] and o['type'] == obj['type']), None)
                if not match:
                    new_obj = dashboard.organizations.createOrganizationPolicyObject(org_id, name=obj['name'], type=obj['type'], details=obj.get('details'))
                    object_id_map[obj['id']] = new_obj['id']
                else:
                    object_id_map[obj['id']] = match['id']
            except Exception: pass

        try: live_groups = dashboard.organizations.getOrganizationPolicyObjectsGroups(org_id, total_pages='all')
        except meraki.APIError: live_groups = []

        for group in backup_data.get("global_policy_object_groups", []):
            try:
                mapped_ids = [object_id_map.get(old_id, old_id) for old_id in group.get('objectIds', [])]
                match_group = next((g for g in live_groups if g['name'] == group['name']), None)
                if not match_group:
                    dashboard.organizations.createOrganizationPolicyObjectsGroup(org_id, name=group['name'], objectIds=mapped_ids)
                else:
                    dashboard.organizations.updateOrganizationPolicyObjectsGroup(org_id, match_group['id'], name=group['name'], objectIds=mapped_ids)
            except Exception: pass

        # --- RE-ANCHOR TEMPLATE STRUCTURE ---
        live_template = next((t for t in org_templates if t["name"] == template_name), None)
        
        if was_deleted and not is_isolated_net_run:
            logging.info(f"[!!!] Re-constructing missing template anchor profile: {template_name}")
            new_template = dashboard.organizations.createOrganizationConfigTemplate(org_id, name=template_name, productTypes=product_types)
            target_template_id = new_template["id"]
        else:
            if not live_template:
                logging.error(f"Sync target template '{template_name}' missing from cloud dashboard. Can't attach network. Skipping.")
                continue
            target_template_id = live_template["id"]

        # --- RESTORE GLOBAL BALANCING AND APPLIANCE LAYERS ONLY DURING FULL RUNS ---
        if not is_isolated_net_run:
            logging.info("  -> Syncing Template core baseline configuration maps...")
            net_settings = backup_data.get("network_settings", {})
            if "settings" in net_settings:
                try: dashboard.networks.updateNetworkSettings(target_template_id, **{k:v for k,v in net_settings["settings"].items() if v is not None})
                except Exception: pass
            
            app_settings = backup_data.get("appliance_settings", {})
            if "appliance" in product_types and app_settings:
                # ORDERING UPDATE: Instantiating VLAN subnets completely BEFORE committing routes
                if "vlans" in app_settings:
                    logging.info("     -> Establishing Core Template Layer-3 VLAN boundaries...")
                    for vlan in app_settings["vlans"]:
                        try: dashboard.appliance.createNetworkApplianceVlan(target_template_id, id=vlan["id"], name=vlan["name"], subnet=vlan["subnet"], applianceIp=vlan["applianceIp"])
                        except Exception: pass
                
                if "static_routes" in app_settings:
                    logging.info("     -> Committing Next-Hop Static Routing entries over established VLAN gateways...")
                    for route in app_settings["static_routes"]:
                        try: dashboard.appliance.createNetworkApplianceStaticRoute(target_template_id, **{k:v for k,v in route.items() if k not in ['id']})
                        except Exception: pass

                if "l3_firewall" in app_settings:
                    try: dashboard.appliance.updateNetworkApplianceFirewallL3FirewallRules(target_template_id, **app_settings["l3_firewall"])
                    except Exception: pass

        # Track networks needing VPN activation after they are initialized and fully ready
        vpn_rebuild_queue = []

        # --- RESTORE SEQUENCE LAYER 6: BOUND NETWORKS RE-INJECTION ---
        for net_obj in backup_data.get("networks", []):
            old_net_config = net_obj["config"]
            overrides = net_obj["local_overrides"]
            
            target_net = next((n for n in org_networks if n["name"].lower() == old_net_config["name"].lower()), None)
            
            if not target_net:
                logging.info(f" -> Rebuilding missing isolated network shell: {old_net_config['name']}")
                new_net = dashboard.organizations.createOrganizationNetwork(
                    org_id, name=old_net_config["name"], productTypes=old_net_config["productTypes"],
                    timeZone=old_net_config.get("timeZone"), configTemplateId=target_template_id
                )
                current_net_id = new_net["id"]
            else:
                current_net_id = target_net["id"]
                try: dashboard.networks.bindNetwork(current_net_id, configTemplateId=target_template_id)
                except Exception: pass
            
            logging.info(f" -> Injecting custom localized overrides for {old_net_config['name']}...")
            
            # SERIAL TOLERANCE UPDATE: Failures to claim physically missing inventory won't crash the script
            for dev in overrides.get("devices", []):
                try: 
                    dashboard.networks.claimNetworkDevices(current_net_id, [dev["serial"]])
                except meraki.APIError as dev_err:
                    logging.warning(f"     [!] Hardware Claim Bypass on serial {dev['serial']}: {dev_err.message}. Logical structural setup continuing.")
            
            for serial, uplinks in overrides.get("wan_uplink_settings", {}).items():
                try: dashboard.appliance.updateDeviceApplianceUplinksSettings(serial, wan1=uplinks.get("interfaces", {}).get("wan1", {}), wan2=uplinks.get("interfaces", {}).get("wan2", {}))
                except Exception: pass
            
            # ORDERING UPDATE: Establish local network overrides for L3 VLAN definitions prior to routing rules
            for vlan in overrides.get("mx_vlans", []):
                try: dashboard.appliance.createNetworkApplianceVlan(current_net_id, id=vlan["id"], name=vlan["name"], subnet=vlan.get("subnet"), applianceIp=vlan.get("applianceIp"))
                except Exception: pass

            # VPN DELAY UPDATE: Append settings to a processing queue to keep interdependencies from failing
            vpn_rules = overrides.get("site_to_site_vpn", {})
            if vpn_rules and vpn_rules.get("mode") is not None:
                vpn_rebuild_queue.append({"net_id": current_net_id, "name": old_net_config["name"], "rules": vpn_rules})

        # --- FINAL PHASE: MESH VPN TOPOLOGY RECONSTRUCTION ---
        if vpn_rebuild_queue:
            logging.info("  -> All configurations settled. Initiating crypto-mesh Site-to-Site VPN connections...")
            for vpn_job in vpn_rebuild_queue:
                try:
                    logging.info(f"     -> Building mesh connections for network: {vpn_job['name']}")
                    clean_payload = {k: v for k, v in vpn_job["rules"].items() if k not in ['networkId', 'networkName']}
                    dashboard.appliance.updateNetworkApplianceVpnSiteToSiteVpn(vpn_job["net_id"], **clean_payload)
                except Exception as vpn_err:
                    logging.warning(f"     [!] Post-execution VPN mesh generation warnings found on network {vpn_job['name']}: {vpn_err}")

def main():
    parser = argparse.ArgumentParser(description="Meraki Disaster Recovery Automation Tool")
    parser.add_argument("-B", "--backup", action="store_true", help="Run backup operations")
    parser.add_argument("-R", "--restore", action="store_true", help="Run restoration operations")
    parser.add_argument("-A", "--all", action="store_true", help="Target all properties automatically")
    parser.add_argument("-O", "--orgname", type=str, help="The name of your Meraki Organization")
    parser.add_argument("-N", "--network", type=str, help="Isolate restore operations strictly to this single named network")
    parser.add_argument("-G", "--github", action="store_true", help="Enable direct configuration archival to a GitHub repository")
    
    args = parser.parse_args()
    
    # RATE-LIMITING UPDATE: Configure engine instance with automatic retries and active thread backoffs
    dashboard_session = meraki.DashboardAPI(
        api_key=API_KEY, 
        suppress_logging=True,
        maximum_retries=5,
        wait_on_rate_limit=True
    )
    
    is_interactive_mode = not args.all
    if is_interactive_mode:
        print_welcome_screen()
        
    if args.orgname:
        org_name = args.orgname
    else:
        org_name = input("Enter the Meraki Organization Name: ").strip()
        if not org_name: sys.exit(1)
            
    org_id = get_org_id_by_name(dashboard_session, org_name)
    
    if not args.backup and not args.restore:
        choice = input("Choose core process mode - [B]ackup or [R]estore: ").strip().upper()
        if choice == 'B': args.backup = True
        elif choice == 'R': args.restore = True
        else: sys.exit(1)
            
    if args.backup:
        if args.all:
            run_backup(dashboard_session, org_id, target_template_id=None, use_github=args.github)
        else:
            try:
                templates = dashboard_session.organizations.getOrganizationConfigTemplates(org_id)
                print("\n=== Active Organization Templates ===")
                for idx, t in enumerate(templates):
                    print(f"{idx + 1}. {t['name']} ({t['id']})")
                sel = int(input("\nSelect a template number to process for backup: ")) - 1
                run_backup(dashboard_session, org_id, target_template_id=templates[sel]['id'], use_github=args.github)
            except (ValueError, IndexError): pass
                
    elif args.restore:
        net_filter_name = args.network if args.network else None
        
        if args.all:
            run_restore(dashboard_session, org_id, target_filename=None, auto_detect_deletion=True, target_network_name=net_filter_name)
        else:
            if not os.path.exists(BACKUP_DIR): return
            files = [f for f in os.listdir(BACKUP_DIR) if f.endswith('.json')]
            if not files: return
            
            print("\n=== Available Backup Point-In-Time Archives ===")
            for idx, f in enumerate(files):
                print(f"{idx + 1}. {f}")
            try:
                sel = int(input("\nSelect target storage configuration number to begin restore: ")) - 1
                chosen_file = files[sel]
                
                if not net_filter_name:
                    net_choice = input("\nDo you want to isolate recovery to ONE specific network inside this file? (yes/no): ").strip().lower()
                    if net_choice in ['y', 'yes']:
                        net_filter_name = input("Enter the EXACT name of the network to recover: ").strip()
                
                run_restore(dashboard_session, org_id, target_filename=chosen_file, auto_detect_deletion=False, target_network_name=net_filter_name)
            except (ValueError, IndexError): pass

if __name__ == "__main__":
    main()