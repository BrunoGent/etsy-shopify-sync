"""Shopify -> Etsy tracking push.

Kept as a thin wrapper so old shortcuts keep working. All logic lives in sync.py
(carrier mapping, hold-until-ship-by-date, multi-parcel, retries).

    python push_all_tracking.py [--dry-run] [--force-dispatch]
"""
import sys

import sync

if __name__ == "__main__":
    sync.main(["--tracking-only"] + sys.argv[1:])
