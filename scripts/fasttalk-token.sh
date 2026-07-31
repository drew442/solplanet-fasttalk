#!/usr/bin/env bash

set -euo pipefail

FASTTALK_TOKEN_SCRIPT_DIRECTORY="$(
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1
    pwd -P
)"
# shellcheck source=_fasttalk-daemon-common.sh
source "${FASTTALK_TOKEN_SCRIPT_DIRECTORY}/_fasttalk-daemon-common.sh"

FASTTALK_TOKEN_CONFIG_BASE="${XDG_CONFIG_HOME:-${HOME}/.config}"
FASTTALK_TOKEN_PATH="${SOLPLANET_FASTTALK_TOKEN_FILE:-${FASTTALK_TOKEN_CONFIG_BASE}/solplanet-fasttalk/diagnostics-api.token}"
FASTTALK_TOKEN_DIRECTORY="$(dirname -- "${FASTTALK_TOKEN_PATH}")"

fasttalk_validate_token() {
    local mode token
    [[ -f "${FASTTALK_TOKEN_PATH}" && ! -L "${FASTTALK_TOKEN_PATH}" ]] || return 1
    mode="$(stat -c '%a' "${FASTTALK_TOKEN_PATH}")"
    [[ "${mode}" == 600 || "${mode}" == 400 ]]
    IFS= read -r token < "${FASTTALK_TOKEN_PATH}"
    [[ ${#token} -ge 32 && "${token}" != *[[:space:]]* ]]
}

fasttalk_create_token() {
    local temporary_path
    if [[ -e "${FASTTALK_TOKEN_PATH}" || -L "${FASTTALK_TOKEN_PATH}" ]]; then
        if fasttalk_validate_token; then
            printf 'diagnostics token already exists: %s\n' "${FASTTALK_TOKEN_PATH}"
            return 0
        fi
        fasttalk_fail "refusing to replace an existing invalid file: ${FASTTALK_TOKEN_PATH}"
        return
    fi
    mkdir -p -- "${FASTTALK_TOKEN_DIRECTORY}"
    temporary_path="${FASTTALK_TOKEN_PATH}.new.$$"
    if [[ -e "${temporary_path}" || -L "${temporary_path}" ]]; then
        fasttalk_fail "temporary token path already exists: ${temporary_path}"
        return
    fi
    umask 077
    python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > "${temporary_path}"
    chmod 600 -- "${temporary_path}"
    if ! ln -- "${temporary_path}" "${FASTTALK_TOKEN_PATH}"; then
        rm -f -- "${temporary_path}"
        fasttalk_fail "token path was created concurrently; no file was replaced"
        return
    fi
    rm -f -- "${temporary_path}"
    printf 'created diagnostics token: %s\n' "${FASTTALK_TOKEN_PATH}"
    printf 'use \"%s show\" to display it for browser entry\n' "$0"
}

fasttalk_destroy_token() {
    local pid mode
    fasttalk_prepare_state_directory
    if pid="$(fasttalk_running_pid)" && mode="$(fasttalk_read_mode)" && [[ "${mode}" == lan ]]; then
        fasttalk_fail "stop LAN mode before destroying its in-memory token (PID ${pid})"
        return
    fi
    if pid="$(fasttalk_untracked_pid)"; then
        fasttalk_fail \
            "an untracked daemon is running (PID ${pid}); stop it before destroying a token it may hold"
        return
    fi
    if [[ ! -e "${FASTTALK_TOKEN_PATH}" && ! -L "${FASTTALK_TOKEN_PATH}" ]]; then
        printf 'diagnostics token does not exist: %s\n' "${FASTTALK_TOKEN_PATH}"
        return 0
    fi
    if [[ -L "${FASTTALK_TOKEN_PATH}" || ! -f "${FASTTALK_TOKEN_PATH}" ]]; then
        fasttalk_fail "refusing to delete a symbolic link or non-file: ${FASTTALK_TOKEN_PATH}"
        return
    fi
    rm -- "${FASTTALK_TOKEN_PATH}"
    printf 'destroyed diagnostics token: %s\n' "${FASTTALK_TOKEN_PATH}"
}

case "${1:-}" in
    create)
        fasttalk_create_token
        ;;
    destroy)
        fasttalk_destroy_token
        ;;
    show)
        if ! fasttalk_validate_token; then
            fasttalk_fail "diagnostics token is missing or invalid: ${FASTTALK_TOKEN_PATH}"
            exit 1
        fi
        printf 'diagnostics token (keep private):\n'
        cat -- "${FASTTALK_TOKEN_PATH}"
        ;;
    status)
        if fasttalk_validate_token; then
            printf 'diagnostics token is present and valid: %s\n' "${FASTTALK_TOKEN_PATH}"
        else
            printf 'diagnostics token is absent or invalid: %s\n' "${FASTTALK_TOKEN_PATH}"
            exit 3
        fi
        ;;
    *)
        printf 'usage: %s {create|destroy|show|status}\n' "$0" >&2
        exit 2
        ;;
esac
