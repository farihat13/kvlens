# engines/check-tools.sh -- sourced by each <db>/build.sh and <db>/lifecycle.sh, and by engines/setup.sh.
# The needs are data, engines/needs.tsv (its header says the columns); this file reads and checks them.
#   check_tools <db> <build|run>   silent when every need of that db and moment is met; otherwise "missing: <what>" per
#                                  need, then the command that installs them, and exit 1.
#   needs_of <db> [when]           the db's rows: how, what, check (tab-separated)
#   need_met <what> <check>        exit 0 when the need is met
NEEDS_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/needs.tsv"

needs_of() {
    awk -F'\t' -v db="$1" -v w="${2:-}" '!/^#/ && NF >= 5 && $1 == db && (w == "" || $2 == w) { print $3 "\t" $4 "\t" $5 }' "$NEEDS_FILE"
}

need_met() {
    case "$2" in
        cmd:*) command -v "${2#cmd:}" > /dev/null ;;
        # a machine without dpkg-query cannot tell: not checked there (the build says what it lacks)
        pkg) ! command -v dpkg-query > /dev/null || dpkg-query -W -f='${Status}' "$1" 2> /dev/null | grep -q "ok installed" ;;
        sh:*) bash -c "${2#sh:}" > /dev/null 2>&1 ;;
        *) echo "$NEEDS_FILE: unknown check '$2' ($1)" >&2; return 2 ;;
    esac
}

check_tools() {
    local db=$1 when=$2 how what check missing=0
    while IFS=$'\t' read -r how what check; do
        need_met "$what" "$check" && continue
        [ "$check" = "cmd:$what" ] && echo "missing: $what" >&2 || echo "missing: $what (${check#cmd:})" >&2
        missing=1
    done < <(needs_of "$db" "$when")
    [ "$missing" = 0 ] && return 0
    echo "Install what is missing: bash engines/setup.sh --db $db (engines/needs.tsv lists it)" >&2
    exit 1
}
