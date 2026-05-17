"""v1.6.7: Bundled domain skills.

Each subpackage is one skill: ``skill.md`` (parsed metadata + docs) +
``__init__.py`` exposing ``async def run(agent, url, inputs) -> dict``.

Adding a new bundled skill:
  1. Create ``builtin_skills/<name>/skill.md`` with frontmatter.
  2. Create ``builtin_skills/<name>/__init__.py`` with an async ``run``.
  3. Append the module to ``BUILTIN_SKILLS`` below.

The registry imports each module, parses ``skill.md``, and overrides
``runnable=True`` regardless of the markdown flag (bundled skills are
always dispatchable -- the markdown flag is informational so the same
file can be hand-copied into a user workspace).
"""

from __future__ import annotations

from . import ec_europa_document_search, github_release_download, sec_gov_filing_search

# Module-level list of registered skill modules. Order is not
# significant for dispatch (the registry keys by (domain, name)).
BUILTIN_SKILLS = [
    sec_gov_filing_search,
    github_release_download,
    ec_europa_document_search,
]

__all__ = ["BUILTIN_SKILLS"]
