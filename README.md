# qrgf-market-data

Private data bridge for the Quality Recovery Gem Finder.

The GitHub Actions workflow retrieves the official Nasdaq Trader symbol directory in a networked runner, applies the pinned structural L0 classifier, and commits only derived L0 artifacts under `data/latest/`. The raw Nasdaq file is temporary and is not committed.
