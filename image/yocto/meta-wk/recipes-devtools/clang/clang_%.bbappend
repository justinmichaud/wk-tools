# yocto_ws.py raises PARALLEL_MAKE for clang (146 min of do_compile, measured, often the last recipe
# running). A compile peaks near 334 MB but a libLLVM/clang/lld link is gigabytes and meta-clang bounds
# none, so the links are bounded here, by a proxy for machine size, capped where disk becomes the limit.
# Build-time only. Delete this file if meta-clang ever bounds its own link jobs.
LLVM_PARALLEL_LINK_JOBS ?= "${@min(8, max(1, int(d.getVar('BB_NUMBER_THREADS') or '1')))}"
EXTRA_OECMAKE:append = " -DLLVM_PARALLEL_LINK_JOBS=${LLVM_PARALLEL_LINK_JOBS}"
