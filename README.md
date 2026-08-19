# qrgf-market-data

Private data bridge for the Quality Recovery Gem Finder.

The GitHub Actions workflow retrieves the official Nasdaq Trader symbol directory in a networked runner, applies the pinned structural L0 classifier, and commits only derived L0 artifacts under `data/latest/`. The raw Nasdaq file is temporary and is not committed.

## V4.1 quality-recovery state

`data/v4/master-core500/**` is the only authoritative MASTER CORE500 path. A published MASTER is exactly 500 unique research scopes and is bound to an immutable quality-source record and selector certificate. `data/v4/campaign/latest.json` is the phase authority: `CANARY`, `PILOT`, `CORE500`, or `COMPLETE`.

The legacy `data/v4/bootstrap/**` Core15 remains immutable historical validation evidence only. It is not used by V4.1 routing.

`data/v4/market/v41/latest.json` is always explicit: either a blocked diagnostic before `COMPLETE`, or a pinned market-session manifest with deterministic challenger pages of 250. Page size is transport only, never a market or quality cutoff.
