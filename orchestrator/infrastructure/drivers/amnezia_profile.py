"""AmneziaVPN guest envelope from an AWG document; no server/admin settings.

Matches AmneziaVPN 5.0.3.0 AwgClientConfig / AwgProtocolConfig (de93650a).
The complete native document and string-valued protocol fields are both required.
"""

import base64
import binascii
import configparser
import ipaddress
import json
import re
import struct
import zlib
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from orchestrator.domain.models import OrchestratorError

AWG_LEGACY = frozenset(
    (
        "Jc",
        "Jmin",
        "Jmax",
        "S1",
        "S2",
        "S3",
        "S4",
        "H1",
        "H2",
        "H3",
        "H4",
        "I1",
        "I2",
        "I3",
        "I4",
        "I5",
    )
)
AWG3 = frozenset(
    (
        "HeaderProtectionKey",
        "ContentPaddingAddition",
        "RekeyAfterTime",
        "RekeyTimeout",
        "RejectAfterTime",
        "KeepaliveTimeout",
        "MaxHandshakeAttempts",
        "RandomTrailers",
        "DisableCookies",
    )
)
INTERFACE = AWG_LEGACY | AWG3 | {"PrivateKey", "Address", "DNS", "MTU"}
PEER = frozenset(("PublicKey", "PresharedKey", "Endpoint", "AllowedIPs", "PersistentKeepalive"))


def parse(native):
    if not isinstance(native, str) or len(native.encode()) > 65536 or "\0" in native:
        raise ValueError()
    parser = configparser.ConfigParser(
        interpolation=None, delimiters=("=",), strict=True, empty_lines_in_values=False
    )
    parser.optionxform = str
    parser.read_string(native)
    if parser.defaults() or parser.sections() != ["Interface", "Peer"]:
        raise ValueError()
    interface, peer = dict(parser["Interface"]), dict(parser["Peer"])
    if not set(interface) <= INTERFACE or not set(peer) <= PEER:
        raise ValueError()
    if any(
        not value or "\n" in value or "\r" in value
        for value in [*interface.values(), *peer.values()]
    ):
        raise ValueError()
    return interface, peer


def key_bytes(value):
    raw = base64.b64decode(value, validate=True)
    if len(raw) != 32:
        raise ValueError()
    return raw


def version(params):
    toggles = {"RandomTrailers", "DisableCookies"}
    if any(params.get(key) for key in AWG3 - toggles) or any(
        params.get(key, "off").lower() not in ("off", "false", "0") for key in toggles
    ):
        return "3.1"
    if any(params.get(key) for key in ("S3", "S4")) or any(
        "-" in params.get(key, "") for key in ("H1", "H2", "H3", "H4")
    ):
        return "2"
    if any(params.get("I" + str(i)) for i in range(1, 6)):
        return "1.5"
    return None


def guest_profile(native):
    try:
        interface, peer = parse(native)
        private = key_bytes(interface["PrivateKey"])
        public = base64.b64encode(
            X25519PrivateKey.from_private_bytes(private).public_key().public_bytes_raw()
        ).decode()
        key_bytes(peer["PublicKey"])
        if "PresharedKey" in peer:
            key_bytes(peer["PresharedKey"])
        if "HeaderProtectionKey" in interface:
            key_bytes(interface["HeaderProtectionKey"])
        address = interface["Address"]
        # Amnezia's guest client_ip is a single interface address. Do not silently drop extra addresses.
        ipaddress.ip_interface(address)
        dns = [str(ipaddress.ip_address(item.strip())) for item in interface["DNS"].split(",")]
        if not 1 <= len(dns) <= 2:
            raise ValueError()
        host, port = peer["Endpoint"].rsplit(":", 1)
        if host.startswith("[") and host.endswith("]"):
            host = str(ipaddress.IPv6Address(host[1:-1]))
        elif not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
            raise ValueError()
        if not 1 <= int(port) <= 65535:
            raise ValueError()
        allowed = [item.strip() for item in peer["AllowedIPs"].split(",")]
        for item in allowed:
            ipaddress.ip_network(item, strict=False)
        params = {key: value for key, value in interface.items() if key in AWG_LEGACY | AWG3}
        client = {
            "config": native,
            "hostName": host,
            "port": int(port),
            "client_ip": address,
            "client_priv_key": interface["PrivateKey"],
            "client_pub_key": public,
            "clientId": public,
            "server_pub_key": peer["PublicKey"],
            "allowed_ips": allowed,
            **params,
        }
        for source, target, owner in (
            ("PresharedKey", "psk_key", peer),
            ("PersistentKeepalive", "persistent_keep_alive", peer),
            ("MTU", "mtu", interface),
        ):
            if source in owner:
                client[target] = owner[source]
        protocol = {
            "port": port,
            "transport_proto": "udp",
            "isThirdPartyConfig": True,
            "last_config": json.dumps(client, ensure_ascii=False, separators=(",", ":")),
        }
        detected = version(params)
        if detected:
            protocol["protocol_version"] = detected
        payload = {
            "description": "Undercore",
            "hostName": host,
            "dns1": dns[0],
            "dns2": dns[-1],
            "containers": [{"container": "amnezia-awg", "awg": protocol}],
            "defaultContainer": "amnezia-awg",
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        packed = struct.pack(">I", len(raw)) + zlib.compress(raw, 8)
        return "vpn://" + base64.urlsafe_b64encode(packed).rstrip(b"=").decode("ascii")
    except (ValueError, KeyError, TypeError, configparser.Error, binascii.Error):
        # Never include a parser exception: it may contain the private document.
        raise OrchestratorError("node_response_invalid", 503) from None
