Look at all the different ways we build or configure images:

- Temp images for provisioning (eg: jumpdrive for pmos)
- pmos images for appliances
- yocto webkit builds for benchmarking
- yocto webkit builds for hosting the benchmark runner and installing other yocto builds
- mac perf images
- mac vm images

Look at all the different ways we benchmark:
- rebooting macOS into a 100% reproducable perf testing install
- rebooting moose, rpi5, rpi4 or rpi3 into a yocto webkit image
- benchmarking MiniBrowser or JSC cli in a container for faster tests

We need a way to manage this complexity. In addition, we need to have a common way to get results and display them, including displaying a histogram and html result summary.

This should have the smallest amount of duplicated code to ensure that we don't have bugs creep in in different modes.

Clear all of this up, and test the html report generation in a container benchmark run.
