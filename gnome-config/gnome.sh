#!/bin/bash

# dconf dump / > saved_settings.dconf

dconf load -f / < config.dconf

echo "Done"