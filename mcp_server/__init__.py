"""Radio Gateway MCP server package.

Split out of the single 3175-LOC gateway_mcp.py so each tool category lives
in its own module. The legacy entry point ``gateway_mcp.py`` still exists and
just runs ``mcp_server.run()`` — `.mcp.json` and any external scripts keep
working unchanged.

Tool registration relies on import side effects: importing each tool module
runs its ``@mcp.tool()`` decorators against the shared ``mcp`` instance from
``mcp_server.server``. ``run()`` triggers that import chain.
"""

from mcp_server.server import mcp


def _register_all_tools():
    # Importing each module registers its tools via @mcp.tool() side effects.
    # Do not remove unused imports — that's how registration happens.
    from mcp_server.tools import (  # noqa: F401
        control,
        radios,
        routing,
        fleet,
        transcription,
        link,
        loop_recorder,
        cloud,
        repeaters,
        metrics,
        usrp,
        manager,
        audio_beds,
    )


def run():
    _register_all_tools()
    mcp.run(transport='stdio')
