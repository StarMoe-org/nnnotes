#!/usr/bin/env python3
"""CI-only JP exit: try direct Version, then VPNGate Japanese relays with routes limited to game API/CDN IPv4s."""
import base64
import csv
import io
import ipaddress
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

import story_site

LIST_URL = "https://www.vpngate.net/api/iphone/"
LIMIT = 6


def relays(text):
    lines = text.lstrip("\ufeff").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("#HostName,"))
    rows = csv.DictReader(io.StringIO("\n".join([lines[start].lstrip("#")]
                                               + [line for line in lines[start + 1:] if not line.startswith("*")])))
    candidates = []
    for row in rows:
        if row.get("CountryShort") != "JP" or not row.get("OpenVPN_ConfigData_Base64"):
            continue
        try:
            address = ipaddress.IPv4Address(row["IP"])
            if not address.is_global:
                continue
            row["score"] = int(row["Score"])
            row["config"] = base64.b64decode(row["OpenVPN_ConfigData_Base64"], validate=True).decode("utf-8-sig")
            candidates.append(row)
        except (ValueError, KeyError, UnicodeError):
            continue
    return sorted(candidates, key=lambda row: row["score"], reverse=True)[:LIMIT]


def profile(row, targets):
    """Recreate the connection settings and inline certificates; never run relay-supplied hooks or routes."""
    source = row["config"]
    remote = re.findall(r"^remote\s+(\S+)\s+(\d+)\s*$", source, re.M)
    if len(remote) != 1 or remote[0][0] != row["IP"] or not 1 <= int(remote[0][1]) <= 65535:
        raise ValueError("relay has an unexpected remote")
    proto = re.search(r"^proto\s+(tcp|tcp-client|udp)\s*$", source, re.M)
    if proto is None:
        raise ValueError("relay has no supported protocol")
    settings = ["client", "dev tun", "proto " + ("tcp-client" if proto[1] == "tcp" else proto[1]),
                f"remote {row['IP']} {remote[0][1]}", "nobind", "persist-key", "persist-tun", "route-nopull",
                "script-security 1", "connect-timeout 15", "ping 5", "ping-restart 30", "verb 3"]
    for directive, allowed, default in [
        ("cipher", {"AES-128-CBC", "AES-256-CBC", "AES-128-GCM", "AES-256-GCM"}, "AES-128-CBC"),
        ("auth", {"SHA1", "SHA256", "SHA384", "SHA512"}, "SHA1"),
    ]:
        match = re.search(rf"^{directive}\s+(\S+)\s*$", source, re.M)
        value = match[1] if match else default
        if value not in allowed:
            raise ValueError(f"unsupported relay {directive}")
        settings.append(f"{directive} {value}")
        if directive == "cipher":
            settings += [f"data-ciphers {value}", f"data-ciphers-fallback {value}"]
    for tag in ("ca", "cert", "key"):
        blocks = re.findall(rf"<{tag}>\s*(.*?)\s*</{tag}>", source, re.S)
        if len(blocks) != 1 or "<" in blocks[0] or ">" in blocks[0]:
            raise ValueError(f"relay has invalid {tag}")
        settings.append(f"<{tag}>\n{blocks[0]}\n</{tag}>")
    for address in sorted(set(targets)):
        ipaddress.IPv4Address(address)
        settings.append(f"route {address} 255.255.255.255 vpn_gateway")
    return "\n".join(settings) + "\n"


def work_dir():
    return Path(story_site.env("RUNNER_TEMP")) / "nnnotes-jp-vpngate"


def read_control_file(path):
    try:
        return path.read_text(errors="replace")
    except FileNotFoundError:
        return ""
    except PermissionError:
        # OpenVPN creates its log/PID files as root with mode 0600.
        return subprocess.run(["sudo", "cat", "--", str(path)], check=True, stdout=subprocess.PIPE,
                              text=True, timeout=10).stdout


def stop():
    pidfile = work_dir() / "openvpn.pid"
    if pidfile.exists():
        pid = read_control_file(pidfile).strip()
        if pid.isdecimal() and int(pid) > 1:
            subprocess.run(["sudo", "kill", "--", pid], check=False, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            time.sleep(1)
        pidfile.unlink(missing_ok=True)


def probe():
    return subprocess.run([sys.executable, str(Path(__file__).with_name("story_site.py")), "jp-check"],
                          timeout=65).returncode == 0


def start():
    if probe():
        print("JP Version is reachable directly; no VPN is needed", flush=True)
        return
    story_site.configure_region()
    from nnnotes.jp import origin
    from urllib.parse import urlsplit
    targets, hosts = [], []
    for setting in ("NNNOTES_SERVERS_JP_API", "NNNOTES_SERVERS_JP_CDN"):
        hostname = urlsplit(origin(story_site.env(setting))).hostname
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(hostname, 443, socket.AF_INET)})
        if not addresses:
            sys.exit(f"jp_vpngate: no IPv4 addresses for {setting}")
        targets.extend(addresses)
        hosts.extend(f"{address} {hostname}\n" for address in addresses)
    # Pin the names to the addresses whose host routes enter the tunnel; their HTTPS hostname checks stay intact.
    subprocess.run(["sudo", "tee", "-a", "/etc/hosts"], input="\n" + "".join(hosts), text=True,
                   check=True, stdout=subprocess.DEVNULL)
    candidates = relays(story_site.get(LIST_URL, 60).decode("utf-8-sig"))
    if not candidates:
        sys.exit("jp_vpngate: the public pool has no Japanese OpenVPN relays")
    directory = work_dir()
    directory.mkdir(parents=True, exist_ok=True)
    log, pidfile = directory / "openvpn.log", directory / "openvpn.pid"
    for row in candidates:
        stop()
        log.unlink(missing_ok=True)
        try:
            contents = profile(row, targets)
        except ValueError as error:
            print(f"Skipping relay {row['IP']}: {error}", flush=True)
            continue
        config = directory / "client.ovpn"
        config.write_text(contents)
        config.chmod(0o600)
        print(f"Trying Japanese VPNGate relay {row['IP']}", flush=True)
        process = subprocess.run(["sudo", "openvpn", "--config", str(config), "--daemon", "--writepid", str(pidfile),
                                  "--log", str(log)], check=False)
        if process.returncode:
            print("Relay connection could not start; trying the next one", flush=True)
            continue
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            message = read_control_file(log)
            if "Initialization Sequence Completed" in message:
                if probe():
                    print(f"JP Version is reachable through {row['IP']}", flush=True)
                    return
                break
            if "Exiting due to fatal error" in message or "AUTH_FAILED" in message:
                break
            time.sleep(0.5)
        print("Relay did not provide JP Version access; trying the next one", flush=True)
        print("\n".join(read_control_file(log).splitlines()[-8:]), flush=True)
    stop()
    sys.exit("jp_vpngate: no relay passed the JP Version check")


if __name__ == "__main__":
    if sys.argv[1:] == ["start"]:
        start()
    elif sys.argv[1:] == ["stop"]:
        stop()
    else:
        sys.exit("usage: jp_vpngate.py start|stop")
