# Streamed into a guest on every start, ahead of the mirror wiring lib/wk/git.py appends.
set -u
git config --global --replace-all include.path "$WK_TOOLS/dotfiles/gitconfig"
git config --global core.sshCommand "$WK_TOOLS/container/push/wk-push-client.py"
git config --global ssh.variant ssh
if [ -d "$WK_SRC/.git" ]; then
    echo checkout=present
elif [ ! -d "$WK_MIRROR" ]; then
    echo "checkout=no-mirror: $WK_MIRROR is not there. The share is mounted at boot, so"
    echo "  'wk stop <name>', then 'wk start <name>' -- or the host has no mirror yet: wk sync"
    exit 1
elif git clone --quiet --shared --branch main "$WK_MIRROR" "$WK_SRC"; then
    echo checkout=cloned
else
    echo checkout=clone-failed; exit 1
fi
[ ! -r "$HOME/.wk-egress" ] || . "$HOME/.wk-egress"
