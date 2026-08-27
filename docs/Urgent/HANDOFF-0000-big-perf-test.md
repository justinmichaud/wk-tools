First do docs/Urgent/HUMAN-benchmark-start.md

- Build the following configs, confirm they work, and are suitable for use in an A/B test:
  - WPE 2.38 X (buildroot) X (rpi3 32-bit, rpi4 32-bit, rpi5 64-bit)
  - WPE 2.46 X (buildroot, yocto) X (rpi3 32-bit, rpi4 32-bit, rpi5 64-bit)
  - WPE 2.52 X (yocto) X (rpi3 32-bit, rpi4 32-bit, rpi5 64-bit)

Confirm they can run an A/B test. 

A/B testing is one image, two webkits. There should not be a reboot between them.

Images should show up in wk list, not be duplicated, and be easy to build incrementally if small changes need to be made.

Make the test have the same code on both side (minus some identifier inside the binary, which all a/b tests should have and the tooling should verify in the running binary to rule out linking bugs or interrupted state).

Meausre the varience we can measure reliably.

Confirm that we can generate subtest results and a histogram, plus normal compare-results tests on JS3, MM and SP3.

Then do docs/Urgent/HUMAN-Benchmarking variance.md
