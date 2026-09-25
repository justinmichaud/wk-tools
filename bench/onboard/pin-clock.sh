for p in /sys/devices/system/cpu/cpufreq/policy*; do
  [ -w "$p/scaling_governor" ] && echo performance > "$p/scaling_governor"
  [ -w "$p/scaling_min_freq" ] && cat "$p/scaling_max_freq" > "$p/scaling_min_freq"
done 2>/dev/null
p=/sys/devices/system/cpu/cpufreq/policy0
echo "governor=$(cat $p/scaling_governor 2>/dev/null)"
echo "min=$(cat $p/scaling_min_freq 2>/dev/null)"
echo "max=$(cat $p/scaling_max_freq 2>/dev/null)"
