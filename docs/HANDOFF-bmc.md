How can I make my librem5 bmc auto turn on after power loss? How can I recover this device if it is left off without being physically present?

Going forward, we will name the librem5 tailnet-bridge-X and switch to pmos for portability. We will support the librem 5 and pinephone as the primary devices.

This role should:
- Support bridging the ethernet to the tailnet
- Support streaming the camera whenever the kill switch is on, so that I can watch the screen remotely.
- Be resilient to power loss and network failures.

Make the setup files run from this repo instead of the phone, and get rid of the tar file in this repo. It should be one command to re-provision a new tailnet bridge device, and it should be easy to remove one.

We will first flash my pinephone to act as a bridge for the rpi4 or rpi3 (tailnet-bridge-generic). Then, we will re-flash my librem5 to act as a bridge for the bmc (rpi-bridge-moose-bmc).
