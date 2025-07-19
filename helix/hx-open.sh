#!/bin/bash

if test -z "$1"
then
  echo "File name not provided"
  exit 1
fi

file_path=$(echo "$1" | sed 's/.*Source\///' | sed 's/:.*//')
line_num=$(echo "$1" | grep -Po '(?<=:).*')
window_title="^KittyHelix"

notify-send "Got $file_path line $line_num"

kitty @ $KITTYHELIX send-text --match title:$window_title '\E'
sleep 0.1
kitty @ $KITTYHELIX send-text --match title:$window_title ' f'
kitty @ $KITTYHELIX send-text --match title:$window_title "$file_path"
kitty @ $KITTYHELIX focus-tab --match title:$window_title
sleep 3
kitty @ $KITTYHELIX send-text --match title:$window_title "\rg${line_num}g"

