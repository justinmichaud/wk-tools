#!/bin/bash
# PreToolUse (Edit|Write|MultiEdit): once per session, when editing a WebKit source file,
# inject a reminder that the jsc skill is mandatory. Guards against forgetting to load it.
input=$(cat)
file=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')
session=$(printf '%s' "$input" | jq -r '.session_id // "nosession"')
[ -z "$file" ] && exit 0

# Match on tree shape, not on an absolute path: workspaces live at different
# paths on every machine and inside every container, so the previous
# /Users/<user>/Development/DebugVersion/OpenSource prefix matched almost
# nowhere. These patterns cover any WebKit checkout, wherever it is.
case "$file" in
    */Source/JavaScriptCore/*|*/Source/WebCore/*|*/Source/WTF/*|*/Source/bmalloc/*|*/Source/WebKit/*) ;;
    */JSTests/*|*/LayoutTests/*|*/PerformanceTests/*|*/Tools/*) ;;
    *) exit 0 ;;
esac

sentinel="${TMPDIR:-/tmp}/claude-jsc-skill-reminder-${session}"
[ -e "$sentinel" ] && exit 0
touch "$sentinel" 2>/dev/null

cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"You are editing a WebKit source file. The jsc skill is mandatory for any WebKit edit (per its own description). If you have not invoked it this session, invoke the jsc skill (Skill tool: jsc) before continuing. Before considering the change done you MUST run its comment pass and Tools/Scripts/check-webkit-style."}}
JSON
exit 0
