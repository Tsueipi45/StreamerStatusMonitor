#!/usr/bin/env bash
# Kept as a compatibility entry point for earlier deployments.
set -euo pipefail
exec "$(cd "$(dirname "$0")" && pwd)/deploy.sh" "$@"
