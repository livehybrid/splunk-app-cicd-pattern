#!/usr/bin/env python
"""
cicdhello custom search command.

    | cicdhello

Returns exactly one event naming the add-on version and the commit hash the
package was built from. That is the whole add-on. It exists so the pipeline has
something real to build, vet and ship, and so you can prove after a deploy that
the thing on the stack is the thing you built.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "lib"))

from splunklib.searchcommands import Configuration, GeneratingCommand, dispatch


def _app_version():
    """Read the version ucc-gen stamped into default/app.conf at build time.

    The version carries the short commit hash (1.2.3+ab12cd3), so the event
    tells you which commit is actually running on the stack.
    """
    app_conf = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), os.pardir, "default", "app.conf"
    )
    try:
        with open(app_conf) as fh:
            for line in fh:
                if line.strip().startswith("version"):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


@Configuration()
class CicdHelloCommand(GeneratingCommand):
    def generate(self):
        yield {
            "_time": datetime.now(timezone.utc).timestamp(),
            "_raw": "TA-cicd-pattern installed and executable. This is the cicdhello command output.",
            "app": "TA-cicd-pattern",
            "version": _app_version(),
            "python_version": "{}.{}.{}".format(*sys.version_info[:3]),
        }


if __name__ == "__main__":
    dispatch(CicdHelloCommand, sys.argv, sys.stdin, sys.stdout, __name__)
