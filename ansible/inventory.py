#!/usr/bin/env python3
"""Ansible's inventory script: the machines of machines/*.conf, as `wk machine inventory` prints them."""
import os

wk = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wk")
os.execv(wk, [wk, "machine", "inventory"])
