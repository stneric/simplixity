"""Simplixity: a self-hosted research assistant that answers with checked citations."""

# The one version the running app reports: in `/api/health`, the OpenAPI
# document and the User-Agent of every outbound request. `pyproject.toml`
# carries the same number for packaging, and `tests/test_version.py` fails when
# the two disagree.
__version__ = "0.3.0"
