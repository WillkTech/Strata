#!/bin/bash
# switch.sh — Strata Model Switcher wrapper
# Backward-compatible wrapper delegating to the unified Strata CLI.
set -e

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$HERE/bin/strata" switch "$@"
