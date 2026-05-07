#!/usr/bin/env python3
"""Bootstrap Keycloak realm, RBAC clients, and client secrets for UCE."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


CLIENT_ROLE_MAP: dict[str, str] = {
    "uce-viewer": "viewer",
    "uce-editor": "editor",
    "uce-admin": "admin",
}


@dataclass
class BootstrapConfig:
    base_url: str
    public_base_url: str
    admin_username: str
    admin_password: str
    realm: str
    audience: str
    access_token_lifespan: int
    output_env_path: str | None


def _normalize_base_url(value: str) -> str:
    return value.rstrip("/")


def _decode_body(raw: bytes) -> tuple[Any, str]:
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return None, text
    try:
        return json.loads(text), text
    except json.JSONDecodeError:
        return text, text


def _request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    json_body: dict[str, Any] | list[Any] | None = None,
    form_body: dict[str, str] | None = None,
    expected_statuses: tuple[int, ...] = (200, 201, 204),
    allowed_error_statuses: tuple[int, ...] = (),
) -> tuple[int, Any]:
    headers = {"Accept": "application/json"}
    data: bytes | None = None

    if token:
        headers["Authorization"] = f"Bearer {token}"

    if json_body is not None and form_body is not None:
        raise ValueError("Provide either json_body or form_body, not both.")

    if json_body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(json_body).encode("utf-8")
    elif form_body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(form_body).encode("utf-8")

    request = urllib.request.Request(url=url, method=method, headers=headers, data=data)

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.getcode()
            payload, _ = _decode_body(response.read())
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload, raw = _decode_body(exc.read())
        if status in allowed_error_statuses:
            return status, payload
        raise RuntimeError(f"HTTP {status} for {method} {url}: {raw}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Unable to reach {url}: {exc}") from exc

    if status not in expected_statuses:
        raise RuntimeError(f"Unexpected HTTP {status} for {method} {url}: {payload}")
    return status, payload


def _admin_token(config: BootstrapConfig) -> str:
    url = (
        f"{config.base_url}/realms/master/protocol/openid-connect/token"
    )
    _, payload = _request(
        "POST",
        url,
        form_body={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": config.admin_username,
            "password": config.admin_password,
        },
    )
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise RuntimeError("Admin token response missing access_token.")
    return str(payload["access_token"])


def _ensure_realm(config: BootstrapConfig, token: str) -> None:
    url = f"{config.base_url}/admin/realms/{config.realm}"
    status, payload = _request("GET", url, token=token, allowed_error_statuses=(404,))
    if status == 404:
        _request(
            "POST",
            f"{config.base_url}/admin/realms",
            token=token,
            json_body={
                "realm": config.realm,
                "enabled": True,
                "displayName": "Unified Context Engine",
                "accessTokenLifespan": config.access_token_lifespan,
            },
            expected_statuses=(201, 204),
        )
        _, payload = _request("GET", url, token=token)

    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected realm payload when loading {config.realm}: {payload}")

    existing_lifespan = payload.get("accessTokenLifespan")
    if existing_lifespan != config.access_token_lifespan:
        payload["accessTokenLifespan"] = config.access_token_lifespan
        _request(
            "PUT",
            url,
            token=token,
            json_body=payload,
            expected_statuses=(204,),
        )


def _ensure_realm_role(config: BootstrapConfig, token: str, role_name: str) -> None:
    role_url = f"{config.base_url}/admin/realms/{config.realm}/roles/{role_name}"
    status, _ = _request("GET", role_url, token=token, allowed_error_statuses=(404,))
    if status == 404:
        _request(
            "POST",
            f"{config.base_url}/admin/realms/{config.realm}/roles",
            token=token,
            json_body={"name": role_name, "description": f"UCE RBAC role: {role_name}"},
            expected_statuses=(201, 204),
        )


def _find_client_uuid(config: BootstrapConfig, token: str, client_id: str) -> str | None:
    url = (
        f"{config.base_url}/admin/realms/{config.realm}/clients"
        f"?clientId={urllib.parse.quote(client_id)}"
    )
    _, payload = _request("GET", url, token=token)
    if not isinstance(payload, list):
        return None
    for item in payload:
        if isinstance(item, dict) and item.get("clientId") == client_id and item.get("id"):
            return str(item["id"])
    return None


def _ensure_client(config: BootstrapConfig, token: str, client_id: str) -> str:
    client_uuid = _find_client_uuid(config, token, client_id)

    if client_uuid is None:
        _request(
            "POST",
            f"{config.base_url}/admin/realms/{config.realm}/clients",
            token=token,
            json_body={
                "clientId": client_id,
                "name": client_id,
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": False,
                "serviceAccountsEnabled": True,
                "standardFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "fullScopeAllowed": True,
            },
            expected_statuses=(201, 204),
        )
        client_uuid = _find_client_uuid(config, token, client_id)

    if not client_uuid:
        raise RuntimeError(f"Unable to locate client UUID for {client_id}.")

    # Ensure core settings stay aligned even if client already existed.
    _request(
        "PUT",
        f"{config.base_url}/admin/realms/{config.realm}/clients/{client_uuid}",
        token=token,
        json_body={
            "id": client_uuid,
            "clientId": client_id,
            "name": client_id,
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": False,
            "serviceAccountsEnabled": True,
            "standardFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "fullScopeAllowed": True,
        },
        expected_statuses=(204,),
    )

    return client_uuid


def _ensure_protocol_mapper(
    config: BootstrapConfig,
    token: str,
    client_uuid: str,
    mapper_name: str,
    mapper_payload: dict[str, Any],
) -> None:
    list_url = (
        f"{config.base_url}/admin/realms/{config.realm}/clients/{client_uuid}/protocol-mappers/models"
    )
    _, payload = _request("GET", list_url, token=token)

    existing_id: str | None = None
    if isinstance(payload, list):
        for row in payload:
            if isinstance(row, dict) and row.get("name") == mapper_name and row.get("id"):
                existing_id = str(row["id"])
                break

    if existing_id:
        _request(
            "PUT",
            (
                f"{config.base_url}/admin/realms/{config.realm}/clients/{client_uuid}"
                f"/protocol-mappers/models/{existing_id}"
            ),
            token=token,
            json_body={"id": existing_id, "name": mapper_name, **mapper_payload},
            expected_statuses=(204,),
        )
    else:
        _request(
            "POST",
            list_url,
            token=token,
            json_body={"name": mapper_name, **mapper_payload},
            expected_statuses=(201, 204),
        )


def _rotate_client_secret(config: BootstrapConfig, token: str, client_uuid: str) -> str:
    url = f"{config.base_url}/admin/realms/{config.realm}/clients/{client_uuid}/client-secret"
    _, payload = _request("POST", url, token=token)
    if not isinstance(payload, dict) or not payload.get("value"):
        raise RuntimeError("Client secret response missing value.")
    return str(payload["value"])


def _ensure_uce_mcp_client(config: BootstrapConfig, token: str) -> None:
    client_uuid = _find_client_uuid(config, token, config.audience)
    if client_uuid:
        return

    _request(
        "POST",
        f"{config.base_url}/admin/realms/{config.realm}/clients",
        token=token,
        json_body={
            "clientId": config.audience,
            "name": config.audience,
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": True,
            "serviceAccountsEnabled": False,
            "standardFlowEnabled": False,
            "directAccessGrantsEnabled": False,
        },
        expected_statuses=(201, 204),
    )


def _bootstrap_clients(config: BootstrapConfig, token: str) -> dict[str, str]:
    secrets: dict[str, str] = {}

    _ensure_uce_mcp_client(config, token)

    for client_id, role_name in CLIENT_ROLE_MAP.items():
        client_uuid = _ensure_client(config, token, client_id)

        _ensure_protocol_mapper(
            config,
            token,
            client_uuid,
            "uce-role-claim",
            {
                "protocol": "openid-connect",
                "protocolMapper": "oidc-hardcoded-claim-mapper",
                "consentRequired": False,
                "config": {
                    "claim.name": "role",
                    "claim.value": role_name,
                    "jsonType.label": "String",
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                    "userinfo.token.claim": "false",
                },
            },
        )

        _ensure_protocol_mapper(
            config,
            token,
            client_uuid,
            "uce-audience-claim",
            {
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "consentRequired": False,
                "config": {
                    "included.custom.audience": config.audience,
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                    "userinfo.token.claim": "false",
                },
            },
        )

        secrets[client_id] = _rotate_client_secret(config, token, client_uuid)

    return secrets


def _output_summary(config: BootstrapConfig, secrets: dict[str, str]) -> str:
    issuer = f"{config.public_base_url}/realms/{config.realm}"
    jwks = f"{config.public_base_url}/realms/{config.realm}/protocol/openid-connect/certs"
    token_endpoint = f"{config.public_base_url}/realms/{config.realm}/protocol/openid-connect/token"

    lines: list[str] = []
    lines.append("Keycloak bootstrap completed successfully.")
    lines.append("")
    lines.append("UCE auth settings:")
    lines.append(f"RBAC_JWT_ISSUER={issuer}")
    lines.append(f"RBAC_JWT_AUDIENCE={config.audience}")
    lines.append(f"RBAC_JWKS_URI={jwks}")
    lines.append(f"KEYCLOAK_ACCESS_TOKEN_LIFESPAN_SECONDS={config.access_token_lifespan}")
    lines.append("")

    for client_id, secret in secrets.items():
        env_key = client_id.upper().replace("-", "_")
        lines.append(f"{env_key}_CLIENT_ID={client_id}")
        lines.append(f"{env_key}_CLIENT_SECRET={secret}")
        lines.append(
            "TOKEN_COMMAND_{0}=curl -s -X POST '{1}' -H 'Content-Type: application/x-www-form-urlencoded' "
            "-d 'grant_type=client_credentials&client_id={2}&client_secret={3}'".format(
                env_key,
                token_endpoint,
                client_id,
                secret,
            )
        )
        lines.append("")

    lines.append("Goose extension target URL (same for all roles):")
    lines.append("http://127.0.0.1:9001/mcp/")
    lines.append("Use header: Authorization: Bearer <role_token>")

    output = "\n".join(lines).strip() + "\n"
    print(output)
    return output


def _parse_args() -> BootstrapConfig:
    parser = argparse.ArgumentParser(
        description="Bootstrap Keycloak realm, RBAC clients, mappers, and regenerated secrets for UCE demos.",
    )
    parser.add_argument("--base-url", default="http://localhost:8080", help="Keycloak admin base URL")
    parser.add_argument(
        "--public-base-url",
        default=None,
        help="Public URL used in printed issuer/JWKS/token endpoints (defaults to --base-url)",
    )
    parser.add_argument("--admin-username", default="admin", help="Keycloak admin username")
    parser.add_argument("--admin-password", default="admin", help="Keycloak admin password")
    parser.add_argument("--realm", default="uce-realm", help="Realm to bootstrap")
    parser.add_argument("--audience", default="uce-mcp", help="Audience claim required by UCE")
    parser.add_argument(
        "--access-token-lifespan-seconds",
        type=int,
        default=3600,
        help="Realm access token TTL in seconds (default: 3600 for local/demo ergonomics)",
    )
    parser.add_argument(
        "--output-env-file",
        default=None,
        help="Optional output file for generated secrets and env snippets",
    )

    args = parser.parse_args()

    base_url = _normalize_base_url(str(args.base_url))
    public_base_url = _normalize_base_url(str(args.public_base_url or args.base_url))

    return BootstrapConfig(
        base_url=base_url,
        public_base_url=public_base_url,
        admin_username=str(args.admin_username),
        admin_password=str(args.admin_password),
        realm=str(args.realm),
        audience=str(args.audience),
        access_token_lifespan=int(args.access_token_lifespan_seconds),
        output_env_path=str(args.output_env_file) if args.output_env_file else None,
    )


def main() -> int:
    config = _parse_args()

    try:
        token = _admin_token(config)
        _ensure_realm(config, token)
        for role in ("viewer", "editor", "admin"):
            _ensure_realm_role(config, token, role)

        secrets = _bootstrap_clients(config, token)
        output = _output_summary(config, secrets)

        if config.output_env_path:
            with open(config.output_env_path, "w", encoding="utf-8") as handle:
                handle.write(output)
            print(f"Wrote bootstrap output to {config.output_env_path}")

        return 0
    except Exception as exc:  # pragma: no cover - defensive CLI wrapper
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
