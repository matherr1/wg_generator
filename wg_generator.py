#!/usr/bin/env python3
"""
WireGuard Configuration Generator

This script reads a simple INI file describing nodes (servers/clients),
automatically assigns IPs when needed, handles server-to-server peering
with custom AllowedIPs logic, and generates ready-to-use .conf files.

Features:
- Manual IP assignment support via 'Address' key
- Automatic IP assignment from declared networks
- Bidirectional peer relationships
- Special handling for server ↔ server connections
- Global preshared key
"""

import configparser
import subprocess
import os
import ipaddress
import sys
from typing import Optional, Set


def generate_key(cmd: str = "genkey", input_data: Optional[str] = None) -> str:
    """Generates WireGuard private key, public key or PSK using the 'wg' tool.
    
    Falls back to mock values when 'wg' command is not available (useful for testing).
    """
    try:
        args = ["wg", cmd]
        stdin = input_data.encode() if input_data else None
        return subprocess.check_output(args, input=stdin).decode().strip()
    except FileNotFoundError:
        # Mock for environments without wg tool (CI, testing, etc.)
        return f"MOCK_{cmd.upper()}_{os.urandom(4).hex()}"


class WGNode:
    """Represents a single WireGuard node (server or client)."""

    def __init__(
        self,
        name: str,
        node_type: str = "node",
        network_str: Optional[str] = None,
        address_str: Optional[str] = None,
        endpoint: Optional[str] = None,
        listen_port: Optional[str] = None,
        private_key: Optional[str] = None,
        persistent_keepalive: Optional[str] = None,
    ):
        self.name = name
        self.node_type = node_type.lower()

        # Networks this node belongs to / is authoritative for
        self.networks: Set[str] = set()
        self.auth_networks: Set[str] = set()

        if network_str:
            for n in network_str.split(","):
                net = n.strip()
                if net:
                    self.networks.add(net)
                    self.auth_networks.add(net)

        # Manually assigned IPs 
        self.assigned_ips: Set[str] = set()
        if address_str:
            for a in address_str.split(","):
                addr = a.strip()
                if addr:
                    self.assigned_ips.add(addr)
                    # Also register the subnet
                    try:
                        net = ipaddress.ip_interface(addr).network
                        self.networks.add(str(net))
                    except ValueError:
                        pass

        self.endpoint = endpoint
        self.listen_port = listen_port
        self.private_key = private_key or generate_key("genkey")
        self.public_key = generate_key("pubkey", self.private_key)
        self.peers: list["WGNode"] = []
        self.persistent_keepalive = persistent_keepalive


class WGGenerator:
    """Main generator class for WireGuard configurations."""

    def __init__(self):
        self.nodes: dict[str, WGNode] = {}
        self.preshared_key: Optional[str] = None

    def load_from_ini(self, file_path: str) -> None:
        """Load configuration from INI file and build node + peer graph."""
        config = configparser.ConfigParser()
        config.optionxform = str  # Preserve case of keys
        config.read(file_path)

        # Global settings
        if "GLOBAL" in config:
            self.preshared_key = config.get("GLOBAL", "PresharedKey", fallback=None)

        if not self.preshared_key:
            self.preshared_key = generate_key("genpsk")

        # Create nodes
        for section in config.sections():
            if section == "GLOBAL":
                continue
            self.nodes[section] = WGNode(
                name=section,
                node_type=config.get(section, "type", fallback="node"),
                network_str=config.get(section, "network", fallback=None),
                address_str=config.get(section, "Address", fallback=None),
                endpoint=config.get(section, "Endpoint", fallback=None),
                listen_port=config.get(section, "ListenPort", fallback=None),
                private_key=config.get(section, "PrivateKey", fallback=None),
                persistent_keepalive=config.get(section, "PersistentKeepalive", fallback=None),
            )

        # Build bidirectional peer relationships
        for section in config.sections():
            if section == "GLOBAL":
                continue
            peer_list = config.get(section, "peers", fallback="")
            if peer_list:
                for p_name in [p.strip() for p in peer_list.split(",") if p.strip()]:
                    if p_name in self.nodes:
                        n = self.nodes[section]
                        p = self.nodes[p_name]
                        if p not in n.peers:
                            n.peers.append(p)
                        if n not in p.peers:
                            p.peers.append(n)

    def infer_and_assign_ips(self) -> None:
        """Propagate networks and auto-assign IPs from pools when not manually set."""
        # 1. Propagate networks from peers to clients (so clients know all reachable subnets)
        changed = True
        while changed:
            changed = False
            for node in self.nodes.values():
                if node.node_type == "server":
                    continue
                for peer in node.peers:
                    new_nets = peer.auth_networks - node.networks
                    if new_nets:
                        node.networks.update(new_nets)
                        changed = True

        # 2. Auto-assign IPs from each unique subnet
        unique_subnets = {n for node in self.nodes.values() for n in node.networks}

        for net_str in unique_subnets:
            net = ipaddress.IPv4Network(net_str)
            hosts = list(net.hosts())

            net_nodes = [n for n in self.nodes.values() if net_str in n.networks]

            # Reserved IPs (manually assigned)
            reserved_ips = set()
            for n in net_nodes:
                for assigned in n.assigned_ips:
                    try:
                        if ipaddress.ip_interface(assigned).ip in net:
                            reserved_ips.add(ipaddress.ip_interface(assigned).ip)
                    except ValueError:
                        continue

            # Nodes that still need an IP in this subnet
            needs_ip = [
                n for n in net_nodes
                if not any(ipaddress.ip_interface(a).ip in net for a in n.assigned_ips)
            ]

            servers = sorted([n for n in needs_ip if n.node_type == "server"], key=lambda x: x.name)
            others = sorted([n for n in needs_ip if n.node_type != "server"], key=lambda x: x.name)

            available_hosts = [h for h in hosts if h not in reserved_ips]

            idx = 0
            for node in (servers + others):
                if idx < len(available_hosts):
                    node.assigned_ips.add(f"{available_hosts[idx]}/{net.prefixlen}")
                    idx += 1

    def save_configs(self, output_dir: str = "wg_configs") -> None:
        """Generate one WireGuard .conf file per node."""
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        for name, node in self.nodes.items():
            lines = [
                f"# Name: {node.name}",
                f"# Type: {node.node_type}",
                "[Interface]",
            ]

            if node.listen_port:
                lines.append(f"ListenPort = {node.listen_port}")

            lines.append(f"PrivateKey = {node.private_key}")

            if node.assigned_ips:
                lines.append(f"Address = {', '.join(sorted(node.assigned_ips))}")

            # Peer sections
            for peer in node.peers:
                lines.extend(["", f"# Name: {peer.name}", "[Peer]"])

                if peer.endpoint:
                    port = peer.listen_port or 51820
                    lines.append(f"Endpoint = {peer.endpoint}:{port}")

                lines.append(f"PublicKey = {peer.public_key}")
                lines.append(f"PresharedKey = {self.preshared_key}")

                # PersistentKeepalive for client→server and server↔server
                if (peer.persistent_keepalive and
                    ((node.node_type == "node" and peer.node_type == "server") or
                     (node.node_type == "server" and peer.node_type == "server"))):
                    lines.append(f"PersistentKeepalive = {peer.persistent_keepalive}")

                # AllowedIPs logic
                if node.node_type == "server" and peer.node_type == "node":
                    # Server accepting traffic from its own clients
                    valid_peer_ips = []
                    for peer_ip_cidr in peer.assigned_ips:
                        try:
                            ip_obj = ipaddress.ip_interface(peer_ip_cidr).ip
                            for srv_net_str in node.auth_networks:
                                if ip_obj in ipaddress.ip_network(srv_net_str):
                                    valid_peer_ips.append(f"{ip_obj}/32")
                                    break
                        except ValueError:
                            continue
                    lines.append(f"AllowedIPs = {', '.join(sorted(valid_peer_ips))}")

                elif node.node_type == "server" and peer.node_type == "server":
                    # Server-to-server special logic
                    # Remote server is acting as a CLIENT to us.
                    # Therefore we treat it like any other client:
                    # - Allow only its tunnel IP inside OUR network as /32
                    # - Additionally, because it's a server for us, we also allow its entire LAN
                    #   so we can reach all its clients.


                    combined: set[str] = set(peer.auth_networks)

                    #   Allow the remote server's IP address that belongs to THIS server's network
                    #    (this is the "client IP" from our point of view)
                    if node.auth_networks and peer.assigned_ips:
                        try:
                            node_net = ipaddress.ip_network(next(iter(node.auth_networks)))
                            for peer_ip_cidr in peer.assigned_ips:
                                try:
                                    peer_ip = ipaddress.ip_interface(peer_ip_cidr).ip
                                    if peer_ip in node_net:
                                        combined.add(f"{peer_ip}/32")
                                        break
                                except ValueError:
                                    continue
                        except ValueError:
                            pass

                    lines.append(f"AllowedIPs = {', '.join(sorted(combined))}")

                else:
                    lines.append(f"AllowedIPs = {', '.join(sorted(peer.auth_networks))}")


            # Write the config file
            with open(os.path.join(output_dir, f"{name}.conf"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
                f.write("\n")

        print(f"Success: .conf files saved to ./{output_dir}")

    def save_inferred_ini(self, output_path: str) -> None:
        """Save an INI file with all inferred/resolved values (great for debugging)."""
        inferred_cfg = configparser.ConfigParser()
        inferred_cfg.optionxform = str
        inferred_cfg.add_section("GLOBAL")
        inferred_cfg.set("GLOBAL", "PresharedKey", self.preshared_key or "")

        for name, node in self.nodes.items():
            inferred_cfg.add_section(name)
            inferred_cfg.set(name, "type", node.node_type)
            inferred_cfg.set(name, "Address", ", ".join(sorted(node.assigned_ips)))
            inferred_cfg.set(name, "peers", ", ".join(p.name for p in node.peers))
            inferred_cfg.set(name, "PrivateKey", node.private_key)
            inferred_cfg.set(name, "PublicKey", node.public_key)

            if node.endpoint:
                inferred_cfg.set(name, "Endpoint", node.endpoint)
            if node.listen_port:
                inferred_cfg.set(name, "ListenPort", str(node.listen_port))
            if node.persistent_keepalive:
                inferred_cfg.set(name, "PersistentKeepalive", str(node.persistent_keepalive))

        with open(output_path, "w", encoding="utf-8") as f:
            inferred_cfg.write(f)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python wg_generator.py <config.ini>")
        sys.exit(1)

    ini_filename = sys.argv[1]
    gen = WGGenerator()

    if os.path.exists(ini_filename):
        gen.load_from_ini(ini_filename)
        gen.infer_and_assign_ips()
        gen.save_inferred_ini(f"inferred_{ini_filename}")
        gen.save_configs()
    else:
        print(f"Error: File '{ini_filename}' not found.")
        sys.exit(1)
