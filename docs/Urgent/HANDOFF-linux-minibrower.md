Support debugging a layout test

Support running minibrowser graphically so that I can interact with it. Support attaching a debugger.

https://github.com/justinmichaud/justinmichaud.github.io/wiki/Debugging-WPE-Linux-(desktop)

Debugging should not get tripped up by prewarm processes, pson or site isolation.

The attach-a-debugger half is specced as `wk debug` in `docs/HANDOFF-debug.md` — implement the two together, not twice.
