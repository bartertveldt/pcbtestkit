# Roadmap

What is planned, in order. Released versions are in [CHANGELOG.md](CHANGELOG.md).

## 1.3.0 (planned)

The full specification, with every feature, setting, check and acceptance test, is
[docs/roadmap/v1.3.0.md](docs/roadmap/v1.3.0.md).

| Id | Epic | In short |
|---|---|---|
| E1 | Best test environment for a new board | every test environment you built is remembered; a new board is matched against all of them and their stations, mainly for the biggest useful batch, and can pick the best one automatically |
| E2 | Jigs of one size that slot together | the placement and holding jigs share one outline and register with a lip and pins, without changing any pocket or clearance |
| E3 | Clay fit visualiser | the gap between each jig and the board shown as squeezed modelling clay, coloured by how snug it is, in the video and as a top view and table in the guide |
| E4 | Hinged jigs with a briefcase latch | the jig pair folds on a hinge and latches, so turning the stack over cannot shift anything; the motions are checked against every part |
| E5 | Multi-material jigs and per-part needs | 16 kinds of part needs (flexible liners for parts that vary in size, heat, cushioning, ESD, retention, ...) with presets and full parameters; suggested automatically, chosen in the KiCad dialog, CLI or MCP; soft material woven into the rigid body; 3MF output; fallbacks for single-material printers |
| E6 | Cost of every build, and downgrades | every jig, fixture, station and catalog states its cost as a range; a catalog that is too expensive can be downgraded to a budget, with every lost board named |

Additions (calibration coupons, bed packing, engraving, analog multiplexers for big boards, wear counters, an
environment doctor, deterministic renders) and maintenance items are in the same document.

## Ideas and requests

Open an issue with the label `enhancement`. Security problems go through [SECURITY.md](SECURITY.md), not issues.
