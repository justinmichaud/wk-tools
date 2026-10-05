_wk_completion() {
    local cur prev bin cmd i typed=0 words= first= verb= slot shift=0
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    bin="${COMP_WORDS[0]}"
    if [ "$COMP_CWORD" -eq 1 ]; then
        COMPREPLY=( $(compgen -W "$_wk_commands" -- "$cur") )
        return 0
    fi
    cmd="${COMP_WORDS[1]}"
    _wk_decl "$cmd" || return 0
    if [ "$prev" = --preset ] && [ "$_wk_preset" = --preset ]; then
        COMPREPLY=( $(compgen -W "$_wk_presets" -- "$cur") )
        return 0
    fi
    i=2
    while [ "$i" -lt "$COMP_CWORD" ]; do
        case " $_wk_valued " in
            *" ${COMP_WORDS[$i]} "*) i=$((i + 1)) ;;
            *) case "${COMP_WORDS[$i]}" in -*) ;; *) [ -n "$first" ] || first=${COMP_WORDS[$i]}; typed=$((typed + 1)) ;; esac ;;
        esac
        i=$((i + 1))
    done
    case "$cur" in
        -*)
            words=$_wk_flags
            if [ -n "$_wk_vslots" ]; then
                case " $_wk_subverbs " in *" $first "*) verb=$first ;; *) verb=$_wk_default ;; esac
                words=$(_wk_lookup "$_wk_vflags" "$verb")
                words=${words//,/ }
            fi
            COMPREPLY=( $(compgen -W "$words" -- "$cur") )
            return 0 ;;
    esac
    slot=$_wk_slot
    if [ -n "$_wk_vslots" ]; then
        # the workspace's place is the verb's: typed, or the default one a first word stands for
        case " $_wk_subverbs " in *" $first "*) verb=$first ;; *) verb=$_wk_default; shift=1 ;; esac
        slot=$(_wk_lookup "$_wk_vslots" "$verb")
        [ -n "$slot" ] || slot=0
        [ "$slot" -eq 0 ] || slot=$((slot - shift))
    fi
    if [ "$slot" -gt 0 ] && [ "$typed" -eq $((slot - 1)) ]; then
        words=$(PYTHONPATH="$_wk_root/lib" python3 -m wk.completion --list-workspaces 2>/dev/null)
    fi
    if [ -n "$_wk_vslots" ]; then
        [ "$typed" -ne 0 ] || words="$words $_wk_subverbs"
        [ "$typed" -ne 1 ] || [ "$shift" -ne 0 ] || [ -z "$_wk_vals" ] || words="$words $(_wk_values "$bin" "$cmd" "$_wk_vals")"
    elif [ -z "$words" ] && [ "$typed" -eq "$_wk_first_arg" ]; then
        words="$_wk_subverbs"
        [ "$_wk_preset" != arg ] || words="$words $_wk_presets"
        [ -z "$_wk_vals" ] || words="$words $(_wk_values "$bin" "$cmd" "$_wk_vals")"
    fi
    COMPREPLY=( $(compgen -W "$words" -- "$cur") )
    return 0
}
_wk_lookup() {
    local r=" $1"
    case "$r" in *" $2:"*) r=${r#* $2:}; printf '%s' "${r%% *}" ;; esac
}
_wk_values() {
    local out
    out=$("$1" "$2" "$3" 2>&1) || return 0
    printf '%s\n' "$out" | awk '!/:$/ && NF { match($0, /^ */); d[NR] = RLENGTH; w[NR] = $1
        if (m == "" || RLENGTH < m) m = RLENGTH } END { for (i = 1; i <= NR; i++) if ((i in d) && d[i] == m) print w[i] }'
}
complete -F _wk_completion wk
