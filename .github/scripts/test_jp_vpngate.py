"""VPNGate relay selection and generated split routes; no network or privileged commands."""
import base64
import csv
import io
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import jp_vpngate


CONFIG = """client
dev tun
proto tcp
remote 219.100.37.1 443
cipher AES-128-CBC
auth SHA1
<ca>
-----BEGIN CERTIFICATE-----
synthetic-ca
-----END CERTIFICATE-----
</ca>
<cert>
-----BEGIN CERTIFICATE-----
synthetic-client
-----END CERTIFICATE-----
</cert>
<key>
-----BEGIN PRIVATE KEY-----
synthetic-key
-----END PRIVATE KEY-----
</key>
"""


def row(ip="219.100.37.1", country="JP", score=100):
    return {"HostName": "example", "IP": ip, "CountryShort": country, "Score": str(score),
            "OpenVPN_ConfigData_Base64": base64.b64encode(CONFIG.replace("219.100.37.1", ip).encode()).decode()}


def listing(rows):
    output = io.StringIO()
    output.write("*vpn_servers\n")
    writer = csv.DictWriter(output, fieldnames=list(row()))
    writer.writeheader()
    for item in rows:
        writer.writerow(item)
    return output.getvalue().replace("HostName,", "#HostName,", 1) + "*\n"


def test_selects_ranked_japanese_relays_with_valid_public_addresses():
    rows = [row(score=20), row(ip="219.100.37.2", score=200), row(country="US", score=900),
            row(ip="127.0.0.1", score=1000), row(ip="10.0.0.1", score=1000)]
    malformed = row(score=1000)
    malformed["OpenVPN_ConfigData_Base64"] = "invalid-base64"
    result = jp_vpngate.relays(listing(rows + [malformed]))
    assert [item["IP"] for item in result] == ["219.100.37.2", "219.100.37.1"]


@pytest.mark.parametrize("count", [3, jp_vpngate.LIMIT, jp_vpngate.LIMIT + 3])
def test_selection_limits_connection_attempts(count):
    result = jp_vpngate.relays(listing([row(ip=f"219.100.37.{index}", score=index)
                                      for index in range(1, count + 1)]))
    assert len(result) == min(count, jp_vpngate.LIMIT)
    assert [item["score"] for item in result] == list(range(count, max(0, count - jp_vpngate.LIMIT), -1))


def test_profile_uses_only_selected_host_routes_and_ignores_supplied_hooks():
    item = {**row(), "config": CONFIG + "up /tmp/hook\nscript-security 2\nredirect-gateway def1\nroute 0.0.0.0 0.0.0.0\n"}
    config = jp_vpngate.profile(item, ["192.0.2.1", "192.0.2.2", "192.0.2.1"])
    assert "proto tcp-client\n" in config
    assert "route-nopull\n" in config
    assert "script-security 1\n" in config
    assert "hook" not in config and "redirect-gateway" not in config
    routes = [line for line in config.splitlines() if line.startswith("route ")]
    assert routes == ["route 192.0.2.1 255.255.255.255 vpn_gateway", "route 192.0.2.2 255.255.255.255 vpn_gateway"]
    assert "<ca>\n" in config and "<cert>\n" in config and "<key>\n" in config


@pytest.mark.parametrize("source", [
    CONFIG.replace("remote 219.100.37.1 443", "remote 219.100.37.2 443"),
    CONFIG.replace("proto tcp", "proto unsupported"),
    CONFIG.replace("<key>", "<unknown>"),
])
def test_invalid_profile_is_not_used(source):
    with pytest.raises(ValueError):
        jp_vpngate.profile({**row(), "config": source}, ["192.0.2.1"])


def test_reachable_api_finishes_without_touching_host_network(monkeypatch):
    monkeypatch.setattr(jp_vpngate, "probe", lambda: True)
    monkeypatch.setattr(jp_vpngate.story_site, "configure_region", lambda: pytest.fail("unexpected VPN setup"))
    jp_vpngate.start()


def test_root_owned_openvpn_files_can_be_read(monkeypatch, tmp_path):
    path = tmp_path / "openvpn.log"

    def denied(self, **kwargs):
        raise PermissionError("root-owned file")

    monkeypatch.setattr(Path, "read_text", denied)

    def run(command, **kwargs):
        assert command == ["sudo", "cat", "--", str(path)]
        assert kwargs["check"] and kwargs["timeout"] == 10
        return SimpleNamespace(stdout="Initialization Sequence Completed\n")

    monkeypatch.setattr(jp_vpngate.subprocess, "run", run)
    assert jp_vpngate.read_control_file(path) == "Initialization Sequence Completed\n"


def test_watch_interval_is_shorter_than_the_workflow_gap():
    assert jp_vpngate.WATCH_INTERVAL <= 30
