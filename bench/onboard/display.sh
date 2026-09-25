drm=$(grep -l "^connected$" /sys/class/drm/card*-HDMI-*/status 2>/dev/null | head -1)
if [ -n "$drm" ]; then d=${drm%/status}; echo "drm:${d##*/}"; exit 0; fi
tv=$(tvservice -s 2>/dev/null)
case "$tv" in
  (*HDMI*|*DVI*) echo "tvservice:$(printf '%s' "$tv" | sed 's/^state [^ ]* //')" ;;
esac
true
