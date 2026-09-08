# The darwin tailscaled this fleet builds, pinned. Read by bench/mac-tailnet.sh
# with sed, which is why every line is a plain `NAME = "value"`.
#
# There is no tailscaled to download: pkgs.tailscale.com publishes tarballs for
# nine Linux arches and none for darwin, and the packaged macOS clients tunnel
# through NetworkExtension, whose "would like to add VPN configurations" panel
# only a person can answer. The daemon in the source tree opens a utun directly
# (github.com/tailscale/wireguard-go tun/tun_darwin.go: an AF_SYSTEM /
# SYSPROTO_CONTROL socket on `com.apple.net.utun_control`), which is root's to
# do and raises nothing. So this lane builds it.
#
# TS_VERSION is NOT here. image/yocto/meta-wk-tailnet/recipes-network/tailscale/
# tailscale-release.inc is the fleet's one version, and the Mac runs the same
# tailscale as every board. TS_SRC_FOR names the version TS_SRC_SHA256 is the
# source of, so a bump there without a bump here is a refusal rather than a
# checksum mismatch nobody can read.
#
# The source is the Go module proxy's zip, not a GitHub archive: the module
# proxy protocol makes a published version immutable, and the same bytes are
# attested in the public checksum log (sum.golang.org/lookup/tailscale.com@v...).
# A GitHub-generated tarball carries no such promise.
#
# To bump, both together --
#
#   v=<version from tailscale-release.inc>
#   curl -fsSL "https://proxy.golang.org/tailscale.com/@v/v$v.zip" | sha256sum
#
# GO_VERSION is the latest patch of the line tailscale's go.mod asks for
# (`go 1.26.5` in 1.102.2), so the build is the toolchain upstream tests on.
# The build runs GOTOOLCHAIN=local: a Go that silently downloads another Go is
# not a pinned build. Checksums are go.dev's own, published per file --
#
#   curl -fsSL 'https://go.dev/dl/?mode=json' | python3 -c 'import json,sys
#   [print(f["os"], f["arch"], f["sha256"]) for r in json.load(sys.stdin)[:1]
#    for f in r["files"] if f["kind"] == "archive"]'
#
# One per host that stages a bench volume: darwin/arm64 is tolken, which is
# where startosinstall runs, and linux/arm64 is moose, which cross-compiles the
# same darwin/arm64 binaries. A host with no line here is a refusal naming the
# key to add.

TS_SRC_FOR = "1.102.2"
TS_SRC_SHA256 = "a08de49e71ba4d1e8d329847c4f026b1ee696ef31697607a1c8a1c36f551fed0"

GO_VERSION = "1.26.8"
GO_SHA256_darwin_arm64 = "a012b25b571bd0138a03dcd25375ceba866fe5ca822f426d2c66a4de56fd3f4b"
GO_SHA256_linux_arm64 = "211ffced9dcb9633a55eac6364816ec0ddd951389a740e88fa8b3337971bdda0"
