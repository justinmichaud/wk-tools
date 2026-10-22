for t in FTL DFG Baseline; do
    echo "tier=$t $(grep -c "with $t into" /tmp/wk-browser.log 2>/dev/null || echo 0)"
done
