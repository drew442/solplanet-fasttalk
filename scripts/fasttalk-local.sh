#!/usr/bin/env bash

set -euo pipefail

FASTTALK_LOCAL_SCRIPT_DIRECTORY="$(
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1
    pwd -P
)"
# shellcheck source=_fasttalk-daemon-common.sh
source "${FASTTALK_LOCAL_SCRIPT_DIRECTORY}/_fasttalk-daemon-common.sh"

# An explicit empty token override ensures local mode remains loopback-only
# even when the base configuration is prepared for authenticated LAN access.
fasttalk_script_main local 127.0.0.1 "" "$@"
