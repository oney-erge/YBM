"""Read-only checks of this computer and the network it sits on, for a job that works out why something does not work (jobs/investigate.py).

A person who says "my TV will not take a cast" has a problem that is not in one folder or one setting. The checks that find it are the ones a technician runs in
turn: which network the computer is on and what Windows calls it, which adapter the discovery broadcasts leave by, which services Windows needs for that, what
the firewall does with it, what answers on the network, what a device says about itself. Each is here as one probe that returns a short text for a model to read and
the numbers behind it. Nothing here changes anything: a change is a different step, comes as a card, and is approved first.

The probes use only the standard library and PowerShell cmdlets that read (`Get-Net*`, `Get-Service`, `netsh ... show`). A network address given to a probe must be
a private one: this tool is for a home network, not for scanning the internet.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree

PROBES = (
    "network_overview", "services", "firewall", "wireless_display", "chrome_policy", "lan_devices", "host_check", "printers", "event_log",
)

#: What a model is told each probe does, in the words it needs to choose one.
PROBE_HELP = {
    "network_overview": "network adapters (cable, Wi-Fi, virtual ones such as WSL, Hyper-V, VPN), whether each is connected, the network type Windows gave it (Public or Private), its address, gateway and metric, and which adapter multicast discovery will leave by",
    "services": "the Windows services that device discovery, casting and media sharing depend on (SSDP, UPnP, Function Discovery, DNS client, WLAN) and whether each is running",
    "firewall": "the firewall's state per network type and the rules for network discovery, mDNS, SSDP and Chrome",
    "wireless_display": "whether this computer's Wi-Fi hardware and driver can do Miracast (Windows' Win+K 'Connect' mirroring) and whether the wireless display feature is installed",
    "chrome_policy": "whether Chrome or Edge has a policy that turns off casting (MediaRouter), and the browser versions",
    "lan_devices": "what answers on the home network: devices found by SSDP (UPnP, DLNA) and mDNS (Chromecast, AirPlay, Samsung), with name, model and services, tried from every network adapter",
    "printers": "the printers Windows knows (name, status, port), the print spooler service and the jobs stuck in the queue",
    "event_log": "the errors and warnings Windows logged in the last day (System and Application), newest first, with their source, optionally only those that mention a word",
    "host_check": "one device by address: does it answer a ping, which ports are open (Samsung 8001/8002/9197, Chromecast 8008/8009, AirPlay 7000, DLNA 7676), what its own web interface says about it",
}

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SAMSUNG_PORTS = (8001, 8002, 9197, 7676, 55000, 52235)
CAST_PORTS = (8008, 8009, 8443)
AIRPLAY_PORTS = (7000, 7100, 49152)
HOST_PORTS = (*SAMSUNG_PORTS, *CAST_PORTS, *AIRPLAY_PORTS, 80, 443)
VIRTUAL_HINTS = ("vethernet", "wsl", "hyper-v", "virtualbox", "vmware", "vpn", "tap-", "wireguard", "tailscale", "zerotier", "docker", "loopback", "bluetooth", "npcap")


@dataclass
class Probe:
    """What a probe found: `summary` is what a model reads, `data` the numbers behind it, `ok` False when the probe could not run (with why in the summary)."""

    op: str
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)


def _run(args: list[str], timeout: float = 25.0) -> str:
    completed = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW, check=False)  # noqa: S603 - fixed argument lists, no shell
    return (completed.stdout or b"").decode("utf-8", errors="replace") + (completed.stderr or b"").decode("utf-8", errors="replace")


def powershell(script: str, timeout: float = 30.0) -> str:
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], timeout)


def powershell_json(script: str, timeout: float = 30.0) -> Any:
    """The JSON a read-only PowerShell expression prints (a single object becomes a one-item list); None when it printed none."""
    text = powershell(f"$ProgressPreference='SilentlyContinue'; ({script}) | ConvertTo-Json -Depth 4 -Compress", timeout).strip()
    start = min((i for i in (text.find("["), text.find("{")) if i >= 0), default=-1)
    if start < 0:
        return None
    try:
        value = json.loads(text[start:])
    except ValueError:
        return None
    return value if isinstance(value, list) else [value]


def is_private(host: str) -> bool:
    """A literal private or link-local address, or a `.local` name: the only things a probe will talk to."""
    host = host.strip().lower()
    if host.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_link_local


# --- the network as Windows sees it -----------------------------------------------------------------------------------------------------------


def _virtual(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in VIRTUAL_HINTS)


def network_overview() -> Probe:
    adapters = powershell_json("Get-NetAdapter | Select-Object Name,InterfaceDescription,Status,LinkSpeed,InterfaceIndex") or []
    interfaces = powershell_json("Get-NetIPInterface -AddressFamily IPv4 | Select-Object InterfaceAlias,InterfaceMetric,ConnectionState,Dhcp") or []
    profiles = powershell_json("Get-NetConnectionProfile | Select-Object Name,InterfaceAlias,NetworkCategory,IPv4Connectivity") or []
    configs = powershell_json(
        "Get-NetIPConfiguration | Select-Object InterfaceAlias,@{n='IP';e={$_.IPv4Address.IPAddress}},@{n='Gateway';e={$_.IPv4DefaultGateway.NextHop}}"
    ) or []
    wifi = _run(["netsh", "wlan", "show", "interfaces"])
    if not interfaces and not adapters:
        return Probe("network_overview", False, "Could not read the network adapters (PowerShell returned nothing).")
    metric = {str(item.get("InterfaceAlias")): int(item.get("InterfaceMetric") or 0) for item in interfaces}
    category = {str(item.get("InterfaceAlias")): (item.get("Name"), {0: "Public", 1: "Private", 2: "Domain"}.get(item.get("NetworkCategory"), str(item.get("NetworkCategory")))) for item in profiles}
    ip = {str(item.get("InterfaceAlias")): (item.get("IP"), item.get("Gateway")) for item in configs}
    rows, connected = [], []
    for adapter in adapters:
        name = str(adapter.get("Name"))
        status = str(adapter.get("Status"))
        up = status.lower() == "up"
        address, gateway = ip.get(name, (None, None))
        row = {
            "name": name, "description": adapter.get("InterfaceDescription"), "up": up, "speed": adapter.get("LinkSpeed"), "metric": metric.get(name),
            "ip": address, "gateway": gateway, "virtual": _virtual(name) or _virtual(str(adapter.get("InterfaceDescription"))),
            "network": category.get(name, (None, None))[0], "category": category.get(name, (None, None))[1],
        }
        rows.append(row)
        if up and address:
            connected.append(row)
    lan = [row for row in connected if not row["virtual"] and not str(row["ip"]).startswith("169.254.")]
    first_out = min((row for row in connected if row["metric"] is not None), key=lambda row: row["metric"], default=None)
    lines = []
    for row in rows:
        if not row["up"] and not row["ip"]:
            continue
        kind = "virtual" if row["virtual"] else "physical"
        network = f", network '{row['network']}' is {row['category']}" if row["category"] else ""
        lines.append(f"- {row['name']} ({kind}): {'connected' if row['up'] else 'not connected'}, {row['speed'] or 'no speed'}, address {row['ip'] or 'none'}, gateway {row['gateway'] or 'none'}, metric {row['metric']}{network}")
    findings = []
    if first_out and first_out["virtual"] and lan:
        findings.append(
            f"Multicast discovery (what finds TVs and speakers) leaves by the adapter with the lowest metric, {first_out['name']} (metric {first_out['metric']}), which is a virtual adapter, "
            f"not {lan[0]['name']} (metric {lan[0]['metric']}). Discovery packets can end up on the virtual network and never reach the TV."
        )
    for row in lan:
        if row["category"] == "Public":
            findings.append(f"{row['name']} is on a Public network ('{row['network']}'). Windows turns network discovery and media sharing off for Public networks.")
    wifi_state = re.search(r"^\s*State\s*:\s*(.+)$", wifi, re.MULTILINE)
    ssid = re.search(r"^\s*SSID\s*:\s*(.+)$", wifi, re.MULTILINE)
    wifi_text = f"Wi-Fi: {wifi_state.group(1).strip()}" + (f", connected to '{ssid.group(1).strip()}'" if ssid else "") if wifi_state else "Wi-Fi: no wireless interface reported"
    summary = "Network adapters:\n" + "\n".join(lines) + f"\n{wifi_text}.\n" + ("\n".join(findings) if findings else "Nothing in the adapter setup stands out.")
    return Probe("network_overview", True, summary, {"adapters": rows, "wifi": wifi_text, "findings": findings, "lan_adapters": [row["name"] for row in lan]})


def services() -> Probe:
    wanted = {
        "SSDPSRV": "SSDP Discovery: finds UPnP and DLNA devices (TVs, media servers)",
        "upnphost": "UPnP Device Host: lets this PC be found as a media server",
        "FDResPub": "Function Discovery Resource Publication: lets this PC and its media be found on the network",
        "fdPHost": "Function Discovery Provider Host: Windows' own device discovery (Cast to device, Connect)",
        "Dnscache": "DNS Client: also does mDNS (.local names, Chromecast, AirPlay discovery)",
        "WlanSvc": "WLAN AutoConfig: Wi-Fi and Miracast",
        "NlaSvc": "Network Location Awareness: decides Public or Private",
    }
    rows = powershell_json("Get-Service " + ",".join(wanted) + " -ErrorAction SilentlyContinue | Select-Object Name,Status,StartType") or []
    by_name = {str(row.get("Name")): row for row in rows}
    status = {1: "stopped", 4: "running", 2: "starting", 3: "stopping"}
    lines, findings = [], []
    for name, meaning in wanted.items():
        row = by_name.get(name)
        if row is None:
            lines.append(f"- {name}: not found. {meaning}")
            continue
        state = status.get(row.get("Status"), str(row.get("Status")))
        start = {0: "boot", 1: "system", 2: "automatic", 3: "manual", 4: "disabled"}.get(row.get("StartType"), str(row.get("StartType")))
        lines.append(f"- {name}: {state} (start: {start}). {meaning}")
        if state == "stopped" and name in ("SSDPSRV", "fdPHost", "FDResPub", "Dnscache", "NlaSvc"):
            findings.append(f"{name} is stopped ({meaning.split(':')[0]}); start type is {start}.")
    summary = "Windows services for discovery and casting:\n" + "\n".join(lines) + "\n" + ("\n".join(findings) if findings else "The services Windows needs for discovery are running.")
    return Probe("services", bool(rows), summary if rows else "Could not read the services.", {"services": rows, "findings": findings})


def firewall() -> Probe:
    profiles = powershell_json("Get-NetFirewallProfile | Select-Object Name,Enabled,DefaultInboundAction") or []
    groups = powershell_json(
        "Get-NetFirewallRule -DisplayGroup 'Network Discovery' -ErrorAction SilentlyContinue | Select-Object DisplayName,Enabled,Direction,Action,Profile"
    ) or []
    chrome = powershell_json(
        "Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -match 'Chrome|mDNS|SSDP|Cast' } | Select-Object -First 30 DisplayName,Enabled,Direction,Action,Profile"
    ) or []
    if not profiles:
        return Probe("firewall", False, "Could not read the firewall settings.")
    on = lambda value: "on" if value in (True, 1, "True") else "off"  # noqa: E731
    lines = [f"- {item.get('Name')} profile: firewall {on(item.get('Enabled'))}" for item in profiles]
    enabled_discovery = [item for item in groups if item.get("Enabled") in (True, 1, "True")]
    findings = []
    if groups and not enabled_discovery:
        findings.append("Every 'Network Discovery' firewall rule is switched off, so this computer neither finds nor answers devices on the network.")
    elif groups:
        public = [item for item in enabled_discovery if item.get("Profile") in (4, 7, 2147483647) or "Public" in str(item.get("Profile"))]
        findings.append(f"{len(enabled_discovery)} of {len(groups)} 'Network Discovery' rules are on; on a Public network they are off by design." if not public else f"{len(enabled_discovery)} of {len(groups)} 'Network Discovery' rules are on.")
    blocking = [item for item in chrome if item.get("Action") in (4, "Block") and item.get("Enabled") in (True, 1, "True")]
    if blocking:
        findings.append("Blocking rules that mention Chrome, mDNS, SSDP or Cast: " + "; ".join(str(item.get("DisplayName")) for item in blocking[:5]) + ".")
    summary = "Firewall:\n" + "\n".join(lines) + "\n" + ("\n".join(findings) if findings else "No rule that blocks discovery or Chrome was found.")
    return Probe("firewall", True, summary, {"profiles": profiles, "discovery_rules": len(groups), "discovery_rules_on": len(enabled_discovery), "findings": findings})


def wireless_display() -> Probe:
    text = _run(["netsh", "wlan", "show", "drivers"])
    support = re.search(r"Wireless Display Supported\s*:\s*(.+)", text)
    driver = re.search(r"^\s*Driver\s*:\s*(.+)$", text, re.MULTILINE)
    devices = powershell_json("Get-PnpDevice -ErrorAction SilentlyContinue | Where-Object { $_.FriendlyName -match 'Wireless Display|Miracast|Wi-Fi Direct' } | Select-Object -First 8 FriendlyName,Status") or []
    feature = powershell_json("Get-WindowsCapability -Online -Name 'App.WirelessDisplay.Connect*' -ErrorAction SilentlyContinue | Select-Object Name,State", 40) or []
    if not support:
        return Probe("wireless_display", False, "No wireless interface or driver information was reported, so Miracast cannot be checked.")
    lines = [f"Wireless Display Supported: {support.group(1).strip()}" + (f"; Wi-Fi driver: {driver.group(1).strip()}" if driver else "")]
    lines += [f"- {item.get('FriendlyName')}: {item.get('Status')}" for item in devices]
    lines += [f"- Windows feature {item.get('Name')}: {item.get('State')}" for item in feature]
    return Probe("wireless_display", True, "Miracast (Win+K) support:\n" + "\n".join(lines), {"supported": support.group(1).strip(), "devices": devices, "feature": feature})


def chrome_policy() -> Probe:
    script = (
        "foreach ($k in 'HKLM:\\SOFTWARE\\Policies\\Google\\Chrome','HKCU:\\SOFTWARE\\Policies\\Google\\Chrome','HKLM:\\SOFTWARE\\Policies\\Microsoft\\Edge','HKCU:\\SOFTWARE\\Policies\\Microsoft\\Edge') "
        "{ if (Test-Path $k) { $p = Get-ItemProperty $k; [pscustomobject]@{Key=$k; EnableMediaRouter=$p.EnableMediaRouter; ShowCastIconInToolbar=$p.ShowCastIconInToolbar; MediaRouterCastAllowAllIPs=$p.MediaRouterCastAllowAllIPs} } }"
    )
    keys = powershell_json(script) or []
    versions = powershell_json(
        "foreach ($f in 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe','C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe','C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe') "
        "{ if (Test-Path $f) { [pscustomobject]@{Path=$f; Version=(Get-Item $f).VersionInfo.ProductVersion} } }"
    ) or []
    disabled = [item for item in keys if item.get("EnableMediaRouter") == 0]
    lines = [f"- {item.get('Path')}: {item.get('Version')}" for item in versions] or ["- no Chrome or Edge found in the usual places"]
    if keys:
        lines += [f"- policy key {item.get('Key')}: EnableMediaRouter={item.get('EnableMediaRouter')}" for item in keys]
    finding = "A browser policy turns casting off (EnableMediaRouter is 0)." if disabled else "No policy turns browser casting off."
    return Probe("chrome_policy", True, "Browsers:\n" + "\n".join(lines) + "\n" + finding, {"versions": versions, "policies": keys, "casting_disabled_by_policy": bool(disabled)})


# --- what answers on the network --------------------------------------------------------------------------------------------------------------

SSDP_ADDRESS = ("239.255.255.250", 1900)
MDNS_ADDRESS = ("224.0.0.251", 5353)
MDNS_NAMES = ("_googlecast._tcp.local", "_airplay._tcp.local", "_raop._tcp.local", "_samsungmsf._tcp.local", "_services._dns-sd._udp.local", "_spotify-connect._tcp.local")


def local_addresses() -> list[str]:
    """The IPv4 addresses of this computer's connected adapters that are not loopback or link-local: one search is sent from each."""
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    text = _run(["ipconfig"])
    found.update(re.findall(r"IPv4 Address[^:]*:\s*([\d.]+)", text))
    return sorted(address for address in found if not address.startswith(("127.", "169.254.")))


def _ssdp_request(search_target: str) -> bytes:
    return "\r\n".join(["M-SEARCH * HTTP/1.1", f"HOST: {SSDP_ADDRESS[0]}:{SSDP_ADDRESS[1]}", 'MAN: "ssdp:discover"', "MX: 2", f"ST: {search_target}", "", ""]).encode()


def parse_ssdp(data: bytes) -> dict[str, str]:
    headers = {}
    for line in data.decode(errors="replace").split("\r\n")[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().upper()] = value.strip()
    return headers


def _mdns_query(name: str) -> bytes:
    labels = b"".join(bytes([len(part)]) + part.encode() for part in name.split("."))
    return struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + labels + b"\x00" + struct.pack(">HH", 12, 0x8001)  # PTR, class IN with the unicast-response bit


def _dns_name(data: bytes, offset: int) -> tuple[str, int]:
    parts, jumped, end = [], False, offset
    for _ in range(40):
        if offset >= len(data):
            break
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                end = offset + 2
            offset, jumped = pointer, True
            continue
        parts.append(data[offset + 1:offset + 1 + length].decode(errors="replace"))
        offset += 1 + length
    return ".".join(parts), (end if jumped else offset)


def parse_mdns(data: bytes) -> list[str]:
    """The names a multicast DNS answer carries (pointer targets, service and host names): enough to say what kind of device answered."""
    names: list[str] = []
    try:
        _id, _flags, questions, answers, authority, additional = struct.unpack(">HHHHHH", data[:12])
        offset = 12
        for _ in range(questions):
            _name, offset = _dns_name(data, offset)
            offset += 4
        for _ in range(answers + authority + additional):
            name, offset = _dns_name(data, offset)
            rtype, _cls, _ttl, length = struct.unpack(">HHIH", data[offset:offset + 10])
            offset += 10
            if rtype in (12, 33):  # PTR, SRV
                target, _ = _dns_name(data, offset + (6 if rtype == 33 else 0))
                names += [name, target]
            else:
                names.append(name)
            offset += length
    except (struct.error, IndexError):
        pass
    return list(dict.fromkeys(item for item in names if item))


def _scan_from(address: str, seconds: float) -> tuple[list[tuple[str, dict[str, str]]], list[tuple[str, list[str]]]]:
    """Send SSDP and mDNS searches from one local address and collect the answers for `seconds`."""
    ssdp: list[tuple[str, dict[str, str]]] = []
    mdns: list[tuple[str, list[str]]] = []
    sockets = []
    try:
        for kind in ("ssdp", "mdns"):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((address, 0))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.settimeout(0.3)
            sockets.append((kind, sock))
        for target in ("ssdp:all", "urn:schemas-upnp-org:device:MediaRenderer:1", "urn:dial-multiscreen-org:service:dial:1"):
            sockets[0][1].sendto(_ssdp_request(target), SSDP_ADDRESS)
        for name in MDNS_NAMES:
            sockets[1][1].sendto(_mdns_query(name), MDNS_ADDRESS)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            for kind, sock in sockets:
                try:
                    data, (ip, _port) = sock.recvfrom(65535)
                except (TimeoutError, socket.timeout, OSError):
                    continue
                if kind == "ssdp":
                    ssdp.append((ip, parse_ssdp(data)))
                else:
                    mdns.append((ip, parse_mdns(data)))
    except OSError:
        pass
    finally:
        for _kind, sock in sockets:
            sock.close()
    return ssdp, mdns


def _describe(location: str) -> dict[str, str]:
    """A UPnP device's own description (name, maker, model, services). Only an address on the home network is asked."""
    host = re.match(r"https?://([^/:]+)", location)
    if not host or not is_private(host.group(1)):
        return {}
    try:
        with urllib.request.urlopen(location, timeout=3) as response:  # noqa: S310 - private address, checked above
            root = ElementTree.fromstring(response.read(200_000))
    except Exception:  # noqa: BLE001 - a device that does not describe itself is still a device
        return {}
    namespace = {"u": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    prefix = "u:" if namespace else ""

    def text(path: str) -> str:
        found = root.find(f".//{prefix}{path}", namespace)
        return " ".join((found.text or "").split()) if found is not None else ""

    services = sorted({(item.text or "").split(":")[-2] for item in root.iter() if item.tag.endswith("serviceType") and item.text and item.text.count(":") >= 3})
    return {"name": text("friendlyName"), "maker": text("manufacturer"), "model": text("modelName"), "type": text("deviceType").split(":")[-2] if text("deviceType").count(":") >= 3 else "", "services": ", ".join(services)}


def lan_devices(seconds: float = 5.0) -> Probe:
    addresses = local_addresses()
    if not addresses:
        return Probe("lan_devices", False, "This computer has no network address to search from.")
    seen: dict[str, dict[str, Any]] = {}
    answered_from: dict[str, int] = {}
    for address in addresses:
        ssdp, mdns = _scan_from(address, seconds)
        answered_from[address] = len({ip for ip, _ in ssdp} | {ip for ip, _ in mdns})
        for ip, headers in ssdp:
            entry = seen.setdefault(ip, {"ip": ip, "ssdp": [], "mdns": [], "via": set()})
            entry["via"].add(address)
            if headers.get("ST") and headers["ST"] not in [item["st"] for item in entry["ssdp"]]:
                entry["ssdp"].append({"st": headers["ST"], "server": headers.get("SERVER", ""), "location": headers.get("LOCATION", "")})
        for ip, names in mdns:
            entry = seen.setdefault(ip, {"ip": ip, "ssdp": [], "mdns": [], "via": set()})
            entry["via"].add(address)
            entry["mdns"] = list(dict.fromkeys([*entry["mdns"], *names]))[:12]
    for entry in seen.values():
        location = next((item["location"] for item in entry["ssdp"] if item["location"]), "")
        entry["description"] = _describe(location) if location else {}
        entry["via"] = sorted(entry["via"])
    arp = _run(["arp", "-a"])
    neighbours = sorted(set(re.findall(r"^\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f-]{17})\s+dynamic", arp, re.IGNORECASE | re.MULTILINE)))
    devices = sorted(seen.values(), key=lambda item: tuple(int(part) for part in item["ip"].split(".")))
    lines = []
    for entry in devices:
        description = entry["description"]
        who = " ".join(filter(None, [description.get("name"), f"({description.get('maker')} {description.get('model')})".replace("( ", "(") if description.get("maker") or description.get("model") else "", description.get("type")])) or "unnamed device"
        extras = []
        if description.get("services"):
            extras.append("services " + description["services"])
        if entry["mdns"]:
            extras.append("mDNS " + ", ".join(entry["mdns"][:4]))
        extras.append("answered to " + ", ".join(entry["via"]))
        lines.append(f"- {entry['ip']}: {who}; " + "; ".join(extras))
    tried = ", ".join(f"{address} ({count} answered)" for address, count in answered_from.items())
    arp_text = f"{len(neighbours)} neighbours are in this computer's ARP table: " + ", ".join(ip for ip, _mac in neighbours[:20]) if neighbours else "The ARP table lists no neighbours."
    header = f"Searched from {tried}." if tried else ""
    body = "\n".join(lines) if lines else "No device answered an SSDP or mDNS search from any adapter."
    return Probe("lan_devices", True, f"{header}\n{body}\n{arp_text}", {"devices": devices, "answered_from": answered_from, "arp": [ip for ip, _ in neighbours]})


def _tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def host_check(host: str, ports: list[int] | None = None) -> Probe:
    host = host.strip()
    if not is_private(host):
        return Probe("host_check", False, f"{host} is not an address on the home network; only private addresses are checked.")
    ping = _run(["ping", "-n", "2", "-w", "1000", host], 10)
    ping_ok = bool(re.search(r"TTL=", ping))
    loss = re.search(r"\((\d+)% loss\)", ping)
    chosen = list(dict.fromkeys(ports or HOST_PORTS))[:24]
    opened = [port for port in chosen if _tcp_open(host, port)]
    notes = []
    info: dict[str, Any] = {}
    for port, scheme in ((8001, "http"), (8002, "https")):
        if port in opened:
            try:
                import ssl

                context = ssl._create_unverified_context() if scheme == "https" else None  # noqa: S323 - a TV's own self-signed certificate, on the home network
                with urllib.request.urlopen(f"{scheme}://{host}:{port}/api/v2/", timeout=3, context=context) as response:  # noqa: S310
                    info = json.loads(response.read(100_000).decode("utf-8", errors="replace"))
                break
            except Exception:  # noqa: BLE001 - not every device on that port is a TV
                continue
    device = info.get("device") if isinstance(info.get("device"), dict) else {}
    if device:
        notes.append(
            f"Its own interface (port 8001/8002) says: {device.get('name')} - {device.get('modelName')} ({device.get('type')}), power state {device.get('PowerState', 'on')}, "
            f"{'wired' if device.get('networkType') == 'wired' else device.get('networkType', 'unknown network type')}, firmware {device.get('firmwareVersion', 'unknown')}."
        )
    kinds = []
    if any(port in opened for port in SAMSUNG_PORTS[:3]):
        kinds.append("a Samsung smart TV control port is open")
    if any(port in opened for port in CAST_PORTS[:2]):
        kinds.append("Google Cast ports are open")
    if 7000 in opened:
        kinds.append("the AirPlay port is open")
    summary = (
        f"{host}: {'answers a ping' if ping_ok else 'does not answer a ping'}" + (f" ({loss.group(1)}% loss)" if loss else "") + f". Open ports: {', '.join(str(p) for p in opened) or 'none of ' + ', '.join(str(p) for p in chosen[:8]) + '...'}."
        + (" " + "; ".join(kinds).capitalize() + "." if kinds else "") + (" " + " ".join(notes) if notes else "")
        + ("" if ping_ok or opened else " A TV in standby often answers nothing until it is switched on.")
    )
    return Probe("host_check", ping_ok or bool(opened), summary, {"host": host, "ping": ping_ok, "open_ports": opened, "device": device})


def printers() -> Probe:
    devices = powershell_json("Get-Printer -ErrorAction SilentlyContinue | Select-Object Name,PrinterStatus,PortName,DriverName,Type,Shared") or []
    spooler = powershell_json("Get-Service Spooler -ErrorAction SilentlyContinue | Select-Object Name,Status,StartType") or []
    queue = powershell_json("Get-Printer -ErrorAction SilentlyContinue | ForEach-Object { Get-PrintJob -PrinterName $_.Name -ErrorAction SilentlyContinue } | Select-Object -First 15 PrinterName,DocumentName,JobStatus,SubmittedTime") or []
    if not devices and not spooler:
        return Probe("printers", False, "Could not read the printers or the print spooler.")
    status = {0: "ready", 1: "paused", 2: "error", 3: "deleting", 4: "paper jam", 5: "out of paper", 6: "manual feed", 7: "paper problem", 8: "offline", 128: "offline", 131072: "toner low", 262144: "no toner"}
    spooler_state = {1: "stopped", 4: "running"}.get(spooler[0].get("Status"), str(spooler[0].get("Status"))) if spooler else "unknown"
    lines = [f"- {item.get('Name')}: {status.get(item.get('PrinterStatus'), item.get('PrinterStatus'))}, port {item.get('PortName')}, driver {item.get('DriverName')}" for item in devices]
    lines += [f"- queued: {item.get('DocumentName')} on {item.get('PrinterName')} ({item.get('JobStatus')})" for item in queue]
    findings = []
    if spooler_state != "running":
        findings.append(f"The print spooler is {spooler_state}: nothing can print until it runs.")
    findings += [f"{item.get('Name')} is {status.get(item.get('PrinterStatus'), item.get('PrinterStatus'))}." for item in devices if item.get("PrinterStatus") not in (0, None)]
    if queue:
        findings.append(f"{len(queue)} job(s) are waiting in the queue; a stuck job can block the ones behind it.")
    summary = f"Print spooler: {spooler_state}.\n" + ("\n".join(lines) if lines else "No printers are installed.") + "\n" + ("\n".join(findings) if findings else "Nothing in the printer setup stands out.")
    return Probe("printers", True, summary, {"printers": devices, "spooler": spooler_state, "queue": queue, "findings": findings})


def event_log(word: str = "") -> Probe:
    needle = re.sub(r"[^\w .-]", "", word)[:40]
    filter_script = "$_.Message -match '" + needle + "'" if needle else "$true"
    script = (
        "Get-WinEvent -FilterHashtable @{LogName='System','Application'; Level=1,2,3; StartTime=(Get-Date).AddHours(-24)} -MaxEvents 200 -ErrorAction SilentlyContinue "
        "| Where-Object { " + filter_script + " } | Select-Object -First 12 TimeCreated,ProviderName,Id,LevelDisplayName,@{n='Text';e={($_.Message -split \"`n\")[0]}}"
    )
    events = powershell_json(script, 45) or []
    if not events:
        return Probe("event_log", True, "No errors or warnings" + (f" mentioning '{needle}'" if needle else "") + " were logged in the last 24 hours.", {"events": []})
    lines = [f"- {item.get('ProviderName')} ({item.get('LevelDisplayName')}, id {item.get('Id')}): {str(item.get('Text') or '')[:160]}" for item in events]
    return Probe("event_log", True, "Recent errors and warnings:\n" + "\n".join(lines), {"events": events})


# --- the menu ---------------------------------------------------------------------------------------------------------------------------------

_SYNC: dict[str, Callable[..., Probe]] = {
    "network_overview": network_overview, "services": services, "firewall": firewall, "wireless_display": wireless_display,
    "chrome_policy": chrome_policy, "lan_devices": lan_devices, "host_check": host_check, "printers": printers, "event_log": event_log,
}


async def run_probe(op: str, **args: Any) -> Probe:
    """Run one probe off the event loop. An unknown name or a probe that raises is a failed probe with the reason, not an exception for the job to count."""
    function = _SYNC.get(op)
    if function is None:
        return Probe(op, False, f"There is no probe called {op}. The probes are: {', '.join(PROBES)}.")
    if sys.platform != "win32":
        return Probe(op, False, "These checks read Windows settings and run on Windows only.")
    try:
        return await asyncio.wait_for(asyncio.to_thread(function, **args), timeout=120)
    except Exception as exc:  # noqa: BLE001 - reported to the job as a finding
        return Probe(op, False, f"The {op} check failed: {str(exc)[:200]}")
