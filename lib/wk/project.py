"""The names of the project lab code checks out, builds and measures: project.json beside this file, which the wk layer owns."""
import json
import os

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "project.json")


def _frozen(v):
    if isinstance(v, list):
        return tuple(_frozen(x) for x in v)
    return {k: _frozen(x) for k, x in v.items()} if isinstance(v, dict) else v


def get(key):
    with open(PATH) as f:
        return _frozen(json.load(f)[key])
