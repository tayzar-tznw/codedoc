"""Leiden topics for codedoc_go_leiden (leiden spec §3–§6).

| Module | Role |
|---|---|
| `graph_builder` | Atom projection with weights and ledger (Phase 4a input, §3.1–3.2) |
| `community` | Leiden communities, hierarchy, identity, carry-over (Phase 4a, §3.3–3.6) |
| `metrics` | ARI, AMI, NMI, modularity, conductance, cohesion in plain Python |
| `naming` | Batched naming prompt and validation (Phase 4b, §5.1) |
| `reports` | Evidence packages, Mermaid scaffolds, grounding check (Phase 5, §5.2) |
| `compare` | `compare-topics` metrics and report (§6) |
| `synthetic` | Synthetic C6 rows for tests and the 4a benchmark |

The package depends only on the standard library, graspologic-native and
networkx; inputs are C6 rows (table → list of column dicts).
"""
