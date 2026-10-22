"""Completion helper installed by anvil. Generated; edits will be lost."""

import os
import sys

sys.path.insert(0, 'C:\\Users\\Robbie\\Documents\\Default Project\\anvil\\src\\anvil')


def main() -> int:
    argv = sys.argv[1:]
    shell = os.environ.get("ANVIL_COMPLETE_SHELL", "bash")
    words = argv
    current = ""
    if words and words[-1] == "":
        words, current = words[:-1], ""

    from anvil.completion_probe import complete
    try:
        for suggestion in complete(words, current, shell=shell):
            print(suggestion)
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
