# helix in a wk-tools workspace

`hx` is installed once by `container/firstrun.sh` (`_install_helix`), which
also copies this directory to `~/.config/helix/`. Editing a file here changes
the *next* workspace's config, not a running one -- edit
`~/.config/helix/{config,languages}.toml` directly for a same-session change,
same as any other helix install.

A macOS guest's start (`lib/wk/guest.py`, `write_helix_config`) copies the
same directory, its `languages.toml` without the comments, without glibc's
`--malloc-trim`, and with clangd reading the checkout root: an Xcode build
leaves only fragments in `WebKitBuild/<Type>/compile_commands/`, which
`Tools/Scripts/generate-compile-commands WebKitBuild/<Type>` merges into the
checkout's `compile_commands.json`.

TODO: a macOS guest installs no `hx`, and no `wk build` there runs
`generate-compile-commands`.

## Coming from Zed: the same task, the helix way

| Zed | helix |
| --- | --- |
| `Cmd-P` fuzzy open | `space f` |
| `Cmd-Shift-F` search in project | `space /` |
| Click through to a definition | put the cursor on the symbol, `gd` |
| Find references | `gr` |
| Rename symbol | `space r` |
| Code action / quick fix | `space a` |
| Hover docs | `space k` |
| Buffer switcher | `space b` |
| Problems panel | `space d` (this buffer), `space D` (workspace) |
| Git panel | see "git", below -- helix has none |

`space f` and `space /` both respect `container/helix/ignore`: LayoutTests,
WebKitBuild, the other ports' Source/ subtrees and the rest of the noise this
checkout carries are filtered out so a fuzzy match actually lands on
JSTests/Tools/WebCore/WebKit, which is what a JSC workspace is for.

## git: lazygit, from a second terminal

helix has no panel system and no embedded terminal -- `:sh`/`:!` run a shell
command and capture or discard its output, they do not attach a tty, so
nothing in helix can host an interactive TUI. Faking a "git panel" keybinding
that quietly does something else would be worse than not having one.

The honest way to get lazygit next to a helix session: open a second terminal
into the same workspace (`wk enter <name>` again, or a second pane in
whatever multiplexer the outer terminal has) and run `lg` there -- installed
alongside helix, and aliased in `shell/bashrc` because `lazygit` on its own is
eleven characters to type every time.

## clangd and which build it points at

clangd needs a `compile_commands.json`, and this checkout's is written inside
whichever build directory produced it
(`lib/wk/presets.py`'s `build_dir`), not at the checkout root. Every
container workspace starts on `config=jsc-release`, which builds to
`WebKitBuild/JSCOnly/Release` -- so `container/helix/languages.toml` points
`--compile-commands-dir` there, and that directory has a `compile_commands.json`
as soon as `wk build <ws> jsc-release` has run once
(`build/build-in-workspace.sh`'s `--export-compile-commands` is on by default).

Working a different config -- `gtk-release`, `jsc-debug`, an Apple port --
instead? Build it at least once so its own `compile_commands.json` exists,
edit the `--compile-commands-dir` line in `~/.config/helix/languages.toml` to
match (see `build_dir` in `lib/wk/presets.py` for the path each config
uses), and restart clangd with `:lsp-restart`.
