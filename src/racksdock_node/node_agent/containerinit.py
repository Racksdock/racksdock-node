"""Entry point of a container-tier rental (PID 1 of the container). A container has no cloud-init, so this applies the subset of
the tenant's cloud-config the platform renders (users with SSH keys, write_files, runcmd), brings up sshd, nested dockerd and
(verify image) DCGM, runs the tenant's launch configuration, then supervises the node-agent.

The files come from the host-agent: /etc/racksdock/user-data (the exact bytes the platform rendered; the node-agent hashes them
into its attestation nonce), /etc/racksdock/image-version, optional dev-trust material and an optional operator key."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import yaml

ETC = Path("/etc/racksdock")
LOG_PREFIX = "racksdock-container-init:"


def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", flush=True)


def plan_cloud_config(cfg: dict, operator_keys: list[str] | None = None) -> list[tuple[str, ...]]:
    """Translate the cloud-config subset into actions (pure; unit-tested). Action tuples:
    ('user', name, sudo_spec_or_None, groups_csv), ('keys', user, key_line), ('file', path, content, mode), ('run', command)."""
    actions: list[tuple[str, ...]] = []
    users = cfg.get("users") or []
    for u in users:
        if not isinstance(u, dict) or not u.get("name"):
            continue
        actions.append(("user", u["name"], str(u["sudo"]) if u.get("sudo") else "", str(u.get("groups", ""))))
        for k in u.get("ssh_authorized_keys") or []:
            actions.append(("keys", u["name"], k))
    for k in cfg.get("ssh_authorized_keys") or []:
        actions.append(("keys", "ubuntu", k))
    # root login with the same keys: the container tier promises root access (docs/01 §10)
    tenant_keys = [a[2] for a in actions if a[0] == "keys"]
    for k in tenant_keys:
        actions.append(("keys", "root", k))
    for k in operator_keys or []:
        actions.append(("keys", "ubuntu", k))
        actions.append(("keys", "root", k))
    for f in cfg.get("write_files") or []:
        if isinstance(f, dict) and f.get("path"):
            actions.append(("file", f["path"], str(f.get("content", "")), str(f.get("permissions", "0644"))))
    for cmd in cfg.get("runcmd") or []:
        actions.append(("run", cmd if isinstance(cmd, str) else " ".join(shlex.quote(str(x)) for x in cmd)))
    return actions


def load_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines (the same file the VM unit reads through EnvironmentFile=); comments and blanks ignored."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"')
    return env


def sh(cmd: str, check: bool = False) -> int:
    r = subprocess.run(["bash", "-lc", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0:
        log(f"command failed ({r.returncode}): {cmd[:120]}: {r.stdout.strip()[-300:]}")
        if check:
            raise SystemExit(r.returncode)
    return r.returncode


def apply(actions: list[tuple[str, ...]]) -> None:
    for a in actions:
        kind = a[0]
        if kind == "user":
            name, sudo, groups = a[1], a[2], a[3]
            if subprocess.run(["id", name], capture_output=True).returncode != 0:
                sh(f"useradd -m -s /bin/bash {shlex.quote(name)}")
            sh(f"usermod -p '*' {shlex.quote(name)}")  # key login only
            if sudo:
                Path("/etc/sudoers.d").mkdir(exist_ok=True)
                Path(f"/etc/sudoers.d/90-{name}").write_text(f"{name} {sudo}\n")
                os.chmod(f"/etc/sudoers.d/90-{name}", 0o440)
            for g in [x.strip() for x in groups.split(",") if x.strip()]:
                sh(f"getent group {shlex.quote(g)} >/dev/null && usermod -aG {shlex.quote(g)} {shlex.quote(name)}")
        elif kind == "keys":
            user, key = a[1], a[2]
            home = Path("/root") if user == "root" else Path(f"/home/{user}")
            if not home.exists():
                continue
            ssh_dir = home / ".ssh"
            ssh_dir.mkdir(mode=0o700, exist_ok=True)
            ak = ssh_dir / "authorized_keys"
            existing = ak.read_text() if ak.exists() else ""
            if key.strip() and key.strip() not in existing:
                ak.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + key.strip() + "\n")
            ak.chmod(0o600)
            sh(f"chown -R {shlex.quote(user)}:{shlex.quote(user)} {shlex.quote(str(ssh_dir))}")
        elif kind == "file":
            path, content, mode = Path(a[1]), a[2], a[3]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            try:
                path.chmod(int(mode, 8))
            except ValueError:
                pass
        elif kind == "run":
            sh(a[1])


def main() -> int:
    log("starting")
    sh("mkdir -p /run/racksdock /run/sshd && ssh-keygen -A >/dev/null 2>&1")
    cfg: dict = {}
    ud = ETC / "user-data"
    if ud.exists():
        try:
            cfg = yaml.safe_load(ud.read_text()) or {}
        except yaml.YAMLError as e:
            log(f"user-data is not valid cloud-config YAML: {e}")
    operator = []
    ok = ETC / "operator_authorized_keys"
    if ok.exists():
        operator = [line for line in ok.read_text().splitlines() if line.strip()]
    apply(plan_cloud_config(cfg if isinstance(cfg, dict) else {}, operator))
    # root login with keys only
    Path("/etc/ssh/sshd_config.d").mkdir(parents=True, exist_ok=True)
    Path("/etc/ssh/sshd_config.d/racksdock.conf").write_text("PermitRootLogin prohibit-password\nPasswordAuthentication no\nUsePAM no\n")
    sh("/usr/sbin/sshd")
    if Path("/usr/bin/dockerd").exists():
        subprocess.Popen(["bash", "-lc", "dockerd --host=unix:///var/run/docker.sock --storage-driver=overlay2 >/var/log/dockerd.log 2>&1"])
    if Path("/usr/bin/nv-hostengine").exists():
        sh("nv-hostengine >/var/log/nv-hostengine.log 2>&1")
    if Path("/usr/local/bin/racksdock-launch").exists():
        subprocess.Popen(["bash", "-lc", "sleep 5; /usr/local/bin/racksdock-launch >/var/log/racksdock-launch.log 2>&1"])
    # the node-agent's environment: what the VM unit sets, plus /etc/racksdock/node-agent.env from the host (dev trust material)
    env = {**os.environ, "RACKSDOCK_NODE_AGENT_MODE": "real", "RACKSDOCK_TRUST_MODE": os.environ.get("RACKSDOCK_TRUST_MODE", "prod")}
    env.update(load_env_file(ETC / "node-agent.env"))
    log(f"sshd up; supervising the node-agent (trust {env.get('RACKSDOCK_TRUST_MODE')}, transport {env.get('RACKSDOCK_AGENT_TRANSPORT')})")
    while True:
        r = subprocess.run(["/usr/local/bin/racksdock-node-agent"], env=env)
        if r.returncode == 0:
            # a clean exit follows the host's teardown: the container is about to be removed; do not re-attest
            log("node-agent finished (teardown); waiting for the host to remove this container")
            time.sleep(3600)
            continue
        log(f"node-agent exited with {r.returncode}; restarting in 2s")
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
