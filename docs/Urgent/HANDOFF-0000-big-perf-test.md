- We need to re-provision rpi3 and 4 from scratch and confirm they work. We need to re-configure rpi5 from scratch as a workstation, confirm this is automated, then re-provision it from scratch and confirm we can bulid + test a yocto image.

- Build the following configs, confirm they work, and are suitable for use in an A/B test:
  - WPE 2.38 X (buildroot) X (rpi3 32-bit, rpi4 32-bit, rpi5 64-bit)
  - WPE 2.46 X (buildroot, yocto ) X (rpi3 32-bit, rpi4 32-bit, rpi5 64-bit)
  - WPE 2.52 X (yocto) X (rpi3 32-bit, rpi4 32-bit, rpi5 64-bit)

Confirm they can run an A/B test. Make the test have the same code on both side, and meausre the varience we can measure reliably. Confirm they connect only through tailscale, and that we can generate subtest results and a histogram. then do handoff-benchmarking-variance.
