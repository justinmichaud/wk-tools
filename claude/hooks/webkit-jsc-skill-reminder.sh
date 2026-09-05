#!/bin/bash
# PreToolUse (Edit|Write|MultiEdit): once per session, when a WebKit source file is edited, injects a reminder that the jsc skill is mandatory.
input=$(cat)
file=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')
session=$(printf '%s' "$input" | jq -r '.session_id // "nosession"')
[ -z "$file" ] && exit 0

case "$file" in   # tree shape and not an absolute path: a workspace lives at a different path on every machine and in every container, so these cover any WebKit checkout wherever it is
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
