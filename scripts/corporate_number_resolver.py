#!/usr/bin/env python3
"""Compatibility entrypoint for corporate-number lookup and sync."""

from __future__ import annotations

from corporate_number_resolver_lib import run_full


if __name__ == "__main__":
    raise SystemExit(run_full())
