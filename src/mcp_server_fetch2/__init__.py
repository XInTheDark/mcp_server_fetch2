from .server import serve


def main():
    """MCP Fetch Server - HTTP fetching functionality for MCP with FastMCP"""
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(
        description="give a model the ability to make web requests"
    )
    parser.add_argument("--user-agent", type=str, help="Custom User-Agent string")
    parser.add_argument(
        "--ignore-robots-txt",
        action="store_true",
        help="Ignore robots.txt restrictions",
    )
    parser.add_argument("--proxy-url", type=str, help="Proxy URL to use for requests")
    parser.add_argument("--port", type=int, help="Port to run the HTTP server on (defaults to PORT env var or 3000)")
    parser.add_argument(
        "--transport", 
        choices=["stdio", "http"], 
        default="http",
        help="Transport to use: stdio (traditional MCP) or http (FastMCP with HTTP)"
    )

    args = parser.parse_args()
    asyncio.run(serve(args.user_agent, args.ignore_robots_txt, args.proxy_url, args.port, args.transport))


if __name__ == "__main__":
    main()
