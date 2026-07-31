#!/usr/bin/env bash

# Shared implementation for the local and LAN operator scripts.
# This file is sourced; use fasttalk-local.sh or fasttalk-lan.sh directly.

set -euo pipefail

FASTTALK_SCRIPT_DIRECTORY="$(
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1
    pwd -P
)"
FASTTALK_REPOSITORY_DIRECTORY="$(
    cd -- "${FASTTALK_SCRIPT_DIRECTORY}/.." >/dev/null 2>&1
    pwd -P
)"
FASTTALK_RUNTIME_BASE="${XDG_RUNTIME_DIR:-/tmp}"
FASTTALK_STATE_DIRECTORY="${SOLPLANET_FASTTALK_STATE_DIR:-${FASTTALK_RUNTIME_BASE}/solplanet-fasttalk-run}"
FASTTALK_PID_FILE="${FASTTALK_STATE_DIRECTORY}/daemon.pid"
FASTTALK_START_FILE="${FASTTALK_STATE_DIRECTORY}/daemon.start"
FASTTALK_MODE_FILE="${FASTTALK_STATE_DIRECTORY}/daemon.mode"
FASTTALK_LOG_FILE="${SOLPLANET_FASTTALK_LOG_FILE:-${FASTTALK_STATE_DIRECTORY}/daemon.log}"
FASTTALK_API_PORT="${SOLPLANET_FASTTALK_API_PORT:-8765}"

fasttalk_fail() {
    printf 'error: %s\n' "$*" >&2
    return 1
}

fasttalk_prepare_state_directory() {
    if [[ -L "${FASTTALK_STATE_DIRECTORY}" ]]; then
        fasttalk_fail "state directory must not be a symbolic link: ${FASTTALK_STATE_DIRECTORY}"
        return
    fi
    mkdir -p -- "${FASTTALK_STATE_DIRECTORY}"
    chmod 700 -- "${FASTTALK_STATE_DIRECTORY}"
}

fasttalk_config_path() {
    local supplied_path="${1:-}"
    if [[ -n "${supplied_path}" ]]; then
        printf '%s\n' "${supplied_path}"
    elif [[ -n "${SOLPLANET_FASTTALK_CONFIG:-}" ]]; then
        printf '%s\n' "${SOLPLANET_FASTTALK_CONFIG}"
    elif [[ -f /etc/solplanet-fasttalk.toml ]]; then
        printf '%s\n' /etc/solplanet-fasttalk.toml
    else
        printf '%s\n' "${FASTTALK_REPOSITORY_DIRECTORY}/config/solplanet-fasttalk.example.toml"
    fi
}

fasttalk_binary_path() {
    local candidate
    if [[ -n "${SOLPLANET_FASTTALK_BIN:-}" ]]; then
        candidate="${SOLPLANET_FASTTALK_BIN}"
    elif command -v solplanet-fasttalk >/dev/null 2>&1; then
        candidate="$(command -v solplanet-fasttalk)"
    elif [[ -x /root/solplanet-fasttalk-venv/bin/solplanet-fasttalk ]]; then
        candidate=/root/solplanet-fasttalk-venv/bin/solplanet-fasttalk
    elif [[ -x "${FASTTALK_REPOSITORY_DIRECTORY}/venv/bin/solplanet-fasttalk" ]]; then
        candidate="${FASTTALK_REPOSITORY_DIRECTORY}/venv/bin/solplanet-fasttalk"
    elif [[ -x "${FASTTALK_REPOSITORY_DIRECTORY}/.venv/bin/solplanet-fasttalk" ]]; then
        candidate="${FASTTALK_REPOSITORY_DIRECTORY}/.venv/bin/solplanet-fasttalk"
    else
        fasttalk_fail "cannot find solplanet-fasttalk; set SOLPLANET_FASTTALK_BIN"
        return
    fi
    if [[ ! -x "${candidate}" ]]; then
        fasttalk_fail "daemon executable is not executable: ${candidate}"
        return
    fi
    printf '%s\n' "${candidate}"
}

fasttalk_read_pid() {
    local pid
    [[ -f "${FASTTALK_PID_FILE}" && ! -L "${FASTTALK_PID_FILE}" ]] || return 1
    IFS= read -r pid < "${FASTTALK_PID_FILE}"
    [[ "${pid}" =~ ^[1-9][0-9]*$ ]] || return 1
    printf '%s\n' "${pid}"
}

fasttalk_cmdline_is_daemon() {
    local pid="$1"
    local argument program_seen=false run_seen=false
    local -a arguments=()
    [[ -r "/proc/${pid}/cmdline" ]] || return 1
    mapfile -d '' -t arguments < "/proc/${pid}/cmdline"
    for argument in "${arguments[@]}"; do
        if [[ "${argument}" == solplanet-fasttalk ||
              "${argument}" == */solplanet-fasttalk ||
              "${argument}" == solplanet_fasttalk ]]; then
            program_seen=true
        elif [[ "${program_seen}" == true && "${argument}" == run ]]; then
            run_seen=true
        fi
    done
    [[ "${program_seen}" == true && "${run_seen}" == true ]]
}

fasttalk_process_matches() {
    local pid="$1"
    local expected_start actual_start
    [[ -r "/proc/${pid}/cmdline" && -r "/proc/${pid}/stat" ]] || return 1
    [[ -f "${FASTTALK_START_FILE}" && ! -L "${FASTTALK_START_FILE}" ]] || return 1
    IFS= read -r expected_start < "${FASTTALK_START_FILE}"
    actual_start="$(awk '{print $22}' "/proc/${pid}/stat")"
    [[ -n "${expected_start}" && "${actual_start}" == "${expected_start}" ]] || return 1
    fasttalk_cmdline_is_daemon "${pid}"
}

fasttalk_running_pid() {
    local pid
    pid="$(fasttalk_read_pid)" || return 1
    fasttalk_process_matches "${pid}" || return 1
    printf '%s\n' "${pid}"
}

fasttalk_untracked_pid() {
    local cmdline_path pid tracked_pid=""
    tracked_pid="$(fasttalk_read_pid 2>/dev/null || true)"
    for cmdline_path in /proc/[1-9]*/cmdline; do
        [[ -r "${cmdline_path}" ]] || continue
        pid="${cmdline_path#/proc/}"
        pid="${pid%/cmdline}"
        [[ "${pid}" != "${tracked_pid}" ]] || continue
        if fasttalk_cmdline_is_daemon "${pid}"; then
            printf '%s\n' "${pid}"
            return 0
        fi
    done
    return 1
}

fasttalk_read_mode() {
    local mode
    [[ -f "${FASTTALK_MODE_FILE}" && ! -L "${FASTTALK_MODE_FILE}" ]] || return 1
    IFS= read -r mode < "${FASTTALK_MODE_FILE}"
    [[ "${mode}" == local || "${mode}" == lan ]] || return 1
    printf '%s\n' "${mode}"
}

fasttalk_clear_stale_state() {
    local pid
    if pid="$(fasttalk_running_pid)"; then
        return 0
    fi
    rm -f -- "${FASTTALK_PID_FILE}" "${FASTTALK_START_FILE}" "${FASTTALK_MODE_FILE}"
}

fasttalk_start() {
    local mode="$1"
    local host="$2"
    local token_file="$3"
    local supplied_config="${4:-}"
    local config binary pid existing_mode start_time
    local -a api_arguments

    fasttalk_prepare_state_directory
    fasttalk_clear_stale_state
    if pid="$(fasttalk_running_pid)"; then
        existing_mode="$(fasttalk_read_mode || printf 'unknown')"
        if [[ "${existing_mode}" == "${mode}" ]]; then
            printf 'solplanet-fasttalk is already running in %s mode (PID %s)\n' "${mode}" "${pid}"
            return 0
        fi
        fasttalk_fail \
            "daemon is already running in ${existing_mode} mode (PID ${pid}); stop it first"
        return
    fi
    if pid="$(fasttalk_untracked_pid)"; then
        fasttalk_fail \
            "an untracked solplanet-fasttalk daemon is already running (PID ${pid}); stop it through its original service"
        return
    fi

    config="$(fasttalk_config_path "${supplied_config}")"
    [[ -f "${config}" ]] || {
        fasttalk_fail "configuration file does not exist: ${config}"
        return
    }
    binary="$(fasttalk_binary_path)"
    api_arguments=(
        --api-host "${host}"
        --api-port "${FASTTALK_API_PORT}"
        --api-auth-token-file "${token_file}"
    )

    "${binary}" check-config --config "${config}" "${api_arguments[@]}" >/dev/null
    nohup "${binary}" run --config "${config}" "${api_arguments[@]}" \
        </dev/null >>"${FASTTALK_LOG_FILE}" 2>&1 &
    pid=$!
    sleep 1
    if ! kill -0 "${pid}" 2>/dev/null; then
        wait "${pid}" || true
        fasttalk_fail "daemon did not remain running; inspect ${FASTTALK_LOG_FILE}"
        return
    fi
    start_time="$(awk '{print $22}' "/proc/${pid}/stat")"
    printf '%s\n' "${pid}" > "${FASTTALK_PID_FILE}"
    printf '%s\n' "${start_time}" > "${FASTTALK_START_FILE}"
    printf '%s\n' "${mode}" > "${FASTTALK_MODE_FILE}"
    chmod 600 -- "${FASTTALK_PID_FILE}" "${FASTTALK_START_FILE}" "${FASTTALK_MODE_FILE}"

    printf 'started solplanet-fasttalk in %s mode (PID %s)\n' "${mode}" "${pid}"
    if [[ "${mode}" == local ]]; then
        printf 'diagnostics: http://127.0.0.1:%s/diagnostics/\n' "${FASTTALK_API_PORT}"
    else
        printf 'diagnostics: http://DEVICE_LAN_ADDRESS:%s/diagnostics/\n' "${FASTTALK_API_PORT}"
    fi
    printf 'log: %s\n' "${FASTTALK_LOG_FILE}"
}

fasttalk_stop() {
    local requested_mode="$1"
    local pid running_mode attempt

    fasttalk_prepare_state_directory
    fasttalk_clear_stale_state
    if ! pid="$(fasttalk_running_pid)"; then
        printf 'solplanet-fasttalk is not running\n'
        return 0
    fi
    running_mode="$(fasttalk_read_mode || printf 'unknown')"
    if [[ "${running_mode}" != "${requested_mode}" ]]; then
        fasttalk_fail "refusing to stop ${running_mode} mode through the ${requested_mode} script"
        return
    fi

    kill -TERM "${pid}"
    for attempt in {1..120}; do
        if ! fasttalk_process_matches "${pid}"; then
            rm -f -- "${FASTTALK_PID_FILE}" "${FASTTALK_START_FILE}" "${FASTTALK_MODE_FILE}"
            printf 'stopped solplanet-fasttalk %s mode\n' "${requested_mode}"
            return 0
        fi
        sleep 0.25
    done
    fasttalk_fail "daemon did not stop within 30 seconds; PID ${pid} was not force-killed"
}

fasttalk_status() {
    local requested_mode="$1"
    local pid running_mode

    fasttalk_prepare_state_directory
    fasttalk_clear_stale_state
    if ! pid="$(fasttalk_running_pid)"; then
        if pid="$(fasttalk_untracked_pid)"; then
            printf 'an untracked solplanet-fasttalk daemon is running (PID %s)\n' "${pid}"
            return 4
        fi
        printf 'solplanet-fasttalk is stopped\n'
        return 3
    fi
    running_mode="$(fasttalk_read_mode || printf 'unknown')"
    printf 'solplanet-fasttalk is running in %s mode (PID %s)\n' "${running_mode}" "${pid}"
    [[ "${running_mode}" == "${requested_mode}" ]]
}

fasttalk_script_main() {
    local mode="$1"
    local host="$2"
    local token_file="$3"
    shift 3
    local command="${1:-}"
    local config="${2:-}"

    case "${command}" in
        start)
            fasttalk_start "${mode}" "${host}" "${token_file}" "${config}"
            ;;
        stop)
            fasttalk_stop "${mode}"
            ;;
        restart)
            fasttalk_stop "${mode}"
            fasttalk_start "${mode}" "${host}" "${token_file}" "${config}"
            ;;
        status)
            fasttalk_status "${mode}"
            ;;
        *)
            printf 'usage: %s {start|stop|restart|status} [CONFIG]\n' "$0" >&2
            return 2
            ;;
    esac
}
