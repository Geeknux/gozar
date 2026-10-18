#!/usr/bin/env bash
# Build the Gozar SPA into src/bi_admin/static/bi_admin/spa (used by CI and releases).
set -euo pipefail
cd "$(dirname "$0")/../frontend"
if [ ! -d node_modules ]; then npm ci --no-audit --no-fund; fi
npm run build
