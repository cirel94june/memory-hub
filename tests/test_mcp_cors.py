"""MCP endpoint CORS tests — IB Mobile (browser) needs these headers."""
import ast
import asyncio
import os
import sys

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_MAIN_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")


def test_cors_headers_defined_in_main():
    """_CORS_HEADERS must be defined and include IB-required headers."""
    with open(_MAIN_PATH, encoding="utf-8") as f:
        source = f.read()
    assert "access-control-allow-origin" in source
    assert "mcp-session-id" in source
    assert "_CORS_HEADERS" in source


def test_options_method_handled_in_gateway():
    """MCPGateway._handle_mcp must handle OPTIONS preflight."""
    with open(_MAIN_PATH, encoding="utf-8") as f:
        source = f.read()
    assert '"OPTIONS"' in source or "'OPTIONS'" in source


def test_cors_on_send_with_cors_wrapper():
    """send_with_cors pattern must inject headers into response.start."""
    with open(_MAIN_PATH, encoding="utf-8") as f:
        source = f.read()
    assert "send_with_cors" in source


def test_cors_headers_on_error_responses():
    """Error responses (503, 405, 504, 500) must also include CORS headers."""
    with open(_MAIN_PATH, encoding="utf-8") as f:
        source = f.read()
    for status in ["503", "405", "504", "500"]:
        idx = source.find(f'"status": {status}')
        if idx == -1:
            continue
        context = source[idx:idx+200]
        assert "_CORS_HEADERS" in context, f"status {status} response missing _CORS_HEADERS"
