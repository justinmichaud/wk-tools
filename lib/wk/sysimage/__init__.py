"""`wk sysimage`: build, find, write and remove the images a machine boots for one run."""


class Failed(Exception):
    pass


def fail(text):
    raise Failed(text)
