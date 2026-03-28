# WireGuard Config Generator

This generator is written in Python. It produces WireGuard `.conf` files based on a simple INI file describing nodes.

It is designed for **manageable, practical deployments**, not for modeling every possible network propagation scenario. For special settings, one should correct config files manually. 

The scenario I had in mind while writing this script is as follow. There are several **servers**, each has its own **clients** (some of which can belong to multiple servers). The **servers** can interact with each other as well. 

---
# Usage

python wg_generator.py nodes.ini

This will produce inferred_nodes.ini and a directory wg_configs with ready-to-use .conf files.

---

## 1. Node Roles

* Each node has a `type`:

  * `"server"` → typically a central node or routing hub
  * `"node"` (or unspecified) → client node
* Role determines:

  * IP assignment order (servers first)
  * AllowedIPs generation logic
  * PersistentKeepalive handling
* **Note:** WireGuard itself treats all nodes equally; roles are only used to produce configurations in some useful real-world scenarios.

---

## 2. Networks

* Each node has:

  * `auth_networks`: networks the node **owns/authoritative for**
  * `networks`: networks the node is aware of (including inherited from peers)
* Only servers define `auth_networks` by default.
* Clients inherit networks **only from their direct peers** to avoid unnecessary propagation.

---

## 3. IP Assignment

* Optional manual `Address` values are supported.
* Nodes without manually assigned IPs get addresses **auto-assigned from their networks**.
* Auto-assignment is:

  * Per-network, per-node
  * Deterministic based on node names (lexicographic order)
* Assumptions:

  * IPv4 only
  * Servers get first available IPs, then clients
  * Conflicts are avoided by checking reserved IPs

---

## 4. Peers

* Each node has a `peers` list.
* Peer relationships are **automatically bidirectional**.
* Assumptions:

  * Symmetric peer relationships only
  * Nodes only generate AllowedIPs for **direct peers**
  * Server ↔ server peering allows access to each server’s networks and relevant IPs

---

## 5. AllowedIPs

* Client → server: clients can reach server’s authoritative networks
* Server → client: servers accept traffic **only from assigned IPs of their clients**
* Server ↔ server: each server allows traffic to the other server’s authoritative networks + peer IP in own network
* Only direct peer networks are used to prevent config explosion

---

## 6. PersistentKeepalive

* Applied for:

  * Client → server connections
  * Server ↔ server connections
* Nodes without PersistentKeepalive rely on WireGuard defaults.

---

## 7. Key Generation

* Private/Public keys and PSK are generated automatically if not provided
* Mock keys are used if the `wg` tool is unavailable (for testing)

---

## 8. Limitations & Design Choices

* Not a universal network propagator: nodes do **not learn all networks beyond direct peers**
* Not fully symmetrical routing-aware: only ensures **peer-level reachability**
* Config explosion is intentionally avoided
* No validation of overlapping subnets: input INI should avoid conflicts

---

## 9. How to Add Nodes Safely

1. **New servers:**

   * Add with `type = server`
   * Assign `network` and optional `Address`
   * List peers (servers and clients) explicitly
2. **New clients:**

   * Add with optional `PrivateKey`
   * Assign `peers` to the server(s) they should connect to
   * Avoid adding indirect peers unless intentionally needed
3. **Consistency check:**

   * Ensure each server has `auth_networks` defined (via `network`)
   * Ensure no overlapping IPs unless intentional

---

## 10. Example Test INI

```ini
[GLOBAL]

[server1]
type = server
network = 10.0.0.0/24
Address = 10.0.0.1/24, 10.0.1.1/24
peers = server2, client1, client2
Endpoint = endpoint1.com
ListenPort = 51285
PersistentKeepalive = 25

[server2]
type = server
network = 10.0.1.0/24
Address = 10.0.1.20/24, 10.0.0.20/24
peers = client1, client3
Endpoint = 1.2.3.4
PrivateKey = iAae+56ood9h9jE2Jd23g0+rwVJiyPQp7JEeDKvcRF0=
PersistentKeepalive = 25

[client1]
PrivateKey = 3olFqv4K7V28S6qlACyDPgQew1yu2KbZgP67vaG6dEU=

[client2]

[client3]
```

---

## 11. Visual Diagram of Connections

```
              +-------------------+
             |      server1       |
             |    10.0.0.1/24     |
             |    10.0.1.1/24     |
             +-------------------+
                       ^
                       |
            +----------+----------+
            |          |          |
            v          |          v
     +------+-----+    |    +-----+------+
     |   client1  |    |    |   client2  |
     |  auto IPs  |    |    |  auto IPs  |
     +------------+    |    +------------+
            ^          |
            |          |
            v          |
     +--------------+  |
     |   server2    |  |
     | 10.0.1.20/24 |<-|
     | 10.0.0.20/24 |
     +--------------+
            ^ 
            |
            v
       +----+-----+
       |  client3 |
       | auto IPs |
       +----------+
```

**Notes on diagram:**

* Lines represent **direct peer relationships**.
* Clients only inherit networks from their **directly connected servers**.
* Servers share networks with each other as allowed by server-server logic.
* Auto-assigned IPs for clients will come from the networks of their direct servers.
