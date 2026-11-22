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

`wk` derives the job count from the memory the server has free. A raw `ninja -j$(nproc)` has taken shared build
machines down, so the build tools on your `PATH` (`build-webkit`, `build-jsc`, `cmake`, `ninja`, `make`,
`xcodebuild`, `run-webkit-tests`, `run-javascriptcore-tests`) refuse to run for you and name the `wk` command to use.
Do not route around them by path.

A build is tens of minutes and outlives the command that started it: start it with `--detach`, then wait with
`wk status $WK_WORKSPACE --wait` and read `--log` when it ends. One build runs on a server at a time; yours may
queue behind another.

Every build is watched for memory. If yours is killed over its budget, `wk status` says so: that is the machine
protecting itself, not a fault in the code.

## Git

Commit freely in this checkout when the task calls for it.

**You cannot push, and must not try.** No key or token that publishes is readable by you: the credentials on this
server are hidden from your process, and you inherit no token and no ssh agent. That is deliberate. Do not look for
a credential and do not work around a refused push, pull request or bug comment. Say what you would push (the branch,
the commits, the remote) and let the person do it from a shell of their own (`wk enter`).

`origin` is WebKit/WebKit, fetched from the server's mirror; `fork` is the person's fork. Never rewrite a branch
somebody else can see.

## Network

The network is not restricted. Fetching documentation, bug reports and source is fine.

## WebKit conventions

Where the `jsc` skill is installed it is mandatory for any edit under `Source/`: it carries the house rules for
comment style, C++ conventions, smart pointers, Safer C++, naming and copyright headers. Invoke it before editing,
not after.

Before considering a change done, run `Tools/Scripts/check-webkit-style`.
