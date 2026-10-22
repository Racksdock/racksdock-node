"""Port forwarding for rentals on single-IP hosts (docs/01 §7 NAT mode). The host owns the public address; a rental sits on a
private bridge (libvirt NAT network for VMs, docker0 for containers). Two exposure policies:

  all  (default)  the whole public IP is the tenant's: every TCP port except the host's reserved ones (its own sshd) and every
                  UDP port are DNATed 1:1 to the rental, so a service on port 8000 inside is reachable at <public_ip>:8000; the
                  rental's own firewall (ufw rules from the launch form) decides what is open. SSH stays on the advertised
                  nat_ssh_port -> 22 as well.
  ssh             only <public_ip>:<nat_ssh_port> -> rental:22 (tunnels for everything else).

The catch-alls match on the host's EXTERNAL interface (`ext_iface`, the device of the default route), not on the public address:
many hosts sit behind a provider's 1:1 NAT and see their public IP only as the destination of packets that already carry the
host's private address (the 4090 box: public 103.x, interface 172.19.x). Matching the interface also keeps the rental's own
traffic to the host (DHCP/DNS on the libvirt or docker bridge) out of the catch-all.

Every rule carries a comment tag `racksdock-nat-<instance_id>` so it can be removed by tag at teardown or at host-agent start.
Rules are inserted at position 1 in reverse order of the list, so the specific SSH rules end up above the catch-alls and both
precede libvirt's and Docker's own chains."""
from __future__ import annotations

import shlex

NAT_TAG = "racksdock-nat"
EXPOSURES = ("all", "ssh")


def forward_rules(host_ip: str, guest_ip: str, ssh_port: int, tag: str, expose: str = "all", reserved_ports: list[int] | None = None,
                  ext_iface: str | None = None) -> list[tuple[str, list[str]]]:
    """iptables rules (table, args-after-'-A') for one rental, in insertion order (each inserted at position 1). The catch-alls
    match on ext_iface when known (see the module docstring), else on the destination address host_ip."""
    if expose not in EXPOSURES:
        raise ValueError(f"unknown exposure {expose!r}")
    c = ["-m", "comment", "--comment", tag]
    rules: list[tuple[str, list[str]]] = []
    if expose == "all":
        keep = sorted({int(p) for p in (reserved_ports or [22])} | {int(ssh_port)})
        ports = ",".join(str(p) for p in keep)
        where = ["-i", ext_iface] if ext_iface else ["-d", host_ip]
        rules += [
            ("nat", ["PREROUTING", *where, "-p", "tcp", "-m", "multiport", "!", "--dports", ports, "-j", "DNAT", "--to-destination", guest_ip, *c]),
            ("nat", ["PREROUTING", *where, "-p", "udp", "-j", "DNAT", "--to-destination", guest_ip, *c]),
            ("filter", ["FORWARD", "-d", guest_ip, "-j", "ACCEPT", *c]),
        ]
    rules += [
        ("nat", ["PREROUTING", "-p", "tcp", "--dport", str(ssh_port), "-j", "DNAT", "--to-destination", f"{guest_ip}:22", *c]),
        ("nat", ["OUTPUT", "-p", "tcp", "-d", host_ip, "--dport", str(ssh_port), "-j", "DNAT", "--to-destination", f"{guest_ip}:22", *c]),
        ("filter", ["FORWARD", "-d", guest_ip, "-p", "tcp", "--dport", "22", "-j", "ACCEPT", *c]),
    ]
    return rules


def parse_tagged_rules(iptables_save: str, tag_prefix: str, exclude_prefix: str | None = None) -> list[tuple[str, list[str]]]:
    """From `iptables-save` output, every rule whose comment starts with tag_prefix (and not with exclude_prefix), as
    (table, args-after-'-A')."""
    out: list[tuple[str, list[str]]] = []
    table = ""
    for line in iptables_save.splitlines():
        if line.startswith("*"):
            table = line[1:].strip()
        elif line.startswith("-A "):
            args = shlex.split(line[3:])
            if "--comment" in args:
                comment = args[args.index("--comment") + 1]
                if comment.startswith(tag_prefix) and not (exclude_prefix and comment.startswith(exclude_prefix)):
                    out.append((table, args))
    return out


async def detect_ext_iface(run) -> str | None:
    """The device of the default route (where tenant traffic from the internet arrives)."""
    import json

    cp = await run("ip", "-j", "route", "get", "1.1.1.1", check=False)
    try:
        return json.loads(cp.stdout or "[]")[0].get("dev") or None
    except (ValueError, IndexError, AttributeError):
        return None


async def apply_rules(run, rules: list[tuple[str, list[str]]]) -> None:
    """`run` is the backend's async command runner (_run). Each rule goes to position 1 of its chain."""
    for table, args in rules:
        await run("iptables", "-w", "-t", table, "-I", args[0], "1", *args[1:])


async def clear_rules(run, which, tag_prefix: str, exclude_prefix: str | None = None) -> int:
    """Delete every rule tagged tag_prefix (except exclude_prefix). `which` resolves a binary (shutil.which)."""
    if which("iptables-save") is None:
        return 0
    cp = await run("iptables-save", check=False)
    rules = parse_tagged_rules(cp.stdout, tag_prefix, exclude_prefix)
    for table, args in rules:
        await run("iptables", "-w", "-t", table, "-D", *args, check=False)
    return len(rules)
