# Mac bench mode
1) The mac in bench mode should be able to turn the screen on and off, and report this state as part of quiescing.
2) Confrim this setup will work with sip enabled on my workstation volume. The bench volume should have sip disabled to allow disabling every single possible system service.
3) The only manual step should be the tailscale key during setup, and rebooting back into normal mode (since booting into bench mode should be the default)

Confirm all of these things.

Do research to increase the reliability of this setup.

wk doctor should check all of these things and report accurately.

Everything should be driven over ssh over tailscale from the very first boot

some option should exist to run everything from the very beginning in a vm to confirm it is still working.

# Mac Variance

I need to be able to measure less than 0.1% regressions in SP3, JS3 and MM. This will require serious work. Do lots of research.

Look at other ways to combat this noise statistically.

- Changing env variables and the size of env (to change stack layout)
- Disabling ASLR, but measuring with multiple layouts manually
- Path lengths for the binaries
- With and without the shared cache
- Consider: we have 5 rounds of A and 5 of B, interleaved, with each combo of [aslr base, path length, shared cache, env sizes]. How can we pair up these data in a way that can eliminate this natural noise variance? We should also be able to see if a patch increases variance in one of these configurations, since that is also bad.

In addition to the compare-results output, give a histogram of both runs and all subtests in an html report. Remember that this must be generated automatically, everything here should work with one command.

We should compare both score and wall time.

Each run will have a full night (8 hours) to run autonomously.

One run from each side should be run under samply with jitdump.

# Linux variance

Everything in the investigation above should apply here too.
