#!/bin/bash
# PreToolUse (Edit|Write|MultiEdit): once per session, when editing a WebKit source file,
# inject a reminder that the jsc skill is mandatory. Guards against forgetting to load it.
input=$(cat)
file=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')
session=$(printf '%s' "$input" | jq -r '.session_id // "nosession"')
[ -z "$file" ] && exit 0

case "$file" in
    /Users/justinmichaud/Development/DebugVersion/OpenSource/*) ;;
    */Source/JavaScriptCore/*|*/Source/WebCore/*|*/Source/WTF/*|*/Source/bmalloc/*|*/Source/WebKit/*) ;;
    *) exit 0 ;;
esac

sentinel="${TMPDIR:-/tmp}/claude-jsc-skill-reminder-${session}"
[ -e "$sentinel" ] && exit 0
touch "$sentinel" 2>/dev/null

cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"You are editing a WebKit source file. The jsc skill is mandatory for any WebKit edit (per its own description). If you have not invoked it this session, invoke the jsc skill (Skill tool: jsc) before continuing. Before considering the change done you MUST run its comment pass and Tools/Scripts/check-webkit-style."}}
JSON
exit 0
