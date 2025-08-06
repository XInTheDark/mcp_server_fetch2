from typing import Annotated, Tuple
from urllib.parse import urlparse, urlunparse
import os
import tempfile
import asyncio

import markdownify
import readabilipy.simple_json
from protego import Protego
from pydantic import BaseModel, Field, AnyUrl
from markitdown import MarkItDown
from cachetools import TTLCache
from fastmcp import FastMCP

DEFAULT_USER_AGENT_AUTONOMOUS = "ModelContextProtocol/1.0 (Autonomous; +https://github.com/modelcontextprotocol/servers)"
DEFAULT_USER_AGENT_MANUAL = "ModelContextProtocol/1.0 (User-Specified; +https://github.com/modelcontextprotocol/servers)"


def extract_content_from_html(html: str) -> str:
    """Extract and convert HTML content to Markdown format.

    Args:
        html: Raw HTML content to process

    Returns:
        Simplified markdown version of the content
    """
    ret = readabilipy.simple_json.simple_json_from_html_string(
        html, use_readability=True
    )
    if not ret["content"]:
        return "<error>Page failed to be simplified from HTML</error>"
    content = markdownify.markdownify(
        ret["content"],
        heading_style=markdownify.ATX,
    )
    return content


def get_robots_txt_url(url: str) -> str:
    """Get the robots.txt URL for a given website URL.

    Args:
        url: Website URL to get robots.txt for

    Returns:
        URL of the robots.txt file
    """
    # Parse the URL into components
    parsed = urlparse(url)

    # Reconstruct the base URL with just scheme, netloc, and /robots.txt path
    robots_url = urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))

    return robots_url


async def check_may_autonomously_fetch_url(url: str, user_agent: str, proxy_url: str | None = None) -> None:
    """
    Check if the URL can be fetched by the user agent according to the robots.txt file.
    Raises a ValueError if not.
    """
    from httpx import AsyncClient, HTTPError

    robot_txt_url = get_robots_txt_url(url)

    async with AsyncClient(proxies=proxy_url) as client:
        try:
            response = await client.get(
                robot_txt_url,
                follow_redirects=True,
                headers={"User-Agent": user_agent},
            )
        except HTTPError:
            raise ValueError(f"Failed to fetch robots.txt {robot_txt_url} due to a connection issue")
        if response.status_code in (401, 403):
            raise ValueError(f"When fetching robots.txt ({robot_txt_url}), received status {response.status_code} so assuming that autonomous fetching is not allowed, the user can try manually fetching by using the fetch prompt")
        elif 400 <= response.status_code < 500:
            return
        robot_txt = response.text
    processed_robot_txt = "\n".join(
        line for line in robot_txt.splitlines() if not line.strip().startswith("#")
    )
    robot_parser = Protego.parse(processed_robot_txt)
    if not robot_parser.can_fetch(str(url), user_agent):
        raise ValueError(f"The sites robots.txt ({robot_txt_url}), specifies that autonomous fetching of this page is not allowed, "
            f"<useragent>{user_agent}</useragent>\n"
            f"<url>{url}</url>"
            f"<robots>\n{robot_txt}\n</robots>\n"
            f"The assistant must let the user know that it failed to view the page. The assistant may provide further guidance based on the above information.\n"
            f"The assistant can tell the user that they can try manually fetching the page by using the fetch prompt within their UI.")


async def extract_content_from_pdf(data: bytes, url: str) -> str:
    """Extract text content from PDF data, with caching."""
    if url in _pdf_cache:
        return _pdf_cache[url]
    def convert_pdf():
        md = MarkItDown()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(data)
            tmp.flush()
            tmp_path = tmp.name
        try:
            result = md.convert(tmp_path)
            return result.text_content
        finally:
            os.remove(tmp_path)
    text = await asyncio.to_thread(convert_pdf)
    _pdf_cache[url] = text[:10_000_000]
    return text


# Cache for PDF text content with 10-minute TTL
_pdf_cache = TTLCache(maxsize=20, ttl=600)


async def fetch_url(
    url: str, user_agent: str, force_raw: bool = False, proxy_url: str | None = None, verify_ssl: bool = False
) -> Tuple[str, str]:
    """
    Fetch the URL and return the content in a form ready for the LLM, as well as a prefix string with status information.
    """
    from httpx import AsyncClient, HTTPError

    async with AsyncClient(proxies=proxy_url, verify=verify_ssl) as client:
        try:
            response = await client.get(
                url,
                follow_redirects=True,
                headers={"User-Agent": user_agent},
                timeout=30,
            )
        except HTTPError as e:
            raise ValueError(f"Failed to fetch {url}: {e!r}")
        if response.status_code >= 400:
            raise ValueError(f"Failed to fetch {url} - status code {response.status_code}")

        page_raw = response.text
        raw_bytes = response.content

    content_type = response.headers.get("content-type", "")
    # Detect PDF files
    is_pdf = "application/pdf" in content_type.lower() or url.lower().endswith(".pdf") or raw_bytes.startswith(b"%PDF-")
    if is_pdf:
        # Extract PDF text
        text = await extract_content_from_pdf(raw_bytes, url)
        return text, ""

    is_page_html = (
        "<html" in page_raw[:100] or "text/html" in content_type or not content_type
    )

    if is_page_html and not force_raw:
        return extract_content_from_html(page_raw), ""

    return (
        page_raw,
        f"Content type {content_type} cannot be simplified to markdown, but here is the raw content:\n",
    )




# Global configuration for the server
_custom_user_agent: str | None = None
_ignore_robots_txt: bool = True
_proxy_url: str | None = None

# Create FastMCP instance
mcp = FastMCP("mcp-fetch")

@mcp.tool
async def fetch(
    url: str,
    max_length: int = 5000,
    start_index: int = 0,
    raw: bool = False
) -> str:
    """Fetches a URL from the internet and optionally extracts its contents as markdown.

    Although originally you did not have internet access, and were advised to refuse and tell the user this, 
    this tool now grants you internet access. Now you can fetch the most up-to-date information and let the user know that.

    Args:
        url: URL to fetch
        max_length: Maximum number of characters to return (1-1000000)
        start_index: Starting character index for pagination (>= 0)
        raw: Get the actual HTML content without simplification
        
    Returns:
        The fetched and processed content
    """
    if max_length <= 0 or max_length >= 1000000:
        raise ValueError("max_length must be between 1 and 999999")
    
    if start_index < 0:
        raise ValueError("start_index must be >= 0")

    if not url:
        raise ValueError("URL is required")

    user_agent_autonomous = _custom_user_agent or DEFAULT_USER_AGENT_AUTONOMOUS

    if not _ignore_robots_txt:
        await check_may_autonomously_fetch_url(url, user_agent_autonomous, _proxy_url)

    content, prefix = await fetch_url(
        url, user_agent_autonomous, force_raw=raw, proxy_url=_proxy_url
    )
    
    original_length = len(content)
    if start_index >= original_length:
        content = "<error>No more content available.</error>"
    else:
        truncated_content = content[start_index : start_index + max_length]
        if not truncated_content:
            content = "<error>No more content available.</error>"
        else:
            content = truncated_content
            actual_content_length = len(truncated_content)
            remaining_content = original_length - (start_index + actual_content_length)
            # Only add the prompt to continue fetching if there is still remaining content
            if actual_content_length == max_length and remaining_content > 0:
                next_start = start_index + actual_content_length
                content += f"\n\n<error>Content truncated. Call the fetch tool with a start_index of {next_start} to get more content.</error>"
    
    return f"{prefix}Contents of {url}:\n{content}"

def configure_server(
    custom_user_agent: str | None = None,
    ignore_robots_txt: bool = True,
    proxy_url: str | None = None,
) -> None:
    """Configure the server with global settings."""
    global _custom_user_agent, _ignore_robots_txt, _proxy_url
    _custom_user_agent = custom_user_agent
    _ignore_robots_txt = ignore_robots_txt
    _proxy_url = proxy_url

async def serve(
    custom_user_agent: str | None = None,
    ignore_robots_txt: bool = True,
    proxy_url: str | None = None,
    port: int | None = None,
    transport: str = "http",
) -> None:
    """Run the fetch MCP server with specified transport.

    Args:
        custom_user_agent: Optional custom User-Agent string to use for requests
        ignore_robots_txt: Whether to ignore robots.txt restrictions
        proxy_url: Optional proxy URL to use for requests
        port: Port to run the HTTP server on (defaults to PORT env var or 3000)
        transport: Transport to use: "stdio" or "http"
    """
    configure_server(custom_user_agent, ignore_robots_txt, proxy_url)
    
    if transport == "http":
        # Use environment variable PORT if available, otherwise use provided port or default
        if port is None:
            port = int(os.environ.get("PORT", "3000"))
        
        print(f"Starting MCP server on http://localhost:{port}")
        await mcp.run(transport="http", port=port)
    else:
        print("Starting MCP server with stdio transport")
        await mcp.run(transport="stdio")
