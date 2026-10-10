#!/usr/bin/env python3
"""Write-capable corporate-number sync CLI."""

from __future__ import annotations

from corporate_number_resolver_lib import run_sync


if __name__ == "__main__":
    raise SystemExit(run_sync())
