# Working environment

You are an agent in a **wk workspace**: one WebKit checkout for one task, on a build server. Your working directory
is the checkout, and `$WK_WORKSPACE` is the workspace's name.

The workspace is disposable. Scratch files, experimental branches and broken build directories are fine; prefer
making a mess here over being careful. The server is not disposable, and other work runs on it.

## Build, run and test through wk

```
wk build $WK_WORKSPACE <preset>            # jsc-release, gtk-debug, wpe-release, mac-release, ...
wk build $WK_WORKSPACE <preset> --detach   # returns at once; the build goes on as a task on the server
wk build $WK_WORKSPACE --kill              # stop it
wk run $WK_WORKSPACE -- <args>             # run jsc from the current build
wk test $WK_WORKSPACE -- <tests>           # run tests
wk status $WK_WORKSPACE                    # what is running; the exit code is machine-readable (2 is busy)
wk status $WK_WORKSPACE --wait             # block until it is not busy, instead of polling
wk status $WK_WORKSPACE --log              # the build log
```

`wk` derives the job count from the memory the server has free: a raw `ninja -j$(nproc)` has taken servers down.
Build, run and test through it.

A build is tens of minutes and outlives the command that started it: start it with `--detach`, then wait with
`wk status $WK_WORKSPACE --wait` and read `--log` when it ends. One build runs on a server at a time; yours may
queue behind another.

## Git

Commit freely in this checkout when the task calls for it. `git push`, `git-webkit` and `gh` work as they do in the
person's own shell.

`origin` is WebKit/WebKit, fetched from the server's mirror; `fork` is the person's fork. Never rewrite a branch
somebody else can see.

## Network

The network is not restricted. Fetching documentation, bug reports and source is fine.

## WebKit conventions

Where the `jsc` skill is installed it is mandatory for any edit under `Source/`: it carries the house rules for
comment style, C++ conventions, smart pointers, Safer C++, naming and copyright headers. Invoke it before editing,
not after.

Before considering a change done, run `Tools/Scripts/check-webkit-style`.
