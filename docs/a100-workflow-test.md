# A100 workflow test (temporary)

Runs the miner → validator → user workflow with one NVIDIA A100. Exercises what the simulator cannot: the real SSH runner with the
pinned host key, the parsers on real `nvidia-smi`, `dcgmi`, `fio`, `iperf3`, STREAM and sysbench output, the node-agent's NVML
health counters, a real login into the node, and (Topology B) real networking between validator, platform and node.

Not exercised, by hardware limitation: GPU attestation (Ampere has no device attestation; a dev certificate authority stands in),
VM creation and passthrough (the box is used as-is through the `static` host-agent backend), NVLink and the 8-GPU workload checks.

Safety: the node-agent contains no power-off or reboot command. On the box, the test changes only `/home/ubuntu/.ssh/authorized_keys`
(tenant keys added at launch, restored at delete), `/tmp/racksdock-fio` (scratch), and it stops and restarts `sshd` once during the
admission test's "recover" step (existing SSH sessions survive that).

## 1. The parties and their processes

| Party | Process | Package | Machine |
|---|---|---|---|
| Miner | `racksdock-host-agent` (static mode) and the `racksdock-node-agent` it starts | `racksdock-node` | the A100 box |
| Miner | `racksdock-miner bootstrap` (one-off: commits the node's registration hash on chain, registers with the platform) | `racksdock-subnet` | wherever the chain is reachable; with the mock chain, next to the marketplace |
| Platform (owner) | `racksdock-marketplace serve` | `racksdock-platform` | owner machine |
| Validator | `racksdock-validator run` and an `iperf3 -s` for the network check | `racksdock-subnet` | validator machine |
| User | `racksdock` CLI | `racksdock-platform` | anywhere |

Production: miner box runs `racksdock-node` + the miner neuron; validators run `racksdock-subnet` only; the owner runs
`racksdock-platform`. The mock chain is a JSON file, so in a test everything that writes or reads the chain (marketplace, miner
bootstrap, validator) must sit on one machine. With `RACKSDOCK_CHAIN=subtensor` that constraint disappears.

Wallets: the test uses the mock chain and plain hotkey strings, no wallet. In production (`RACKSDOCK_CHAIN=subtensor`) the miner's
**hotkey** signs the on-chain registration commitment in `racksdock-miner bootstrap` and receives emissions; the validator's
**hotkey** signs `set_weights` every tempo and must hold a validator permit; the **coldkey** is used only by you with `btcli`
(register, stake) and is never read by any racksdock process.

What a validator receives: the node list (GPU identities, miner hotkey, claimed spec), SSH access only into verify sessions it
created with its own key, signed challenge responses with the fixed health allowlist, and the public evidence feed. It never
receives tenant keys, the miner's host key seed, the platform database, API secrets, or the dev CA private keys.

## 2. Server requirements

| | Miner box (the A100) | Validator / platform box (Topology B only) |
|---|---|---|
| GPU | 1× A100, `nvidia-smi` works | none |
| OS / Python | Ubuntu 24.04 (22.04 with python3.11 from deadsnakes) | Ubuntu 22.04/24.04, Python 3.11+ |
| Resources | whatever the box has; 15 GB free in `/tmp` | 2 vCPU, 4 GB RAM, 20 GB disk |
| Access | root; sshd reachable on a port the validator can reach | root or sudo |
| Network | Topology B: outbound to the platform's port 8000 and the validator's iperf3 port 5201; inbound SSH from the validator | inbound 8000 and 5201 from the A100 box |

## 3. Topology A: everything on the A100 box (do this first)

All five processes run on the box; the validator SSHes to `127.0.0.1`. No firewall work.

**On the A100 box, as root:**

1. Get the code: `git clone` the three public repos side by side into `/opt/racksdock-src/` (or rsync them from your laptop).
2. Check prerequisites:
   ```
   nvidia-smi --query-gpu=name,pci.device_id,memory.total --format=csv   # 0x20B510DE -> 10de:20b5; allowed: 20b0 20b2 20b5 20f1 20f3 20b7
   python3.11 --version    # 22.04: apt-get install -y software-properties-common && add-apt-repository -y ppa:deadsnakes/ppa && apt-get install -y python3.11 python3.11-venv python3.11-dev
   grep -E "^(PubkeyAuthentication|AllowUsers)" /etc/ssh/sshd_config    # pubkey yes; if AllowUsers is set, add ubuntu
   df -h /tmp
   ```
3. Setup (tools, DCGM, the `ubuntu` SSH user with sudo, packages into `/opt/racksdock/venv`, the dev CA, `/etc/racksdock/a100.env`):
   ```
   PYTHON=python3.11 REPOS=/opt/racksdock-src bash /opt/racksdock-src/racksdock-platform/dev/a100/setup-box.sh
   ```
4. Run the test:
   ```
   /opt/racksdock/venv/bin/python /opt/racksdock-src/racksdock-platform/dev/a100/run_a100_test.py --duration 600
   ```
   The script plays every party in turn and labels each step: it starts `iperf3 -s` and the marketplace (platform), the host-agent
   (miner), runs `racksdock-miner bootstrap` (miner), starts `racksdock-validator run` (validator), then uses the `racksdock` CLI
   (user). While the validator works it prints a progress line every 30 seconds with the node's badges, the evidence counts and
   the validator's last log line, and surfaces validator errors as they appear.
5. Expected: admission in 2–3 min, qualification in 10–12 min, `qualified in Ns: profile a100-dev A100x1-DEV`, a `measured:` block,
   then `instance …: status=active ip=127.0.0.1:22 attestation=verified gpu_ids_match=True`, `attestation: VERIFIED`, and
   `in-rental checks: N · last: attestation_ok=True reachable=True error=None` every 30 s. It prints the real `ssh -i …` login and the
   console URL (reach it with `ssh -L 8000:127.0.0.1:<port> root@<box>` from your laptop; login demo@example.com / password1).
6. If it stalls or quarantines: `tail -f /var/lib/racksdock-a100-test/logs/validator.log`, and `curl -s <API>/v1/evidence?kind=qualification`.
   Usual causes: `Q4` DCGM not running (`nv-hostengine`), `Q12` no iperf3 server, `Q2` device id not in the allowlist, `Q11` no space in `/tmp`,
   sshd not running after a previous aborted run (`systemctl start ssh`).
   To rerun from scratch: `rm -rf /var/lib/racksdock-a100-test` and run step 4 again.

## 4. Topology B: validator and platform on another machine

Box **A** = the A100 (miner). Box **V** = platform + validator (two processes; V holds the mock chain file they share).
Nothing is copied between the machines. In dev trust mode the platform plays NVIDIA's role: it creates the simulated device CA at
first start, publishes its public part at `GET /v1/dev/devca`, and issues simulated GPU identities at `POST /v1/dev/issue-node`.
The validator downloads the public root from the platform by itself. (In production there is no dev CA; evidence verifies against
NVIDIA's public root certificate, which ships with the attestation SDK.)

**On V (platform + validator), as root**

1. Install: Python 3.11+, `git`, `iperf3` (answer Yes to "start as a daemon"). Clone the three repos side by side, create a venv,
   `pip install -e racksdock-subnet[validator] -e racksdock-node -e racksdock-platform`. Open ports 8000 and 5201 to A.
2. Environment file `/etc/racksdock/env`:
   ```
   RACKSDOCK_PROFILE=a100-dev
   RACKSDOCK_TRUST_MODE=dev
   RACKSDOCK_DEV_CA_DIR=/etc/racksdock/devca
   RACKSDOCK_CHAIN=mock
   RACKSDOCK_CHAIN_FILE=/var/lib/racksdock/chain.json
   RACKSDOCK_PLATFORM_URL=http://127.0.0.1:8000
   ```
3. Start the platform (creates `/etc/racksdock/devca` on first start):
   ```
   set -a; . /etc/racksdock/env; set +a
   RACKSDOCK_DB_URL=sqlite:////var/lib/racksdock/marketplace.db RACKSDOCK_BASE_URL=http://<V>:8000 racksdock-marketplace serve --host 0.0.0.0 --port 8000
   ```

**On A (miner), as root**

4. Install Python 3.11+ and `git`; clone the three repos into `/opt/racksdock-src`. Then the setup, pointing at the platform (it fetches
   the public CA and this box's simulated GPU identity from the platform):
   ```
   PLATFORM_URL=http://<V>:8000 PUBLIC_IP=<A> PYTHON=python3.11 REPOS=/opt/racksdock-src bash /opt/racksdock-src/racksdock-platform/dev/a100/setup-box.sh
   ```
5. Start the host-agent:
   ```
   set -a; . /etc/racksdock/a100.env; set +a
   RACKSDOCK_HOST_AGENT_MODE=static RACKSDOCK_PLATFORM_WS=ws://<V>:8000/v1/hosts/ws RACKSDOCK_MINER_HOTKEY=5FminerA100 RACKSDOCK_REGION=lab \
     RACKSDOCK_STATE_DIR=/var/lib/racksdock-host-agent /opt/racksdock/venv/bin/racksdock-host-agent
   ```
6. In a second terminal, print the host key (needed on V):
   ```
   /opt/racksdock/venv/bin/python -c "from racksdock_subnet.core.keys import SigningKey; print(SigningKey.load_or_create('/var/lib/racksdock-host-agent/host.seed').public.hex)"
   ```

**On V (platform + validator), as root, with the environment from step 2**

7. Register the miner (this is the miner's command; it runs here only because the mock chain file is here):
   ```
   RACKSDOCK_HOTKEY=5FminerA100 RACKSDOCK_STATE_DIR=/var/lib/racksdock-miner racksdock-miner bootstrap --host-pubkey <hex from step 6>
   ```
8. Start the validator (it fetches the public dev CA from the platform on first start):
   ```
   RACKSDOCK_HOTKEY=5GvalA100 RACKSDOCK_RUNNER=ssh RACKSDOCK_SSH_USER=ubuntu RACKSDOCK_IPERF_ENDPOINTS=<V> RACKSDOCK_STATE_DIR=/var/lib/racksdock-validator \
     RACKSDOCK_INRENTAL_S=60 RACKSDOCK_REACH_S=15 RACKSDOCK_TEMPO_S=300 RACKSDOCK_LOOP_S=2 racksdock-validator run
   ```
   About 15 minutes later `curl -s http://127.0.0.1:8000/v1/nodes` shows the node with `"available": true`.

**From any machine with the `racksdock` CLI (user)**

9. ```
   racksdock auth signup you@example.com password1 --api-url http://<V>:8000
   racksdock ssh-keys generate --name laptop
   curl -s -X POST http://<V>:8000/v1/billing/credits/purchase -H "X-API-KEY: <key from ~/.racksdock/config.json>" -H 'Content-Type: application/json' -d '{"cents":100000}'
   racksdock nodes list --available
   racksdock instances create --name a100-demo --node <node_id> --os ubuntu24.04
   racksdock instances get <id>            # until status active
   racksdock ssh <id>                      # verifies attestation, pins the host key, logs in as ubuntu@<A>
   racksdock instances delete <id>
   ```

## 5. Remove after the test

A100-only files (delete all of them):

- `racksdock-subnet/src/racksdock_subnet/core/spec/profiles/a100-dev.json`
- `racksdock-subnet/src/racksdock_subnet/core/spec/reference-sets/ref_a100_dev_v0.json`
- the three `a100` tests in `racksdock-subnet/tests/core/test_profile.py` (keep `test_profiles_available_and_defaults`)
- `racksdock-platform/dev/a100/`
- this file, and the `a100-dev` sentence in the workspace README

On the A100 box: `userdel -r ubuntu`, `rm -rf /opt/racksdock /etc/racksdock /var/lib/racksdock-* /tmp/racksdock-fio`.

Generic pieces that may stay (not A100-specific): the hardware profile mechanism (`core/profile.py`, `spec/profiles/b300x8.json`),
the `static` host-agent backend and its test (lab boxes without passthrough, never production), `RACKSDOCK_IPERF_ENDPOINTS`,
`RACKSDOCK_SSH_USER`, and the `racksdock-stream` fallback tool.
