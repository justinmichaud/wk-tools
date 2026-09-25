mkdir -p "$WK_BYHOST" && defaults write "$WK_SAVER" idleTime -int 0; defaults write "$WK_PREFS" askForPassword -int 0
defaults read "$WK_SAVER" idleTime 2>/dev/null
