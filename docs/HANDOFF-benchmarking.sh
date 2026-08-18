I want a way to set up a managed image automatically on an external drive that can be used for benchmarking.

It should automatically deploy to a usb drive, then I can boot into it and have it already be configured with tailscale (like the rpi5 provisioning).

Then, a benchmark runner on another computer can drive it with no sandboxing and complete maximum perf stability.

THis should work for macOS and linux, plus rpi5 (ubuntu), plus rpi5/4/3 (yocto).

Do research on what speed and size drive will be required. Ideally a 32gb flash drive would work, or the bmc image boot option for moose (there should be more than enough ram).

Netboot is another possibility that could work.

*** THis should supersede the previous discussion about rpi5. rpi5 should retain full privileges just like any other workstation, but when used for benchmarking, should netboot into a special benchmarking image, and should use podman for sandboxing claude just like moose. ***
