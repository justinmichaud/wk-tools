echo "--- ps"; ps -o pid,etime,time,rss,comm 2>/dev/null || ps w
echo "--- browser log"; tail -c 20000 /tmp/wk-browser.log 2>/dev/null
echo "--- weston"; tail -20 /tmp/wk-weston.log 2>/dev/null
echo "--- dmesg"; dmesg 2>/dev/null | tail -30
true
