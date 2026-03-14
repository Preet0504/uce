from uce.core.config import load_config
from uce.server.mcp_server import run_server


def main():
    config = load_config("config.yaml")
    run_server(config)


if __name__ == "__main__":
    main()
