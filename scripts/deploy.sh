#!/usr/bin/env bash
#
# deploy.sh — deploy Tolerance and leave real consensus evidence on the explorer.
#
#   ./scripts/deploy.sh studionet
#
# A contract page showing only a deploy transaction proves the file compiles and
# nothing else. This script deploys AND exercises the contract, so the explorer
# shows method calls with the leader's proposal and the validators' votes beside
# them. That page is the strongest single artifact in a submission.
#
# Requires: npm i -g genlayer

set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

NETWORK="${1:-studionet}"
gold() { printf '\033[33m%s\033[0m\n' "$*"; }
dim()  { printf '\033[2m%s\033[0m\n' "$*"; }

gold "Deploying Tolerance to $NETWORK"
genlayer network "$NETWORK"

dim "linting"
genvm-lint contracts/tolerance.py

ADDR=$(genlayer deploy --contract contracts/tolerance.py --args '[]' \
       | grep -oE '0x[0-9a-fA-F]{40}' | head -1)
gold "deployed at $ADDR"

dim "define()  three fields, three tolerances, three plausibility guards"
genlayer write "$ADDR" define --args '[
  "tolerance deploy fixture",
  "https://raw.githubusercontent.com/meitipro/genlayer-tolerance/main/fixture.txt",
  "fee_pct|active_meters|treasury",
  "exact|pct:5|band:1000,100000",
  "range:0,100|range:1,10000|step:40000;range:0,100000000"
]' >/dev/null

dim "read()    one prompt, three fields, each agreed under its own rule"
genlayer write "$ADDR" read --args '[0]'

dim "latest()  the reading, and whether the guard accepted it"
genlayer call "$ADDR" latest --args '[0]'

dim "meter()   the rules, published so a value can be argued with"
genlayer call "$ADDR" meter --args '[0]'

cat <<EOF

  Contract:  $ADDR
  Explorer:  https://explorer-studio.genlayer.com/address/$ADDR

Open that page before submitting. It must show a Deploy transaction AND at
least one method call with a Consensus Result beside it.

EOF
