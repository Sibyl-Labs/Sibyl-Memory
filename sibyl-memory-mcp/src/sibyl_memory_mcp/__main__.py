"""Entry point: `sibyl-memory-mcp` console script + `python -m sibyl_memory_mcp`."""
from .server import run_stdio


def main() -> None:
    run_stdio()


if __name__ == "__main__":
    main()
