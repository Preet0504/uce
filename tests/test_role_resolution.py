"""Tests for MCP server role-claim resolution.

Covers the fix that resolves a caller's role from either a custom flat ``role``
claim or the standard Keycloak locations (``realm_access.roles`` /
``resource_access.<client>.roles``), picking the highest-ranked known role.
"""
from uce.server.mcp_server import _highest_known_role


def test_highest_known_role_flat_string():
    assert _highest_known_role(["editor"]) == "editor"


def test_highest_known_role_picks_highest():
    assert _highest_known_role(["viewer", "admin", "editor"]) == "admin"
    assert _highest_known_role(["viewer", "editor"]) == "editor"


def test_highest_known_role_ignores_unknown_roles():
    # Keycloak realms include default roles like "offline_access"; those must be
    # ignored, and only the recognized UCE role should be returned.
    assert _highest_known_role(["offline_access", "uma_authorization", "viewer"]) == "viewer"


def test_highest_known_role_none_when_no_known_role():
    assert _highest_known_role(["offline_access", "default-roles-uce-realm"]) is None
    assert _highest_known_role([]) is None
