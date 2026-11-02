import logging
import os

from .config import Config
from .controller import JtopStartTimeout, run


def main() -> None:
    try:
        run(Config())
    except JtopStartTimeout:
        # run() has already forced the fan on and saved the history. Skip
        # the normal interpreter shutdown: multiprocessing's exit handlers
        # would try to talk to the jtop server that just stopped answering,
        # and could hang the same way. systemd restarts the service.
        logging.getLogger(__name__).exception("Could not start jtop; exiting")
        os._exit(1)


if __name__ == "__main__":
    main()
