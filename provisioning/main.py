"""Entry point del servizio systemd filesystem-agent."""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main(["run-filesystem-agent"]))
