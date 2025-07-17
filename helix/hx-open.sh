#!/bin/bash

if test -z "$1"
then
  echo "File name not provided"
  exit 1
fi

file_path=$(echo "$1" | sed 's/.*Source\///')
line_num=$2
window_title="^KittyHelix"

kitty @ send-text --match title:$window_title '\E'
sleep 0.1
kitty @ send-text --match title:$window_title ' f'
kitty @ send-text --match title:$window_title "$file_path"
kitty @ focus-tab --match title:$window_title
sleep 0.3
kitty @ send-text --match title:$window_title "\rg${line_num}g"
