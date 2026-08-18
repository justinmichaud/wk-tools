# HANDOFF — helix as a real editor on the rpi5

Make helix work with dotfiles configured to cover most of what zed provides —
in particular an easy way to view and edit git changes across multiple files.
The motivation is the rpi5: zed cannot run there with acceptable performance,
helix can, so this decides the development experience on that machine.

Starting points: `container/helix/` holds the config firstrun installs into
workspaces; the rpi5 is reached over the tailnet (`wk pi setup rpi5`,
`host/linux/rpi5/`).
