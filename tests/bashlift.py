"""Shell functions lifted out of a script by name, to run against stand-in directories and stub tools."""
import subprocess


def lift(path, *funcs):
    """The text of each `name() {` ... `}` function in `path`, joined."""
    out = []
    for func in funcs:
        text = subprocess.run(["sed", "-n", f"/^{func}()/,/^}}/p", str(path)],
                              capture_output=True, text=True).stdout
        assert text.strip(), f"could not lift {func} from {path}"
        out.append(text)
    return "\n".join(out)
