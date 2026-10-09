# scripts/lib/guided.sh -- shared helpers for the GUIDED (no-flags) setup flow.
#
# Sourced by install-gateway.sh, install-pi.sh and install-esp32.sh (relative to the script's own
# directory) so the three installers look and behave identically: a short header, DETECT what the
# host can tell us, PROMPT for the rest with sane defaults shown in [brackets] (bare Enter accepts
# them), VALIDATE with the same rules the flag path uses, print a SUMMARY, then require an
# explicit yes/no before anything is written. The assembled answers are handed to the very same
# flag path by re-executing the installer with them, so a guided run and the equivalent flag
# invocation run exactly the same steps. Flags with no prompt of their own (--bundle-dir,
# --model, --hermes-bin, --no-deps, ...) are forwarded verbatim to that re-execution, so
# nothing the user typed on the command line is ever dropped.
#
# This file only defines functions; sourcing it has no side effects. It is allowed to be missing
# (a script copied on its own keeps working on the flag path); the guided entry points refuse
# with a clear message when it is.

# ---------------------------------------------------------------- detection helpers
# Every detector prints what it found (one item per line) or nothing, and ALWAYS exits 0: an
# absent tool or empty machine must degrade to "no default", never abort the installer under
# `set -e` / `pipefail`.

# guided_detect_lan_ip -- the address this host is reachable on (never a loopback answer).
guided_detect_lan_ip() {
    local ip=""
    if command -v ip >/dev/null 2>&1; then
        ip="$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.*src \([0-9.][0-9.]*\).*/\1/p' | head -n1 || true)"
    fi
    if [ -z "$ip" ] && command -v hostname >/dev/null 2>&1; then
        ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    fi
    case "$ip" in
        ''|127.*) return 0 ;;
    esac
    printf '%s\n' "$ip"
    return 0
}

# guided_detect_serial_ports -- the USB serial devices a board could be attached to.
guided_detect_serial_ports() {
    { compgen -G '/dev/ttyACM*' || true; compgen -G '/dev/ttyUSB*' || true; } | sort
    return 0
}

# guided_first_dir_with MARKER DIR... -- the first DIR that contains MARKER (a file or dir).
guided_first_dir_with() {
    local marker="$1"; shift
    local d
    for d in "$@"; do
        if [ -n "$d" ] && [ -e "$d/$marker" ]; then
            printf '%s\n' "$d"
            return 0
        fi
    done
    return 0
}

# guided_first_file FILE... -- the first FILE that exists (e.g. an existing Wi-Fi PSK file).
guided_first_file() {
    local f
    for f in "$@"; do
        if [ -n "$f" ] && [ -f "$f" ]; then
            printf '%s\n' "$f"
            return 0
        fi
    done
    return 0
}

# ---------------------------------------------------------------- validators
# These mirror, rule for rule, the checks the flag paths run (install-esp32.sh's endpoint
# split, install-gateway.sh's reachable-as / host:port checks), so a bad answer is re-prompted
# with the same reason the flag path would die with. The flag path remains authoritative: the
# guided flow re-executes it with the assembled flags.

# guided_valid_reachable_as -- install-gateway.sh's --device-reachable-as rule.
guided_valid_reachable_as() {
    case "$1" in
        ''|127.0.0.1*|::1|localhost*|0.0.0.0*)
            echo "   must be an address a device can route to, not '$1' (loopback/any never receives a device's dial)"
            return 1 ;;
    esac
    return 0
}

# guided_valid_endpoint_strict -- install-esp32.sh's --endpoint rule (scheme, host, port, range).
guided_valid_endpoint_strict() {
    local body host port
    case "$1" in
        tls://*|tcp://*) ;;
        *) echo "   must be tls://host:port or tcp://host:port (got: $1)"; return 1 ;;
    esac
    body="${1#*://}"
    host="${body%%:*}"
    port="${body##*:}"
    if [ "$host" = "$body" ]; then
        echo "   no port: $1 (want tls://host:port)"
        return 1
    fi
    case "$host" in
        ''|*[![:alnum:].-]*) echo "   host is not usable: $host"; return 1 ;;
    esac
    case "$port" in
        ''|*[!0-9]*) echo "   port is not a number: $port"; return 1 ;;
    esac
    if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
        echo "   port out of range 1-65535: $port"
        return 1
    fi
    return 0
}

# guided_valid_endpoint_loose -- install-pi.sh's --endpoint rule (the port must be numeric).
guided_valid_endpoint_loose() {
    local port
    port="$(printf '%s' "$1" | sed 's#.*:##')"
    case "$port" in
        ''|*[!0-9]*) echo "   endpoint port is not a number: $1"; return 1 ;;
    esac
    return 0
}

# guided_valid_hostport -- install-gateway.sh's satellite-host rule (host:port, port 1-65535).
guided_valid_hostport() {
    local port
    port="$(printf '%s' "$1" | sed 's#.*:##')"
    case "$port" in
        ''|*[!0-9]*) echo "   not host:port: $1"; return 1 ;;
    esac
    if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
        echo "   port out of range 1-65535: $port"
        return 1
    fi
    return 0
}

# guided_valid_number -- a plain positive integer (ports).
guided_valid_number() {
    case "$1" in
        ''|*[!0-9]*) echo "   not a number: $1"; return 1 ;;
    esac
    return 0
}

# ---------------------------------------------------------------- prompts
# All prompts write the question to stdout and read one line from stdin. End of input is an
# ABORT (exit 1), never a silent default: a truncated piped answer list must not proceed to a
# write half-configured.

guided_read_line() {
    # guided_read_line VAR -- one line of stdin into VAR; aborts on EOF.
    local _answer
    if ! IFS= read -r _answer; then
        printf '\n'
        echo "guided: end of input before an answer; aborting (nothing was written)." >&2
        exit 1
    fi
    printf -v "$1" '%s' "$_answer"
}

# guided_prompt VAR "Question" ["default" ["validator"]] -- a required value. Bare Enter accepts
# the default (shown in [brackets]); a validator failure re-prompts with its printed reason.
guided_prompt() {
    local _var="$1" _q="$2" _def="${3:-}" _validator="${4:-}" _a
    while :; do
        if [ -n "$_def" ]; then
            printf '%s [%s]: ' "$_q" "$_def"
        else
            printf '%s: ' "$_q"
        fi
        guided_read_line _a
        [ -n "$_a" ] || _a="$_def"
        if [ -n "$_validator" ]; then
            if "$_validator" "$_a"; then
                break
            fi
        else
            if [ -n "$_a" ]; then
                break
            fi
            echo "   a value is required"
        fi
    done
    printf -v "$_var" '%s' "$_a"
}

# guided_prompt_secret VAR "Question" -- like guided_prompt for anything the person typed that
# must not sit on screen or in a scrollback: a passphrase. Echo is off for the read only, EOF is
# still an abort, and an empty answer is still refused. The value is never printed anywhere.
guided_prompt_secret() {
    local _var="$1" _q="$2" _a
    while :; do
        printf '%s: ' "$_q"
        # -s: do not echo. The typed newline never reached the terminal, so print one after.
        if ! IFS= read -rs _a; then
            printf '\n'
            echo "guided: end of input before an answer; aborting (nothing was written)." >&2
            exit 1
        fi
        printf '\n'
        if [ -n "$_a" ]; then
            break
        fi
        echo "   a value is required"
    done
    printf -v "$_var" '%s' "$_a"
}

# guided_prompt_optional VAR "Question" ["default" ["validator"]] -- like guided_prompt, but an
# empty value is a legal answer for a value the flag path treats as optional. When a default is
# shown, bare Enter accepts it and 'none' (or '-') means explicitly no value.
guided_prompt_optional() {
    local _var="$1" _q="$2" _def="${3:-}" _validator="${4:-}" _a
    while :; do
        if [ -n "$_def" ]; then
            printf '%s [%s] (none = no value): ' "$_q" "$_def"
        else
            printf '%s (none = no value): ' "$_q"
        fi
        guided_read_line _a
        case "$_a" in
            none|-) _a='' ;;
            '')     _a="$_def" ;;
        esac
        if [ -z "$_a" ] || [ -z "$_validator" ] || "$_validator" "$_a"; then
            break
        fi
    done
    printf -v "$_var" '%s' "$_a"
}

# guided_prompt_yn VAR "Question" ["y"|"n"] -- a yes/no question with the default shown as
# [Y/n] or [y/N]; bare Enter accepts it.
guided_prompt_yn() {
    local _var="$1" _q="$2" _def="${3:-y}" _shown _a
    case "$_def" in
        y|Y) _shown='Y/n' ;;
        *)   _shown='y/N'; _def='n' ;;
    esac
    while :; do
        printf '%s [%s]: ' "$_q" "$_shown"
        guided_read_line _a
        case "$_a" in
            '')              _a="$_def" ;;
            y|Y|yes|Yes|YES) _a='y' ;;
            n|N|no|No|NO)    _a='n' ;;
            *) echo "   please answer y or n"; continue ;;
        esac
        break
    done
    printf -v "$_var" '%s' "$_a"
}

# guided_confirm "Question" -- the final explicit yes/no gate. Returns 0 only for an explicit
# yes; anything else is a refusal (end of input aborts the run with exit 1).
guided_confirm() {
    local _a
    while :; do
        printf '%s (yes/no): ' "$1"
        guided_read_line _a
        case "$_a" in
            y|Y|yes|Yes|YES) return 0 ;;
            n|N|no|No|NO)    return 1 ;;
            *) echo "   please answer yes or no" ;;
        esac
    done
}

# guided_require_interactive NAME -- the zero-argument entry needs a terminal: a scripted run
# must pass flags instead of guessing at piped answers.
guided_require_interactive() {
    if [ ! -t 0 ]; then
        echo "$1: no arguments given and stdin is not a terminal." >&2
        echo "$1: the guided setup asks questions and needs an interactive terminal." >&2
        echo "$1: for automation, pass the flags explicitly (see --help)." >&2
        exit 2
    fi
}

# guided_end NAME ARGS... -- the shared ending: print the summary's command line, require the
# explicit confirmation, then re-exec THIS installer with the assembled flags so the guided run
# runs exactly the flag path's steps. Never returns. An installer that staged anything before the
# confirmation (install-esp32.sh's enrolment fetch) sets GUIDED_ON_APPROVE (move the staged files
# into place once the run is confirmed) and GUIDED_ON_ABORT (throw them away, so "nothing was
# written" stays true); with neither set, nothing about this function changes.
guided_end() {
    local _name="$1"; shift
    echo
    echo "I will now run:"
    echo "  $_name $*"
    echo
    if guided_confirm "Proceed?"; then
        if [ -n "${GUIDED_ON_APPROVE:-}" ]; then "$GUIDED_ON_APPROVE"; fi
        exec bash "$SCRIPT_PATH" "$@"
    fi
    if [ -n "${GUIDED_ON_ABORT:-}" ]; then "$GUIDED_ON_ABORT"; fi
    echo "$_name: aborted at confirmation; nothing was written." >&2
    exit 1
}
