for d in /proc/[0-9]*; do
    p=${d#/proc/}
    for f in $d/fd/*; do
        case "$(readlink "$f" 2>/dev/null)" in /dev/dri/*) ;; *) continue ;; esac
        n=${f##*/}
        [ -r "$d/fdinfo/$n" ] || continue
        awk -v pid="$p" -v comm="$(cat $d/comm 2>/dev/null)" \
            '/^drm-engine-/ { sub(/^drm-engine-/,"",$1); sub(/:$/,"",$1); print "gpu=" comm " " pid " " $1 " " $2 }
             /^drm-driver/  { print "gpudrv=" $2 }' "$d/fdinfo/$n"
    done
done
