#!/bin/bash

echo "Check for allow_discards on every line here:"
sudo dmsetup table

# Enable luks
# Disable lockdown for https://github.com/erpalma/throttled/
rpm-ostree kargs --append=rd.luks.options=discard --append=lsm=capability,yama,selinux,bpf,landlock,ipe,ima,evm

sudo systemctl enable fstrim.service

flatpak install org.gottcode.Kapow
flatpak install com.google.Chrome

rpm-ostree install input-leap

echo "Done"
