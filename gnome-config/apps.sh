#!/bin/bash

echo "Check for allow_discards on every line here:"
sudo dmsetup table

rpm-ostree kargs --append=rd.luks.options=discard

sudo systemctl enable fstrim.service

flatpak install org.gottcode.Kapow
flatpak install com.google.Chrome

rpm-ostree install input-leap

echo "Done"