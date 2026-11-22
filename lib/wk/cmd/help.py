"""List the commands, or print a section of the design.

`wk help` lists every command and every topic. `wk help <topic>` prints the section of DESIGN.md whose heading
starts with the topic (`wk help seams`); `wk help design` prints all of it. `wk <command> -h` explains one command."""

import importlib
import os

from wk.core.act import die
from wk.core.decl import Command, usage

COMMAND = Command(takes=("topic?",), readonly=True)


def main(args, inv):
    with open(os.path.join(inv.root, "DESIGN.md")) as f:
        design = f.read()
    sections = {"design": design}
    for part in design.split("\n## ")[1:]:
        sections[part.split("\n")[0].lower()] = "## " + part
    if args.topic is None:
        for key in sorted(inv.commands):
            module = importlib.import_module(inv.commands[key])
            print("%-44s %s" % (usage(key, module.COMMAND)[len("usage: "):], module.__doc__.strip().split("\n")[0]))
        print("\ntopics (wk help <topic>): %s" % ", ".join(sections))
        return
    found = [text for heading, text in sections.items() if heading.startswith(args.topic.lower())]
    if not found:
        die("no topic starts with %s: the topics are %s" % (args.topic, ", ".join(sections)))
    print(found[0].rstrip())
