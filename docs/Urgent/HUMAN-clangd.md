Compile commands are on by default and complete (`--export-compile-commands`
on every build, build/build-in-target.sh); clangd in Zed and helix reads them
from `WebKitBuild/JSCOnly/Release` (dotfiles/zed/settings.json,
container/helix/languages.toml), repointed by hand for another config.

Owed, each reproduced from inside a real workspace (`wk zed`, `wk ai claude`):

- Deep dive into the various lsp errors and see how we can fix them without
  changing the WK source code.
- Deep dive into all of the places where jump-to-definition is not working
  correctly.
- Deep dive and do research on performance. Maximise indexing perf, while
  avoiding OOM errors. The current flags (`--background-index-priority=low`,
  `-j=4`, `--pch-storage=memory`, `--malloc-trim`, `--limit-results=200`) are
  unmeasured.
