#!/usr/bin/env bash

set -euo pipefail

FASTTALK_LAN_SCRIPT_DIRECTORY="$(
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1
    pwd -P
)"
# shellcheck source=_fasttalk-daemon-common.sh
source "${FASTTALK_LAN_SCRIPT_DIRECTORY}/_fasttalk-daemon-common.sh"

FASTTALK_LAN_CONFIG_BASE="${XDG_CONFIG_HOME:-${HOME}/.config}"
FASTTALK_LAN_TOKEN_FILE="${SOLPLANET_FASTTALK_TOKEN_FILE:-${FASTTALK_LAN_CONFIG_BASE}/solplanet-fasttalk/diagnostics-api.token}"

if [[ "${1:-}" == start || "${1:-}" == restart ]]; then
    if [[ ! -f "${FASTTALK_LAN_TOKEN_FILE}" || -L "${FASTTALK_LAN_TOKEN_FILE}" ]]; then
        printf 'error: diagnostics token is missing; run scripts/fasttalk-token.sh create\n' >&2
        exit 1
    fi
fi

fasttalk_script_main lan 0.0.0.0 "${FASTTALK_LAN_TOKEN_FILE}" "$@"
