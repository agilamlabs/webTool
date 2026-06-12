"""Allow running as: python -m web_agent <command>"""

from .main import main

# v1.6.16 deep-review fix: guard the CLI dispatch. Without it, ANY plain import
# of ``web_agent.__main__`` (pydoc / sphinx autodoc, pkgutil.walk_packages,
# pytest --doctest-modules collection, IDE indexers) immediately ran argparse
# against the host process's sys.argv -- usually raising SystemExit(2), or in
# the worst case dispatching a real subcommand. ``python -m web_agent`` still
# sets __name__ == "__main__", so the CLI entrypoint behaviour is unchanged.
if __name__ == "__main__":
    main()
