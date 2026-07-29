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
| 28 | Per-plane host addressing (Port = Plane, one IPv6 per plane per host) | §28 |
| 29 | Flow-level packet spraying (flow = XPU-to-XPU, per-packet EV path selection) | §29 |
| 30 | Flow definition modes (unidirectional pair, bidirectional pair, NCCL collective) | §30 |
| 31 | Topology-aware click-to-fail (right-click link/node, auto-resolve affected EVs) | §31 |
| 32 | Spec-based MRC failure detection timeline (SACK gaps, ACK timeout, probing) | §32 |
| 33 | Failure visualization (dashed/red failed links, highlighted nodes, traffic shift) | §33 |
| 34 | MRC detection event log (spec-aligned with section references) | §34 |
| 35 | Collapsible/resizable simulation panels (4 panels, layout persisted) | §35 |
| 36 | Topology zoom & pan | §36 |
| 37 | Topology failure highlighting detail (pulse animation, status dots) | §37 |
| 38 | Per-EV packet counters with clear option | §38 |
| 39 | Animated flow dots on active paths during simulation | §39 |
| 40 | Configurable text size for all panels (topology tiles, EV table, event log, controls) | §40 |
| 41 | Per-plane uSID and SRv6 locator for host XPUs | §41 |
| 42 | Metrics bar tooltips with MRC spec references | §42 |
| 43 | EV profile mode selector (auto from topology / use existing) | §43 |
| 44 | Continuous plan looping for duration-based simulation | §44 |
| 45 | Tabbed GUI layout (Topology Builder + Simulator tabs) | §45 |
| 46 | SRv6 uSID F3216 packet encapsulation (outer IPv6 + optional SRH) | §46 |
| 47 | Real packet I/O via scapy (send/receive on raw interfaces) | §47 |
| 48 | MRC responder loop (receive→parse→SACK/NACK on wire) | §48 |
| 49 | Controller ↔ Host agent API (orchestrate flows across hosts) | §49 |
| 50 | Arista EOS 4.32 SRv6 uSID switch configuration | §50 |
| 51 | Host container auto-configuration (IPv6, routes, MTU) | §51 |
| 52 | Containerlab topology with startup-config mounting | §52 |
| 53 | Live ↔ Offline mode switching in simulator | §53 |
| 54 | Real SACK-driven CC and EV state in live mode | §54 |
| 55 | Configurable QPs per host pair | §55 |
| 56 | Management-only controller host (no data traffic) | §56 |
| 57 | Arista CloudVision light theme default with dark mode toggle | §57 |
| 58 | CSS custom properties for centralized theming (`static/css/style.css`) | §58 |
| 59 | Default admin credentials on all cEOS switch nodes | §59 |
| 60 | cEOS image reference `arista/ceos:latest` | §60 |
| 61 | Controller port 8080 exposed on sandbox host for GUI access | §61 |
| 62 | Multi-platform Docker image build (linux/amd64 + linux/arm64) | §62 |
| 63 | Public GitHub Container Registry for mrc-emu image (no auth required) | §63 |

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

Default: 2 planes × (4 leafs + 2 spines) = 12 Arista cEOS switches + 4 MRC host XPUs (each connected to both planes).

```
                    Plane 0                                     Plane 1
            ┌─────────────────┐                         ┌─────────────────┐
            │  Spine0  Spine1 │                         │  Spine2  Spine3 │
            └──┬──┬──┬──┬────┘                         └──┬──┬──┬──┬────┘
           ┌───┘  │  │  └───┐                          ┌───┘  │  │  └───┐
        Leaf0  Leaf1 Leaf2 Leaf3                    Leaf4  Leaf5 Leaf6 Leaf7
         │      │     │     │                        │      │     │     │
         └──────┼─────┼─────┼────────────────────────┘      │     │     │
                └─────┼─────┼───────────────────────────────┘     │     │
                      └─────┼─────────────────────────────────────┘     │
                            └───────────────────────────────────────────┘
        Host0  Host1 Host2 Host3
       (eth1    eth1   eth1   eth1  = Plane 0 IPv6)
       (eth2    eth2   eth2   eth2  = Plane 1 IPv6)
```

Each host XPU has one port per plane (Port = Plane per spec §9.3.2, §11.5). A single QP
sprays packets across both planes via EV selection. Each port has its own IPv6 address
for SRv6 forwarding.

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

### 28. Per-Plane Host Addressing
**Decision**: Each host XPU has one link per plane with a distinct IPv6 address for SRv6 forwarding (Port = Plane per spec §9.3.2, §11.5).

- The topology generator models hosts as single nodes with one interface per plane
- Each host holds a `plane_interfaces` map: plane → {interface name, IPv6 address, connected leaf}
- Host link IPv6 addresses follow the existing scheme `{base}:{plane}:{leaf_id}:ff::{side}/127`
- In a 2-plane fabric, `host0` has `eth1` (plane 0 IPv6) and `eth2` (plane 1 IPv6)
- Matches the spec's multi-port NIC model (Figure 8) where each port maps to a plane

### 29. Flow-Level Packet Spraying
**Decision**: A flow is defined between XPUs (not links). Packets within a flow are sprayed across all available paths (all planes × all spines) via EV-based selection.

- A flow is `host0 → host2` — the simulator sprays packets per-packet across all paths
- Each EV encodes a plane + spine; the selected EV determines which plane interface the packet egresses from
- The source IPv6 address per packet corresponds to the egress plane's interface address
- The destination IPv6 address per packet corresponds to the destination host's interface on the same plane
- Matches spec §9.3.2 multi-plane EV selection and §6.2.1 work request spraying

### 30. Flow Definition Modes
**Decision**: Users can define traffic as unidirectional XPU pair, bidirectional XPU pair, or NCCL collective from the simulation page.

- **Unidirectional pair**: Select source XPU and destination XPU → single flow direction
- **Bidirectional pair**: Select two XPUs → simultaneous flows in both directions (A→B and B→A)
- **NCCL collective**: Select collective type (AllReduce, AllGather, etc.) + participating hosts → multi-step flow plan
- All modes generate flows between logical XPUs; per-packet path selection is handled by the simulation engine

### 31. Topology-Aware Click-to-Fail
**Decision**: Users click a link or node in the topology view to inject a failure. The tool resolves affected EVs/paths automatically from the topology's path mappings — no EV knowledge required.

- Right-click a link or node in the topology SVG → context menu with: Fail Link/Node, Degrade (ECN), Degrade (partial loss), Restore
- Backend resolves which EVs/paths traverse the clicked element using the topology's path data
- Appropriate fault rules (DROP_EV for hard failure, ECN_MARK for degradation) are created automatically
- Pre-defined failure scenarios remain available: single link, spine failure, plane failure, partial degradation, link flap

### 32. Spec-Based Failure Detection
**Decision**: Failures are not immediately visible to MRC endpoints. The simulation models how MRC discovers failures through its own mechanisms per the spec.

Detection timeline:
1. Packets sent on failed EV are dropped in fabric (never reach responder)
2. SACKs from other packets show bitmap gaps at the failed EV's PSN (§7.4.5)
3. Sender infers loss from SACK bitmap → marks packet for retransmit (§7.4.5)
4. Retransmitted packet uses a different EV; BTH.RTX flag set (§7.4.4)
5. Local ACK Timeout fires if no SACK covers the PSN within timeout (§7.4.1, Table 7-1)
6. After repeated failures on same EV, sender transitions EV to ASSUMED_BAD (§9.3.1, Figure 5)
7. Traffic shifts — `select_next_ev()` skips ASSUMED_BAD EVs (§9.3.1)
8. NSCC adjusts cwnd — multiplicative decrease on ECN+high RTT (§8.2, Table 8-1)
9. Sender sends EV Probes on ASSUMED_BAD EVs for recovery (§6.5.2, §7.4.7)
10. Probe response with m=NONE → EV transitions ASSUMED_BAD → GOOD (§9.3.1, Figure 5)

For degradation (ECN/partial loss):
- Packets arrive but ECN-marked → SACK m=SKIP_ONCE → EV → SKIP (§9.3.1)
- SKIP EVs temporarily avoided, transition back to GOOD after implementation-defined time
- NSCC reduces cwnd proportionally (§8.2)

### 33. Failure Visualization
**Decision**: Failed links and nodes are visually highlighted in the topology during simulation.

- Failed links rendered with dashed stroke and red color (pulsing animation)
- Failed nodes get a red overlay; status dots on CloudVision tiles turn red
- Active paths shown in green/blue; paths transitioning to ASSUMED_BAD turn red then fade
- Traffic visibly shifts to healthy paths as MRC detects and reacts to the failure
- Path overlay updates in real-time as EV states change during simulation

### 34. MRC Detection Event Log
**Decision**: A spec-aligned event log shows how MRC detects and reacts to failures with spec section references.

Event types logged:
- `SACK_RECEIVED` — m-flag value (NONE/SKIP_ONCE/ALWAYS_SKIP), reflected EV, ECN state (§7.5.2)
- `NACK_RECEIVED` — reason code (TRIMMED, NO_BITMAP, etc. per Table 7-3), PSN, action taken (§7.4.4)
- `ACK_TIMEOUT` — EV, retry count, timer value (§7.4.1, Table 7-1)
- `EV_STATE_CHANGE` — EV value, old state → new state, trigger reason (§9.3.1, Figure 5)
- `PROBE_SENT` — EV, probe_id, probe type (reliability §6.5.1 / EV §6.5.2)
- `PROBE_RESPONSE` — EV, m-flag, result (§7.4.7)
- `TRAFFIC_SHIFT` — description of traffic moving away from/to paths
- `CWND_CHANGE` — old cwnd → new cwnd, trigger (§8.2, Table 8-1)

### 35. Collapsible/Resizable Simulation Panels
**Decision**: The simulation page's 4 panels are each independently collapsible and resizable.

Default layout:
- **Flow Definition** — top control bar (XPU pair selector, collective dropdown, start/stop/step)
- **Topology View** — expanded full width (SVG with click-to-fail, path highlighting)
- **Path/EV State** — across the bottom (table with color-coded states, RTT, ECN rate)
- **Event Log** — minimised by default (collapsed to header bar, click to expand)

Each panel has a header bar with collapse/expand toggle. Panels can be maximised (fills available space) or minimised (header only). Layout state persisted in localStorage.

### 36. Topology Zoom & Pan
**Decision**: SVG topology view supports scroll-to-zoom and drag-to-pan for navigating large fabrics.

### 37. Topology Failure Highlighting Detail
**Decision**: When a failure is injected during simulation, the affected links and nodes are visually highlighted:
- Failed links pulse red with dashed animation
- Affected nodes get a red failure flash overlay that fades out
- Status dots on CloudVision tiles turn red for nodes involved in the failure
- Failed paths change color in the path overlay (ASSUMED_BAD = red)

### 38. Per-EV Packet Counters
**Decision**: The simulator tracks per-EV packet counts showing how many packets used each path.

- Displayed in the "Pkts" column of the EV Path State table
- Counters are cumulative across plan cycles (preserved by `restart_plan`)
- A "Clear Counters" button resets per-EV counts and metrics bar totals without affecting EV state or the event log

### 39. Animated Flow Dots
**Decision**: Active paths show animated white dots flowing along the path during simulation.

- 3 dots per active (GOOD) path, staggered timing, moving from source host through leaf→spine→leaf to destination host
- Dots only appear while the simulation is running; they stop immediately when the simulation stops
- ASSUMED_BAD, SKIP, and DENIED paths show no dots — only the colored overlay line
- Dots are bright white (full opacity, r=5) for visibility against the semi-transparent green path overlay

### 40. Configurable Text Size
**Decision**: All panels have an "Aa" button that cycles through small/medium/large text.

- Topology tiles: cycles tile dimensions (120×48 / 160×60 / 200×74) and text (11px / 13px / 16px)
- EV Path State table: cycles row text (0.78rem / 0.92rem / 1.08rem)
- MRC Event Log: cycles log text (0.72rem / 0.9rem / 1.08rem)
- Flow controls bar: cycles label and input text (0.72rem / 0.88rem / 1.05rem)
- Default is medium for all panels

### 41. Per-Plane uSID and SRv6 Locator for Hosts
**Decision**: Each host XPU has per-plane uSID values and SRv6 locators, stored in `plane_interfaces`.

- uSID encoding: `[15:8]=plane, [7:4]=role (3=host), [3:0]=index` — e.g. host0 plane 0 = `0x0030`, plane 1 = `0x0130`
- SRv6 locator: `{srv6_base}:{plane}:{role_id}::/48` — e.g. `fcbb:0:30::/48`
- Topology tiles display per-plane uSIDs: `P0:0x0030 P1:0x0130`
- Tooltips show full per-plane interface details (interface name, IPv6, uSID, SRv6 locator, connected leaf)

### 42. Metrics Bar Tooltips
**Decision**: Each metric in the metrics bar has a hover tooltip explaining its meaning with MRC spec references.

- CWND: Congestion window — max bytes in-flight before waiting for SACKs (§8.3.1)
- Inflight: Total bytes sent but not yet acknowledged (§8.3.1)
- Packets: Total packets sent across all EVs/paths
- Dropped: Packets lost in fabric, triggers ACK timeout (§7.4.1)
- ECN: ECN-marked packets, SACK m=SKIP_ONCE (§9.3.1)
- Trimmed: Payload removed by switch, NACK TRIMMED (§7.5.3)
- RTT: Round-trip time estimate for NSCC congestion detection (§8.2)
- Step: Current step / total steps in flow plan cycle
- EV State: Summary of EV states per spec Figure 5

### 43. EV Profile Mode Selector
**Decision**: The flow controls bar includes an EV Profile selector with two modes.

- **Auto (from topology)**: Auto-generates an EV profile from the topology's path mappings on the first simulation step. Covers all EV values across all planes and spines. Default mode.
- **Use existing profile**: Uses a manually-defined profile from the EV Profiles page. If none exists, falls back to sequential round-robin without EV state tracking.

### 44. Continuous Plan Looping
**Decision**: When a flow plan's steps are exhausted during a timed simulation, the plan restarts from step 0.

- `restart_plan()` resets only the step counter — EV states, packet counts, event log, and CC state are preserved
- Traffic continues flowing until the configured duration expires
- The event log shows "Plan cycle complete — restarting" at each cycle boundary
- On completion, the simulation stops gracefully without clearing state

---

## Containerlab Live Mode

### 45. Tabbed GUI Layout
**Decision**: The GUI has two main tabs alongside the existing sidebar navigation.

- **Tab 1 — Topology Builder**: fabric dimension configuration, .clab.yml generation with startup-config mounting, per-switch EOS config generation/download, per-host startup script generation, eAPI push to live switches, topology preview
- **Tab 2 — Simulator**: existing simulation view (flow definition, topology view, EV state, event log, metrics) working in both offline and live modes

### 46. SRv6 uSID F3216 Packet Encapsulation
**Decision**: The PacketBuilder supports SRv6 encapsulation with outer IPv6 + optional SRH wrapping.

- Outer IPv6 destination address carries the uSID stack (LID + up to 6 uSIDs in F3216)
- Optional SRH extension header for paths requiring additional segments or debug/telemetry
- Inner packet: standard MRC stack (Ethernet/IPv6/UDP/BTH/MRC headers)
- `build_srv6_packet()` method wraps inner packet with SRv6 outer headers
- Source address = host's per-plane interface IPv6 address

### 47. Real Packet I/O via Scapy
**Decision**: In live Containerlab mode, the MRC emulator sends and receives real packets on the wire.

- Send: scapy `sendp()` on per-plane interfaces (eth1 for plane 0, eth2 for plane 1)
- Receive: scapy `AsyncSniffer` filtering on UDP dst port 4971 (RoCE)
- Requires `CAP_NET_RAW` + `CAP_NET_ADMIN` (provided by Containerlab)
- Receive path wired to packet parser → MRC responder loop

### 48. MRC Responder Loop
**Decision**: Each XPU host runs a responder that receives MRC WRITE packets and generates real SACK/NACK responses.

- Listen on data interfaces for incoming MRC packets
- Parse via `packet_parser.parse_packet()`
- Track PSN bitmap per source QP (responder state per §7.5)
- Generate real SACK packets via `PacketBuilder.build_sack()`:
  - cack_psn and sack_bitmap from PSN tracking
  - m-flag from ECN state (IP header ECN CE bits)
  - CC_STATE with rcvd_bytes, ooo_count, reflected tx_timestamp
  - Reflected entropy from received packet (§7.5.2.1)
- Generate NACK for trimmed/dropped/error conditions
- Send SACK/NACK on control traffic class (DSCP_CONTROL)

### 49. Controller ↔ Host Agent API
**Decision**: A management-only controller orchestrates flows across XPU hosts via REST APIs.

- Controller calls host APIs:
  - `POST /api/host/start_flow` — start sending MRC packets to a destination
  - `POST /api/host/stop_flow` — stop sending
  - `GET /api/host/state` — return EV state, packet counts, CC state, event log
  - `GET /api/host/ev_states` — per-EV state for the path state panel
  - `POST /api/host/configure` — set up EV profile, CC config, interface addresses
- Controller aggregates state from all hosts for the unified GUI view
- Host discovery via management IP addresses configured in Topology Builder

### 50. Arista EOS 4.32 SRv6 uSID Switch Configuration
**Decision**: The EOS provisioner generates complete SRv6 uSID configuration for EOS 4.32+.

Per-switch configuration:
```
router segment-routing
   srv6
      encapsulation source-address Loopback0
      locator LOC-<HOSTNAME>
         prefix <srv6_locator>
         micro-segment behavior uN
segment-routing
   srv6
      transit
```

- F3216 uSID encoding: `[31:16]=LID, [15:8]=plane, [7:4]=role (1=spine/2=leaf/3=host), [3:0]=index`
- Static routes to all remote SRv6 locator prefixes via appropriate next-hops
- ECN/WRED profiles for congestion marking on data traffic classes

### 51. Host Container Auto-Configuration
**Decision**: Each XPU host container is configured at startup via a generated shell script.

- IPv6 addresses on per-plane interfaces (from `plane_interfaces`)
- Default routes via connected leaf switches per plane
- MTU 9216 (jumbo frames for MRC + SRv6 overhead)
- No kernel SRv6 config needed — MRC emu crafts SRv6-encapsulated raw packets directly

### 52. Containerlab Topology with Startup-Config Mounting
**Decision**: The generated `.clab.yml` includes startup-config references for all nodes.

- cEOS nodes: `startup-config: configs/<node_name>.cfg`
- Host containers: bind-mount startup script + exec at boot
- Controller node: management-only, no data interfaces
- All configs generated into a `configs/` directory
- Single `containerlab deploy -t topology.clab.yml` deploys the entire fabric

### 53. Live ↔ Offline Mode Switching
**Decision**: The simulator tab works in both live and offline mode with the same GUI.

- Detects live mode by checking host reachability via management IPs
- Live mode: flows trigger real packet generation on remote hosts via controller API
- Offline mode: flows run the existing in-memory simulation
- Mode indicator displayed in the metrics bar
- Same topology view, EV state panel, and event log for both modes

### 54. Real SACK-Driven CC and EV State
**Decision**: In live mode, real wire traffic drives the congestion controller and EV state machine.

- Received SACKs from the wire feed into the NSCC congestion controller
- EV state transitions based on real SACK m-flags and actual packet timeouts
- Event log shows real wire events with actual timestamps
- Packet counts and RTT measured from actual packet round-trips

### 55. Configurable QPs per Host Pair
**Decision**: The number of Queue Pairs per destination host is configurable.

- Default: 1 QP per destination
- User can increase QPs in the flow definition controls
- Multiple QPs enable higher parallelism and more in-flight packets
- Each QP has independent PSN tracking, CC state, and EV selection

### 56. Management-Only Controller Host
**Decision**: The controller is a dedicated management node that does not participate in data traffic.

- Connected only to the management network (eth0)
- Runs the full GUI (Topology Builder + Simulator tabs)
- Orchestrates flows by calling REST APIs on XPU hosts
- Aggregates EV state, packet counts, events from all hosts
- Does not send or receive MRC data packets

### 57. Arista CloudVision Light Theme Default with Dark Mode Toggle
**Decision**: The GUI defaults to a light/white CloudVision theme with an option to switch to dark mode via a sidebar toggle button.

- **Light theme (default)**: White card backgrounds (`#ffffff`), light gray page background (`#f5f6f8`), dark text (`#24292f`), Arista blue accents (`#0078D4`)
- **Dark theme (toggle)**: Dark navy backgrounds (`#101820` body, `#1c2e40` cards), light text (`#d0d8e0`), same Arista blue accents
- **Sidebar is always dark** in both modes — dark navy (`#1B2A3C`) with "ARISTA" wordmark above "MRC emu" brand text
- Theme toggle button at bottom of sidebar (sun/moon icon)
- Preference persisted in `localStorage` and applied on page load via early `<script>` block
- Topology SVG rendering uses dual color constant sets (`C_LIGHT`/`C_DARK`) selected by current theme
- `onThemeChange()` callback re-renders topology tiles when theme is toggled
- All colors centralized as CSS custom properties (`--cv-*`) in `static/css/style.css`

**Why**: CloudVision's standard portal uses a white/light theme. Dark mode is available as an option for users who prefer it.

### 58. CSS Custom Properties for Centralized Theming
**Decision**: All UI colors defined as CSS custom properties on `:root` (light) with dark overrides under `[data-bs-theme="dark"]`.

- `--cv-bg-body`, `--cv-bg-card`, `--cv-bg-sidebar`, `--cv-border`, `--cv-text`, `--cv-accent`, etc.
- Bootstrap 5.3 component-level variable overrides for buttons, badges, forms, tables, nav tabs
- Status colors (green `#00875A`, amber `#D4A017`, red `#CF3040`, gray `#5A6B7A`) consistent across both themes
- All template `<style>` blocks reference `var(--cv-*)` — single file to edit for palette changes

**Why**: The previous approach had ~144 hardcoded hex values scattered across 4 template files, making theme changes error-prone.

### 59. Default Admin Credentials on cEOS Nodes
**Decision**: All cEOS switch startup configs include `username admin privilege 15 role network-admin secret admin`.

- Added to all 12 existing `.cfg` files in `configs/`
- EOS provisioner (`core/eos_provisioner.py`) generates the admin user in all future configs
- Allows immediate login to cEOS nodes after deployment without manual configuration

**Why**: Default cEOS has no password, but eAPI and SSH access require configured credentials.

### 60. cEOS Image Reference — `arista/ceos:latest`
**Decision**: All cEOS image references use `arista/ceos:latest` (the standard Arista container registry path).

- Updated in: `topology.clab.yml`, `core/topology_generator.py`, `generate_deployment.py`, `routes/topology.py`, GUI template defaults
- Previous value was `ceos:4.36.0.1F` (version-specific, non-standard path)

**Why**: `arista/ceos:latest` is the standard image name for Containerlab deployments, allowing users to use whichever cEOS version they have tagged as `latest`.

### 61. Controller Port Exposed on Sandbox Host
**Decision**: The controller node in `topology.clab.yml` exposes port 8080 on the sandbox host via `ports: - 8080:8080`.

- Allows GUI access from the sandbox's web browser at `http://localhost:8080`
- Required because Containerlab runs inside a sandbox with no external connectivity — management IP alone is insufficient

**Why**: The deployment environment is a web-based terminal sandbox with no external network access. Port publishing on the host is the only way to reach the controller GUI.

### 62. Multi-Platform Docker Image Build
**Decision**: The mrc-emu Docker image is built for both `linux/amd64` and `linux/arm64` using `docker buildx`.

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t ghcr.io/anichol67/mrc-emu:latest --push .
```

**Why**: Development is on Apple Silicon (arm64) but Containerlab servers run on amd64. A multi-platform manifest ensures the image works on both.

### 63. Public GitHub Container Registry
**Decision**: The `ghcr.io/anichol67/mrc-emu` package is set to public visibility.

- No `docker login` required to pull the image
- Containerlab can pull directly without authentication tokens

**Why**: The sandbox environment has no way to store or pass registry credentials. Public access eliminates the auth requirement.

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

### 2026-07-27 — Multi-plane host model, flow-level spraying, and simulation enhancements
- Verified per-plane host addressing against OCP MRC spec §9.3.2, §11.5 (Port = Plane)
- Added OCP MRC spec and GitHub references to DESIGN.md
- Restructured host model: single XPU node with one interface per plane (was separate per-plane host nodes)
- Flow definition is between XPUs, not links — per-packet path selection via EV
- Added flow definition modes: unidirectional pair, bidirectional pair, NCCL collective
- Added topology-aware click-to-fail: right-click link/node, auto-resolve affected EVs
- Added spec-based MRC failure detection timeline (SACK gaps → loss inference → ACK timeout → EV state transition → probing → recovery)
- Added failure visualization: dashed/red failed links, highlighted nodes, traffic shifting
- Added spec-aligned MRC detection event log with spec section references
- Added collapsible/resizable simulation panels (4 panels: flow def, topology, EV state, event log)
- Default layout: flow controls top, topology full-width, EV state bottom, event log minimised
- Requirements expanded to 37 total

### 2026-07-27 — Simulation refinements and GUI enhancements
- Fixed EV state transitions: SKIP → ASSUMED_BAD now allowed for escalated failures
- Auto-generate EV profile from topology paths when none manually defined
- Added EV Profile mode selector in flow controls (auto from topology / use existing)
- Per-EV packet counters in EV Path State table with Clear Counters button
- Per-plane uSID and SRv6 locator for host XPUs (role=3, displayed as P0:0x0030 P1:0x0130)
- Animated flow dots on active (GOOD) paths during simulation; no dots on failed/skipped paths
- Continuous plan looping: plan restarts from step 0 preserving EV state and counters
- Configurable text size (Aa button) for all panels: topology tiles, EV table, event log, controls
- Metrics bar hover tooltips with MRC spec references for CWND, Inflight, Dropped, ECN, RTT, etc.
- Fixed EV state display: simulator's ev_states are authoritative during simulation (no pathstate API overwrite)
- Fixed packet counters not resetting on plan cycle (restart_plan preserves counts)
- Requirements expanded to 44 total

### 2026-07-27 — Additional simulation features
- Auto EV probing on ASSUMED_BAD paths (periodic, configurable auto/manual)
- SKIP timeout recovery per spec §9.3.1 (SKIP → GOOD after implementation-defined timeout)
- Full ECN detection chain in event log: ECN_RECEIVED (data packet with CE), SACK_GENERATED (SETH + CC_STATE fields), SACK_RECEIVED (sender processing)
- Per-path BW% column in EV Path State table
- Bidirectional flow dots: forward (white) and reverse (grey-white) flowing in opposite directions
- Help cursor on metrics bar tooltips
- Multiple failures and restores within a single flow session
- Fault restore triggers EV probe_resolved() recovery in simulator

### 2026-07-28 — Containerlab live mode build
- Defined architecture: management-only controller + XPU hosts with real packet I/O
- **EOS provisioner** updated for EOS 4.32 SRv6 uSID F3216:
  - `micro-segment behavior uN` under locator
  - `encapsulation source-address Loopback0`
  - Auto-generated static routes to all remote SRv6 locator prefixes
  - `srv6_locator_routes` field added to `EOSNodeConfig`
- **Topology generator** enhanced:
  - `.clab.yml` includes `startup-config:` for cEOS and `binds:`/`exec:` for hosts
  - Controller node added (management-only, no data interfaces)
  - `generate_host_startup_script()` — IPv6 addresses, routes, MTU 9216 per plane
  - `generate_eos_node_configs()` — full EOSNodeConfig dicts with SRv6 locator routes
  - `get_srv6_locator_routes()` — computes per-switch routes to all other locator prefixes
  - Configurable `qps_per_host_pair` in TopologyConfig
- **SRv6 packet encapsulation** (core/packet_builder.py):
  - `SRv6Header` dataclass — routing type 4, F3216 uSID segment list, `to_bytes()`/`from_bytes()`
  - `MRCPacket` extended with `outer_ipv6` and `srh` fields
  - `to_bytes()` handles outer IPv6 + optional SRH + inner IPv6/UDP/BTH/MRC
  - `build_srv6_write()` method — wraps inner WRITE packet with SRv6 encapsulation
- **Packet I/O** (core/packet_io.py — new):
  - `PacketIO` class with scapy `sendp()` for sending and `AsyncSniffer` for receiving
  - Interface binding, BPF filter on UDP dst port 4971
  - Graceful fallback when scapy unavailable (offline mode)
- **MRC responder** (core/mrc_responder.py — new):
  - `ResponderQPState` — per-QP PSN bitmap tracking, cack_psn, rcvd_bytes, ooo_count
  - `MRCResponder.process_packet()` — parse incoming WRITE, generate real SACK bytes
  - SACK includes reflected entropy, m-flag from ECN CE detection, CC_STATE, bitmap
  - NACK generation for error conditions
- **Host agent API** (routes/host_agent.py — new):
  - `POST /api/host/configure` — bind interfaces, start responder, set QPs
  - `POST /api/host/start_flow` / `stop_flow` — traffic generation control
  - `GET /api/host/state` — responder stats, QP states, packet I/O stats
  - `GET /api/host/event_log` — responder event log
- **Controller API** (routes/controller.py — new):
  - `POST /api/controller/hosts` — register remote hosts with management IPs
  - `POST /api/controller/discover` — test connectivity, set live/offline mode
  - `POST /api/controller/configure_hosts` — push config to all hosts
  - `POST /api/controller/start_flow` — orchestrate flow between host pair (with bidirectional)
  - `GET /api/controller/aggregate_state` — collect state from all hosts
  - `GET /api/controller/aggregate_events` — merge event logs from all hosts
- **Tabbed GUI**:
  - Sidebar nav: Topology Builder + Simulator (renamed from Simulation)
  - `/topology_builder` page: fabric config, generate .clab.yml + configs, download, preview, eAPI push, connectivity test
- **Default deployment files** generated:
  - `topology.clab.yml` — 2 planes, 12 switches, 4 hosts, 1 controller
  - `configs/` — 12 EOS startup configs + 4 host startup scripts
  - `generate_deployment.py` — CLI for regenerating with custom dimensions
- Requirements expanded to 56 total

### 2026-07-29 — CloudVision theme, deployment fixes, and sandbox access
- Restyled GUI to Arista CloudVision light theme (white backgrounds, Arista blue accents) as default
- Added dark mode toggle in sidebar with localStorage persistence
- Centralized all UI colors as CSS custom properties in `static/css/style.css`
- Added "ARISTA" wordmark to sidebar brand area above "MRC emu"
- Sidebar stays dark navy in both light and dark modes (CloudVision pattern)
- Dual JS color constants (`C_LIGHT`/`C_DARK`) for topology SVG rendering with `onThemeChange()` re-render
- Bootstrap component-level overrides for buttons, badges, forms, tables, nav tabs
- Changed cEOS image from `ceos:4.36.0.1F` to `arista/ceos:latest` across all files
- Added `username admin privilege 15 role network-admin secret admin` to all 12 switch startup configs
- Updated EOS provisioner to include admin user in all future generated configs
- Exposed controller port 8080 on sandbox host (`ports: - 8080:8080` in topology.clab.yml)
- Built multi-platform Docker image (linux/amd64 + linux/arm64) via `docker buildx`
- Set ghcr.io/anichol67/mrc-emu package to public (no auth required for pull)
- Requirements expanded to 63 total

---

## Running

### Offline Mode (Mac/Windows/Linux)

```bash
cd /Users/anichol/repo/MRC-Client
pip install -r requirements.txt
python3 app.py
# Access at http://localhost:8080
# Use Topology Builder tab to configure, Simulator tab to simulate
```

### Containerlab Live Mode

**Docker images:**

- `ghcr.io/anichol67/mrc-emu:latest` — MRC emulator (hosts + controller), pulled from GitHub Container Registry (public, no auth required)
- `arista/ceos:latest` — Arista cEOS (switches), pulled from Arista registry or imported locally

**Deploy:**

```bash
git clone https://github.com/anichol67/MRC-Client.git
cd MRC-Client
containerlab deploy -t topology.clab.yml
# Access controller GUI at http://localhost:8080 (port exposed on host)
```

**Switch credentials:** `admin` / `admin` (configured in all startup configs)

**Regenerate topology (optional — only needed to change fabric dimensions):**

```bash
python3 generate_deployment.py --planes 2 --leafs 4 --spines 2 --ceos-image arista/ceos:latest
```

**Rebuild and push multi-platform image (from Mac or CI):**

```bash
docker buildx create --use
docker buildx build --platform linux/amd64,linux/arm64 -t ghcr.io/anichol67/mrc-emu:latest --push .
```

Requires root/sudo for network configuration changes (IPv6 routes, addresses). Runs read-only without root.

---

## References

| Document | Details |
|----------|---------|
| OCP-MRC-1.0 | [Multipath Reliable Connection Specification, Revision 1.0 (03/21/26)](https://github.com/opencomputeproject/OCP-Multipath-Reliable-Connection) — Joint contribution from AMD, Broadcom, Intel, Microsoft, NVIDIA, OpenAI |
| libmrc | [MRC Software API (mrc.h, mrc_ctl.h)](https://github.com/opencomputeproject/OCP-Multipath-Reliable-Connection) |
| IBTASPEC | InfiniBand Architecture Specification Volume 1 Release 1.8 |
| UESPEC | UltraEthernet 1.01 Specification |
| RFC 8986 | Segment Routing over IPv6 (SRv6) Network Programming |
| RFC 9800 | Compressed SRv6 Segment List Encoding |
| SRv6 uSID | Network Programming extension: SRv6 uSID instruction |
