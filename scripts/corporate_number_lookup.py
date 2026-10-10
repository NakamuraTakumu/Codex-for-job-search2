#!/usr/bin/env python3
"""Read-only corporate-number lookup CLI."""

from __future__ import annotations

from corporate_number_resolver_lib import run_lookup


if __name__ == "__main__":
    raise SystemExit(run_lookup())
