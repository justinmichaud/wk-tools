# The tailscale release this fleet installs, in one place.
#
# Two things read this file and they must not drift: the recipe beside it, which
# puts the binaries *in* an image, and `wk pi setup`, which puts the same bytes
# on a board that is already running. A version in both places is two versions
# the day one of them is bumped -- and the failure that produces is a board
# whose tailscale is not the tailscale anything else in the fleet is running.
#
# Bitbake `require`s this; the shell reads it with sed, which is why every line
# is a plain `NAME = "value"` and nothing here is computed.
#
# The checksums are keyed by *tailscale's* architecture names rather than
# Yocto's, because the shell half knows only those (it maps `uname -m` itself).
# The recipe does the Yocto-arch-to-tailscale-arch mapping in one place.
#
# To bump: change TS_VERSION and replace both checksums with the published ones,
# which are files rather than something to compute --
#
#   for a in arm64 arm; do
#       curl -fsS "https://pkgs.tailscale.com/stable/tailscale_<version>_$a.tgz.sha256"
#   done
#
# arm64 is every 64-bit board and the Mac; `arm` is the rpi3, the fleet's only
# 32-bit machine (armv7l).

TS_VERSION = "1.102.2"
TS_SHA256_arm64 = "2b64e9ade7e73034b5ec9e9bcd537f5ddd14ae3abb435e57e929e7486ae42660"
TS_SHA256_arm = "4d514c8659f21aa21b11c10200e25c38e1f5400d3b0c5bc17560b2f7d9c5c676"
