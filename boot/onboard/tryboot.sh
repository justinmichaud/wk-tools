wk_sd_own=
wk_sd_mount() {
    wk_sd_m=$(grep -m1 "^$WK_SD " /proc/mounts | cut -d" " -f2) || wk_sd_m=""
    [ -z "$wk_sd_m" ] || return 0
    wk_sd_m=$(mktemp -d)
    mount -t vfat "$@" "$WK_SD" "$wk_sd_m" || { rmdir "$wk_sd_m"; return 1; }
    wk_sd_own=1
}
wk_sd_drop() {
    [ -n "$wk_sd_own" ] || return 0
    sync; umount "$wk_sd_m"; rmdir "$wk_sd_m"; wk_sd_own=
}
wk_root_of() { tr " " "\n" < "$1" | grep "^root=" | head -1; }

wk_stage() {
    set -e
    wk_sd_mount || { echo 'cannot reach the SD boot partition to stage onto' >&2; exit 9; }
    boot=$wk_sd_m
    src=$(mktemp -d)
    mount -o ro "$WK_SRC" "$src"
    kernel=$(sed -n 's/^kernel=//p' "$src/config.txt" | tail -1)
    if [ -z "$kernel" ]; then
        for k in kernel8.img kernel7l.img kernel7.img kernel.img; do
            [ -f "$src/$k" ] || continue
            [ -z "$kernel" ] || { echo "the bench system names no kernel= and its boot partition holds $kernel and $k: which one the firmware picks is not this staging's guess to make. Name it with kernel= in the profile's config.txt.append." >&2; umount "$src"; exit 9; }
            kernel=$k
        done
    fi
    [ -n "$kernel" ] || { echo 'the bench system config.txt names no kernel= and its boot partition holds none of kernel8.img kernel7l.img kernel7.img kernel.img' >&2; umount "$src"; exit 9; }
    rm -rf "$boot/second.new" "$boot/second"
    mkdir "$boot/second.new"
    cp "$src/$kernel" "$boot/second.new/$kernel"
    cp "$boot/start4.elf" "$boot/fixup4.dat" "$boot/second.new/"
    cp "$src/$WK_DTB" "$boot/second.new/"
    [ -d "$src/overlays" ] && cp -r "$src/overlays" "$boot/second.new/overlays"
    sed '/ panic=[0-9]/!s/[[:space:]]*$/ panic=10/' "$src/cmdline.txt" > "$boot/second.new/cmdline.txt"
    bits=''
    case "$(od -An -tx4 -j36 -N4 "$src/$kernel" | tr -d ' ')" in (016f2818) bits=0 ;; esac
    [ -n "$bits" ] || case "$(od -An -tx4 -j56 -N4 "$src/$kernel" | tr -d ' ')" in (644d5241) bits=1 ;; esac
    # os_prefix leads: the firmware resolves a filename as it reads the directive naming it.
    { echo 'os_prefix=second/'; [ -z "$bits" ] || echo "arm_64bit=$bits"; sed '/^os_prefix=/d; /^arm_64bit=/d' "$src/config.txt"; } > "$boot/tryboot.txt.new"
    umount "$src"; rmdir "$src"
    mv "$boot/second.new" "$boot/second"
    mv "$boot/tryboot.txt.new" "$boot/tryboot.txt"
    sync
}

case "$WK_DO" in
    stage) wk_stage ;;
    disarm)
        wk_sd_mount || exit 0
        rm -rf "$wk_sd_m/second" "$wk_sd_m/tryboot.txt" ;;
    staged)
        wk_sd_mount -o ro || exit 0
        if [ -f "$wk_sd_m/tryboot.txt" ] && [ -d "$wk_sd_m/second" ]; then echo yes; else echo no; fi ;;
    staged-root)
        wk_sd_mount -o ro || exit 0
        wk_root_of "$wk_sd_m/second/cmdline.txt" 2>/dev/null || true ;;
    source)
        r=$(wk_root_of /proc/cmdline)
        wk_sd_mount -o ro || { echo unknown; exit 0; }
        sd=$(wk_root_of "$wk_sd_m/cmdline.txt" 2>/dev/null)
        st=$(wk_root_of "$wk_sd_m/second/cmdline.txt" 2>/dev/null)
        if [ -n "$st" ] && [ "$r" = "$st" ]; then echo staging
        elif [ -n "$sd" ] && [ "$r" = "$sd" ]; then echo sd-config
        else echo unknown; fi ;;
esac
wk_sd_drop
