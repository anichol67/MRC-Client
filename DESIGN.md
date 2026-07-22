# MRC emu — Design & Decision Log

**Version**: 1.0

## Project Overview

**MRC emu** (emu for short) is a web-based software agent that emulates an OCP MRC (Multipath Reliable Connection) end host per the OCP MRC Specification Revision 1.0 (03/21/26). It serves as a **packet generation, network provisioning, and test orchestration tool** for MRC-compliant AI/ML fabrics.

**Spec source**: OCP-MRC-1.0, joint contribution from AMD, Broadcom, Intel, Microsoft, NVIDIA, OpenAI.

---

## v1.0 Requirements Summary

| # | Requirement | Section |
|---|------------|---------|
| 1 | Packet generation tool for MRC-compliant endpoints | Key Decisions §1 |
| 2 | EV programming (ECMP, Structured EV, SRv6) + packet formatting + CC + probing | §2 |
| 3 | Python + Flask web GUI, headless Linux, no desktop required | §3 |
| 4 | IPv6 network configuration via Linux `ip` commands | §4 |
| 5 | Full EV state machine (GOOD/SKIP/DENIED/ASSUMED_BAD) per spec Figure 5 | §5 |
| 6 | MRC header serialization with round-trip verification | §6 |
| 7 | Configurable listen port (default 5001) | §7 |
| 8 | Docker container deployment for Containerlab | §8 |
| 9 | Two deployment models: offline generation + live provisioning | §9 |
| 10 | Arista EOS provisioning via eAPI (IPv6, static routes, SRv6) | §10 |
| 11 | Two-plane leaf-spine topology (configurable dimensions) | §11 |
| 12 | IPv6 addressing derived from single template base prefix | §12 |
| 13 | SRv6 locator on separate configurable base address | §13 |
| 14 | EV↔SRv6 path mapping (EV encodes plane + spine, derives uSID stack per destination) | §14 |
| 15 | NCCL collective communication emulation (AllReduce, AllGather, AllToAll, etc.) | §15 |
| 16 | Real ECN/WRED on cEOS switches (Approach A) | §16 |
| 17 | Receiver-side congestion/fault simulation fallback (Approach B) | §17 |
| 18 | Sender traffic rate control (burst, sustained, multi-QP) | §18 |
| 19 | SRv6 topology visualization with path highlighting | §19 |
| 20 | Link/path failure simulation with failover visualization | §20 |
| 21 | Per-path MRC state display (RTT, ECN rate, loss, cwnd) from control plane | §21 |
| 22 | Probe-driven path visualization with live state updates and color-coded health | §22 |
| 23 | Cross-platform offline mode (Mac/Windows) — full GUI without live network | §23 |
| 24 | Hover tooltips on topology nodes/links (IPv6, SRv6, uSID, interfaces) | §24 |
| 25 | Hosts connected to all planes in fabric view | §25 |
| 26 | Unified simulation page with timed run, fabric view, and live failure injection | §26 |
| 27 | Arista CloudVision-style GUI (dark sidebar nav, blue accents, metric cards) | §27 |

---

## Key Decisions

### 1. Use Case — Packet Generation Tool
**Decision**: The emulator is a packet generation/injection tool, not a full transport simulation or hardware replacement.
**Why**: The primary need is to generate spec-compliant MRC packets on the wire to test real NIC implementations. This means precise header formatting matters more than full transport semantics.

### 2. Scope — EV Programming + Packet Formatting + CC + Probing
**Decision**: Implement all four areas:
- EV profile programming (all three modes: ECMP, Structured EV, SRv6)
- MRC packet construction/parsing for all opcodes
- NSCC congestion control (spec Section 8)
- Path probing (EV Probes, Reliability Probes, Port Status Updates)

**Why**: Initially scoped to EV + packet formatting only, but expanded per user request to include congestion control and probing for a complete endpoint emulator.

### 3. Technology Stack — Python + Flask (Web GUI)
**Decision**: Python 3 with Flask web framework, Bootstrap 5 dark theme via CDN.
**Why**: 
- Must run on headless Linux hosts without a desktop environment
- Flask server listens on 0.0.0.0 — accessed remotely via browser from any machine
- Python + scapy provides raw packet crafting without kernel RDMA drivers
- No native GUI dependencies to install on the Linux host

### 4. Network Configuration via `ip` Commands
**Decision**: Use subprocess calls to Linux `ip -6` commands (with JSON output where available) rather than pyroute2.
**Why**: Fewer dependencies, works on any Linux distro with iproute2 installed. Falls back to text parsing if JSON output unavailable. Read-only mode when not running as root.

### 5. EV State Machine per Spec Figure 5
**Decision**: Implement the full EV state machine with four states:
- GOOD → SKIP: on SACK(m=SKIP_ONCE) or NACK(TRIMMED)
- GOOD → DENIED: admin disable
- GOOD → ASSUMED_BAD: SACK(m=ALWAYS_SKIP) or bad path detected
- SKIP → GOOD: timeout-based recovery
- DENIED → GOOD: admin enable
- ASSUMED_BAD → GOOD: probe response with m=NONE or SKIP_ONCE

**Why**: Matches the spec exactly. The GUI allows manual state transitions for testing.

### 6. Header Serialization — Python Dataclasses with to_bytes()/from_bytes()
**Decision**: Each MRC header is a `@dataclass` with explicit `to_bytes()` and `from_bytes()` methods using `struct.pack`/`struct.unpack`.
**Why**: Provides exact control over bit-level wire format. Round-trip serialization is verified by tests. Easier to debug than scapy layer definitions for a custom protocol.

### 7. Default Port — 5001
**Decision**: Flask listens on port 5001 (changed from initial 5000).
**Why**: User preference. Configurable via `LISTEN_PORT` env var or editing config.py.

---

## Architecture

```
Linux host (headless)              Any machine with browser
┌──────────────────────┐           ┌──────────────────────┐
│  Flask server :5001  │◄─────────►│  Browser (GUI)       │
│  + scapy packet I/O  │  HTTP     │  laptop/workstation  │
│  0.0.0.0 binding     │           └──────────────────────┘
└──────────────────────┘
```

### Module Layout

| Module | Spec Sections | Purpose |
|--------|---------------|---------|
| `core/mrc_headers.py` | 6.2.2, 7.5.5, 7.5.6 | All MRC header dataclasses (BTH, METH, TSETH, RETH, SETH, NETH, PETH, ERTH, EETH, CCState, AETH, ImmDt) |
| `core/ev_engine.py` | 9.1–9.4, Fig 5 | EV profiles, state machine, structured EV builder, SRv6 address construction |
| `core/congestion.py` | 8.1–8.7, Table 8-1/8-2 | NSCC algorithm: QPCC, cwnd, RTT, ECN, responder flow control |
| `core/probing.py` | 6.5, 7.3, 7.4.6–7.4.8 | EV Probes, Reliability Probes, Port Status Updates |
| `core/packet_builder.py` | 6.2.2, 7.5.5 | Full packet assembly (Eth/IPv6/UDP/BTH/MRC/iCRC) |
| `core/packet_parser.py` | — | Decode raw packets into structured MRC representations |
| `core/qp_manager.py` | 6.1, 6.3, 7.4, 7.5 | QP lifecycle (RESET→INIT→RTR→RTS→ERROR), PSN tracking |
| `core/network_config.py` | 11.5 | IPv6 address/route/neighbor management via Linux `ip` |

### GUI Pages

| Page | URL | Purpose |
|------|-----|---------|
| Host | `/host` | Interface listing, IPv6 address management, RoCE settings |
| Network | `/network` | IPv6 routing table, neighbor table, route/neighbor CRUD |
| EV Profiles | `/ev` | Create/manage profiles, add/generate EVs, hop-field editor, EV state |
| Queue Pairs | `/qp` | Create/modify/destroy QPs, state machine transitions |
| Packet Builder | `/packets` | Build any MRC opcode, hex dump, decoded JSON view |
| Congestion Ctrl | `/cc` | QPCC creation, NSCC config, ACK simulation |
| Probing | `/probing` | Send probes, probe-all-EVs sweep, path health view |

---

## MRC Opcodes Supported

| Opcode | Hex | Description | Header Stack |
|--------|-----|-------------|--------------|
| RDMA_WRITE_FIRST | 0xC6 | Multi-packet write, first | METH, [TSETH], RETH, Payload |
| RDMA_WRITE_MIDDLE | 0xC7 | Multi-packet write, middle | METH, [TSETH], RETH, Payload |
| RDMA_WRITE_LAST | 0xC8 | Multi-packet write, last | METH, [TSETH], RETH, Payload |
| RDMA_WRITE_LAST_IMM | 0xC9 | Last with Immediate | METH, [TSETH], RETH, ImmDt, Payload |
| RDMA_WRITE_ONLY | 0xCA | Single-packet write | METH, [TSETH], RETH, Payload |
| RDMA_WRITE_ONLY_IMM | 0xCB | Single-packet write with Imm | METH, [TSETH], RETH, ImmDt, Payload |
| ACKNOWLEDGE | 0xD1 | RDMA Transport ACK | AETH |
| ENDPOINT_REQUEST | 0xD8 | EV Probe / Port Status req | ERTH |
| ENDPOINT_RESPONSE | 0xD9 | EV Probe / Port Status resp | EETH |
| RELIABILITY_SACK | 0xDC | Selective ACK | SETH, CC_STATE |
| RELIABILITY_NACK | 0xDD | Negative ACK | NETH |
| RELIABILITY_PROBE_REQ | 0xDE | Reliability Probe request | PETH |

---

## EV Modes

| Mode | Format Width | Encoding | Spec Section |
|------|-------------|----------|--------------|
| ECMP | 16-bit | UDP source port + IPv6 flow label | 9.3.3 |
| Structured EV | 32-bit | UDP.src_port[15:0] ‖ IPv6.flow_lbl[15:0], hop fields packed MSB-first | 9.1, 9.3.4 |
| SRv6 uSID | 128-bit | LID + up to 6 uSIDs in IPv6 dst addr | 9.2, 9.3.5 |
| SRv6 uSID+SRH | 256-bit | Above + optional SRH with compressed segment list | 9.2, 9.3.5 |

---

## NSCC Congestion Control Summary

Per spec Section 8 and UltraEthernet NSCC algorithm:

| ECN | RTT vs target_Qdelay | Action |
|-----|---------------------|--------|
| Not set | RTT < target | Proportional increase |
| Not set | RTT >= target | Fair (additive) increase |
| Set | RTT >= target | Multiplicative decrease |
| Set | RTT < target | No change |

QPCC schedule states: IDLE → ACTIVE → READY → PENDING → IDLE (per spec Figure 4).

---

## Configuration

All settings in `config.py`, overridable via environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `LISTEN_HOST` | `0.0.0.0` | Flask bind address |
| `LISTEN_PORT` | `5001` | Flask port |
| `ROCE_UDP_PORT` | `4971` | RoCE UDP destination port |
| `DSCP_CONTROL` | `46` | DSCP for control traffic (SACK/NACK) |
| `DSCP_TRIMMABLE` | `4` | DSCP for trimmable data |
| `DSCP_TRIMMED` | `12` | DSCP for trimmed packets |

---

## Containerlab Deployment

### 8. Container Deployment
**Decision**: Package emulator as Docker container for Containerlab.
**Why**: Tool runs within a Containerlab environment alongside Arista cEOS switches in a leaf-spine topology.

- Dockerfile: Python 3.12-slim, iproute2, tcpdump, scapy, Flask
- Requires `CAP_NET_RAW` + `CAP_NET_ADMIN` (Containerlab provides these)
- Listens on 0.0.0.0:5001

### 9. Two Deployment Models
**Decision**: Support both offline generation and live provisioning.

| Model | Description |
|-------|-------------|
| **Model A — Offline** | Generate `.clab.yml` + EOS startup configs as files. User deploys later with `containerlab deploy`. |
| **Model B — Live** | Topology already running. User inputs management IPs of each switch. Tool connects via eAPI and pushes config. |

### 10. Arista EOS Provisioning via eAPI
**Decision**: Use eAPI (JSON-RPC over HTTPS) to configure switches.
**Why**: Arista's native REST API — clean, no SSH dependencies. Uses `urllib.request` (no `requests` library needed).

Per-switch configuration:
- IPv6 interface addresses (loopbacks + point-to-point links)
- IPv6 static routes
- SRv6 locator + uSID functions (uN/uA behaviors)
- SRv6 transit mode

---

## Fabric Topology

### 11. Leaf-Spine Topology Structure
**Decision**: Two-plane leaf-spine with configurable dimensions.

Default: 2 planes × (4 leafs + 2 spines) = 12 Arista cEOS switches + 8 MRC host containers.

```
                    Plane 0                                     Plane 1
            ┌─────────────────┐                         ┌─────────────────┐
            │  Spine0  Spine1 │                         │  Spine2  Spine3 │
            └──┬──┬──┬──┬────┘                         └──┬──┬──┬──┬────┘
           ┌───┘  │  │  └───┐                          ┌───┘  │  │  └───┐
        Leaf0  Leaf1 Leaf2 Leaf3                    Leaf4  Leaf5 Leaf6 Leaf7
         │      │     │     │                        │      │     │     │
        Host0  Host1 Host2 Host3                   Host4  Host5 Host6 Host7
```

### 12. IPv6 Addressing — Single Template Derivation
**Decision**: All IPv6 addresses derived from a single user-provided base prefix.
**Why**: User enters one prefix (e.g. `fd00::/32`), tool auto-generates all addressing.

Encoding scheme:
- Loopbacks: `{base}:{plane}:{role}{index}::1/128` (role: 1=spine, 2=leaf)
- P2P links: `{base}:{plane}:{leaf_id}:{spine_id}::{side}/127` (side: 0=leaf, 1=spine)
- Host links: `{base}:{plane}:{leaf_id}:ff::{side}/127` (side: 0=leaf, 1=host)

### 13. SRv6 — Separate Configurable Base Address
**Decision**: SRv6 locator uses a separate user-provided base prefix (e.g. `fcbb::/32`), independent from IPv6 interface addressing.
**Why**: SRv6 locators are a different address space from interface addressing.

Per-node locator: `{srv6_base}:{plane}:{role}{index}::/48`
Per-node uSID: `0x{plane}{role}{index}` (16-bit for F3216)

### 14. EV↔SRv6 Path Mapping
**Decision**: The EV value (32-bit, in IPv6 flow label + UDP src port) serves dual purpose:
1. Entropy for the packet (carried in flow label + UDP source port)
2. Derives the SRv6 path — the EV determines which plane and spine the packet traverses

Each EV maps to a specific uSID stack per destination. The SRv6 address template is **per-destination** (another MRC emulator host). The EV encodes:
- Which **plane** (Plane 0 or Plane 1)
- Which **spine** within that plane

For Host0→Host2 in a 2-plane, 2-spine topology:

| EV | Plane | Spine | SRv6 uSID Path |
|----|-------|-------|-----------------|
| 0x0000 | 0 | Spine0 | `fcbb:0020:0010:0022::` |
| 0x0001 | 0 | Spine1 | `fcbb:0020:0011:0022::` |
| 0x0100 | 1 | Spine2 | `fcbb:1020:1010:1022::` |
| 0x0101 | 1 | Spine3 | `fcbb:1020:1011:1022::` |

The switch uSID values must match the EV profile in the MRC emulator — single source of truth.

---

## NCCL Collective Emulation

### 15. NCCL Communication Collectives
**Decision**: Emulate NCCL collective communication patterns to generate realistic AI/ML traffic between MRC hosts.

| Collective | Pattern | Steps (N hosts) |
|-----------|---------|-----------------|
| AllReduce (Ring) | Ring | 2*(N-1) — reduce-scatter then all-gather |
| AllGather (Ring) | Ring | N-1 |
| ReduceScatter (Ring) | Ring | N-1 |
| AllToAll | Full mesh rotation | N-1 |
| Broadcast | One-to-all fan-out | 1 |
| Point-to-Point | Single pair | 1 |

The user selects a collective, message size, chunk size, and participating hosts. The tool generates a **flow plan** (which hosts communicate, in what order, with what data sizes), and the packet builder + EV engine construct the actual MRC packets with appropriate path selection.

Ring order is configurable for placement-aware scheduling across planes.

---

## Congestion & Fault Injection

### 16. Congestion Simulation — Real ECN on cEOS (Approach A)
**Decision**: Configure ECN/WRED on Arista cEOS switches so traffic is ECN-marked in the fabric when congestion occurs.

- Configure WRED profiles with `min_thresh` / `max_thresh` per spec section 9.3.1
- Assign WRED to data traffic class queues
- Configure queue buffer sizes (smaller buffers = easier to trigger congestion in lab)
- ECN marking flows back to sender via SACK `m` flag, triggering NSCC and EV state transitions
- Add ECN/WRED configuration commands to `eos_provisioner.py`

### 17. Congestion Simulation — Receiver-Side Fallback (Approach B)
**Decision**: The receiving MRC emulator can simulate congestion signals when cEOS ECN is unavailable or insufficient.

The receiver generates signals as if the network had marked/dropped/trimmed packets. The sender sees identical signals in both approaches.

| Fault Type | Behavior | Sender Reaction |
|-----------|----------|-----------------|
| ECN simulation | Set ECN bits on received packets before SACK generation; generate SACKs with `m=SKIP_ONCE` or `m=ALWAYS_SKIP` for selected EVs | NSCC multiplicative decrease + EV state transition |
| Drop by rate | Random drop X% of received packets (don't SACK them) | Timeout → retransmit |
| Drop by PSN | Drop specific PSN(s) | SACK bitmap shows hole → retransmit |
| Drop by EV | Drop packets using specific EV values (simulate bad path) | EV → ASSUMED_BAD, path avoidance |
| Trim simulation | Accept header only, generate NACK with TRIMMED/TRIMMED_LASTHOP reason | NACK → retransmit + CC decrease |
| Delay injection | Hold packets for N microseconds before processing | RTT > target_Qdelay → CC fair-increase mode |

### 18. Traffic Rate Control
**Decision**: Sender needs configurable rate control to actually cause congestion.

- Configurable send rate (packets/sec or Gbps target)
- Burst mode (send N packets back-to-back, then pause)
- Multi-QP simultaneous sending (multiple flows to create congestion)
- Sustained mode for long-running tests

---

## Topology Visualization

### 19. SRv6 Topology View
**Decision**: Interactive topology visualization page showing the full leaf-spine fabric and SRv6 path state.

- Full leaf-spine fabric layout with nodes and links per plane
- Select a source→destination host pair to highlight all available SRv6 paths
- Select a specific flow/EV to see the exact path (leaf→spine→leaf) light up on the diagram
- Visual distinction between planes, active vs inactive paths
- Congestion state overlay (ECN rates, queue depth indicators)

### 20. Link/Path Failure Simulation & Failover Visualization
**Decision**: Simulate link and node failures and visualize the MRC failover behavior.

- Select a link (leaf↔spine) or node (spine) to fail
- Visualize affected EV paths transitioning to ASSUMED_BAD state
- Show traffic shifting to remaining healthy paths in real-time
- Recovery visualization when link/node is restored (EV transitions back to GOOD via probing)
- Correlate with NSCC behavior (cwnd changes during failover)
- Pre-defined failure scenarios:
  - Single link failure (one leaf↔spine link goes down)
  - Spine failure (all links to a spine go down)
  - Plane failure (entire plane goes down)
  - Partial degradation (intermittent drops or high latency on a path)
  - Link flap (repeated up/down cycles)
- Scenario playback with step-by-step or continuous mode

### 21. Per-Path MRC State Display
**Decision**: Show live MRC control plane state for each path/EV.

- Each EV/path shows current state (GOOD, SKIP, DENIED, ASSUMED_BAD)
- RTT measurements per path (from SACK timestamps / EV probe results)
- ECN marking rate per path (from SACK `m` flag history)
- Packet loss/retransmit rate per path
- SACK/NACK counters per path
- Congestion window impact (cwnd changes correlated to path events)
- All updated live as control messages (SACKs, NACKs, probe responses) are received

### 22. Probe-Driven Path Visualization
**Decision**: Integrate probe results into the topology visualization with live state updates.

- Send EV probes and Reliability probes on demand per path or sweep all paths
- Scheduled periodic probing (configurable interval) for continuous monitoring
- Live path state overlay on the SRv6 topology view as probes are sent and responses received
- Color-coded paths: green (GOOD/reachable), yellow (SKIP/congested), red (ASSUMED_BAD/unreachable), grey (DENIED)
- RTT heatmap per path (color intensity based on latency)
- Probe history timeline per path showing state transitions over time
- During failure scenarios, visualization updates in real-time as probes detect failure and paths transition states

---

## Offline / Standalone Mode

### 23. Cross-Platform Offline Mode (Mac / Windows)
**Decision**: The tool runs in standalone mode on Mac or Windows without Containerlab, cEOS switches, or Linux networking for GUI and logic testing.

In offline mode:
- All network configuration is simulated in-memory (no `ip` commands)
- EOS provisioning generates configs but does not push (no eAPI connectivity required)
- Topology generation, EV profiles, packet building, and NCCL collectives all work with simulated state
- Packets are built and displayed (hex dump, decoded headers) but not sent on the wire — no `CAP_NET_RAW` / scapy raw sockets required
- Fault injection and congestion simulation operate entirely in-memory
- Topology visualization and path state display work against simulated data
- Probe results are simulated locally (synthetic RTT, reachability, ECN responses)
- The GUI is fully functional for configuration, visualization, and testing all logic

Auto-detection: the tool detects the platform at startup and enables offline mode automatically on non-Linux systems (or when run without root on Linux). Can also be forced via `OFFLINE_MODE=1` environment variable.

---

## GUI & Visualization Enhancements

### 24. Hover Tooltips on Topology Nodes/Links
**Decision**: Mouse hover on nodes and links in the Fabric View shows detailed info.

- Nodes: name, role, plane, loopback IPv6, SRv6 locator, uSID value, interfaces
- Links: interface names on each end, IPv6 addresses on each end, link state

### 25. Hosts Connected to All Planes
**Decision**: In the Fabric View, MRC emulator hosts are shown connected to leafs across **both planes**, reflecting multi-port NIC behavior (Port = Plane per spec 9.3.2, 11.5). Hosts are positioned below the full fabric, spanning both planes visually.

### 26. Unified Simulation Page
**Decision**: A `/simulation` page combining timed traffic generation, live fabric view, and failure injection in one view.

- Configurable duration, step interval, collective type, message/chunk size
- Live fabric SVG showing path state updates during the run
- EV path state table with color-coded status
- Inject failure scenarios mid-run and watch EV state transitions
- Live metrics: cwnd, inflight, RTT, packet/drop/ECN/trim counters
- Event log showing failures, state changes, CC reactions

### 27. Arista CloudVision-Style GUI
**Decision**: Restyle the UI to follow Arista CloudVision's design language.

- Fixed dark sidebar navigation with grouped sections (Fabric, Endpoint, Transport, Testing)
- Dark theme (#0d1117 background, #161b22 cards) with blue accent (#58a6ff)
- CloudVision tile-style nodes: rounded rectangles with colored sidebar stripe, device name, role, uSID, status dot
- Clean metric cards and status badges
- Responsive: sidebar collapses to horizontal nav on small screens

### 28. Topology Zoom & Pan
**Decision**: SVG topology view supports scroll-to-zoom and drag-to-pan for navigating large fabrics.

### 29. Failure Highlighting on Topology
**Decision**: When a failure is injected during simulation, the affected links and nodes are visually highlighted:
- Failed links pulse red with animation
- Affected nodes get a red failure flash overlay that fades out
- Status dots on CloudVision tiles turn red for nodes involved in the failure
- Failed paths change color in the path overlay (ASSUMED_BAD = red)

---

## Conversation Log

### 2026-07-21 — Initial build
- User requested OCP MRC end-host emulator with GUI for programming EV values
- Clarified: OCP **Multipath Reliable Connection** (not Memory Reference Code)
- User provided full OCP-MRC-1.0 spec (76 pages)
- Agreed on: packet generation tool, all 3 EV modes, Python + Flask, web-based GUI
- User requested addition of congestion control and path probing (beyond initial EV-only scope)
- User confirmed: must run on headless Linux (no desktop) — Flask web server handles this
- Full implementation completed: 31 files, 4,903 lines
- All tests passed: header round-trips, EV engine, CC, probing, packet builder/parser, Flask routes

### 2026-07-22 — Repo setup & v1.0 requirements
- User changed default port from 5000 to 5001
- Project moved to GitHub repo clone at `/Users/anichol/repo/MRC-Client/`
- Initial commit: `7f95984`
- Created this DESIGN.md as persistent reference
- Named the tool **MRC emu** (emu for short)
- Defined v1.0 requirements (22 total):
  - Containerlab deployment (Docker container, two deployment models)
  - Arista EOS provisioning via eAPI (IPv6, static routes, SRv6 uSID)
  - Two-plane leaf-spine topology with 4 leafs + 2 spines per plane
  - IPv6 addressing derived from single template prefix
  - SRv6 locator on separate configurable base address
  - EV↔SRv6 path mapping: EV (flow label + UDP src port) encodes plane + spine, derives SRv6 uSID stack per destination host
  - NCCL collective emulation (AllReduce, AllGather, ReduceScatter, AllToAll, Broadcast, Point-to-Point)
  - Congestion simulation: real ECN on cEOS (Approach A) + receiver-side fallback (Approach B)
  - Traffic rate control (burst, sustained, multi-QP)
  - SRv6 topology visualization with path highlighting
  - Link/path failure simulation with failover visualization and pre-defined scenarios
  - Per-path MRC state display from control plane (RTT, ECN, loss, cwnd)
  - Probe-driven path visualization with live color-coded health overlay
- Requirements v1.0 finalized

### 2026-07-22 — GUI and simulation enhancements
- Added hover tooltips on topology nodes (IPv6, SRv6, uSID) and links (interfaces, addresses)
- Hosts now shown connected to all planes in fabric view
- Built unified `/simulation` page: timed traffic run + live fabric view + failure injection
- Restyled GUI to Arista CloudVision look: dark sidebar nav, blue accents, grouped sections
- Arista-style switch/server icons in SVG topology
- Added offline traffic simulation with closed-loop CC feedback
- Default port changed to 8080
- Requirements expanded to 27 total

---

## Running

```bash
cd /Users/anichol/repo/MRC-Client
pip install -r requirements.txt
python3 app.py
# Access at http://<host>:5001
```

Requires root/sudo for network configuration changes (IPv6 routes, addresses). Runs read-only without root.
