#!/bin/bash

if test -z "$1"
then
  echo "File name not provided"
  exit 1
fi

file_path=$(echo "$1" | sed 's/.*Source\///' | sed 's/:.*//')
file_path=$(echo "$file_path" | sed 's/.*PrivateHeaders\/JavaScriptCore\///' | sed 's/:.*//')
line_num=$(echo "$1" | grep -Po '(?<=:)[0-9]*')
window_title="^KittyHelix"
window_title="^hx"

#notify-send "Got $file_path line $line_num"

kitty @ $KITTYHELIX send-text --match title:"$window_title" '\E'
sleep 0.1
kitty @ $KITTYHELIX send-text --match title:"$window_title" ' f'
kitty @ $KITTYHELIX send-text --match title:"$window_title" "$file_path"
kitty @ $KITTYHELIX focus-tab --match title:"$window_title"
sleep 1
kitty @ $KITTYHELIX send-text --match title:"$window_title" "\rg${line_num}g"

