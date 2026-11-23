"""Entry point of the downloadable TeXLoom app (PyInstaller).

`TeXLoom mcp …` (the binary inside the app) runs the MCP server instead of the app, so that people
with the app can connect an AI tool without installing the command-line helper as well.
"""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if sys.argv[1:2] == ["mcp"]:
        from texloom.cli import main as cli
    else:
        from texloom.app import main as cli
    cli()
