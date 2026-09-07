from .config import Config
from .controller import run


def main() -> None:
    run(Config())


if __name__ == "__main__":
    main()
