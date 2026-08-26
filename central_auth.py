from __future__ import annotations

import json
import ipaddress
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


class CentralAuthError(RuntimeError):
    """Base error that never contains credentials or OAuth material."""


class CentralAuthUnavailable(CentralAuthError):
    """The authorization server could not make a reliable decision."""


class CentralAuthRejected(CentralAuthError):
    """The authorization server response violates the client contract."""

    def __init__(self, reason: str, *, error_code: str = ""):
        super().__init__(reason)
        self.error_code = error_code


class CentralAuthInvalidGrant(CentralAuthRejected):
    def __init__(self) -> None:
        super().__init__("invalid_grant", error_code="invalid_grant")


def _string_set(value: Any) -> set[str]:
    if isinstance(value, str):
        return {item for item in value.split() if item}
    if isinstance(value, list):
        return {str(item) for item in value if str(item)}
    return set()


def validate_token_response(
    response: Any,
    required_scopes: set[str],
) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise CentralAuthRejected("invalid_token_response")
    access_token = response.get("access_token")
    refresh_token = response.get("refresh_token")
    token_type = response.get("token_type")
    expires_in = response.get("expires_in")
    if not isinstance(access_token, str) or not access_token:
        raise CentralAuthRejected("missing_access_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise CentralAuthRejected("missing_refresh_token")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise CentralAuthRejected("invalid_token_type")
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
        raise CentralAuthRejected("invalid_token_expiry")
    expires_in = int(expires_in)
    if expires_in <= 0:
        raise CentralAuthRejected("invalid_token_expiry")
    scope_value = response.get("scope") if "scope" in response else response.get("scopes")
    granted_scopes = _string_set(scope_value)
    if not required_scopes.issubset(granted_scopes):
        raise CentralAuthRejected("missing_required_scope")
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "scopes": sorted(granted_scopes),
    }


def validate_introspection(
    response: Any,
    *,
    client_id: str,
    required_scopes: set[str],
) -> dict[str, Any]:
    if not isinstance(response, dict) or response.get("active") is not True:
        raise CentralAuthRejected("inactive_token")
    principal = response.get("principal")
    if not isinstance(principal, dict):
        raise CentralAuthRejected("invalid_principal")
    if principal.get("clientId") != client_id:
        raise CentralAuthRejected("wrong_client")
    scopes = _string_set(principal.get("scopes"))
    if not required_scopes.issubset(scopes):
        raise CentralAuthRejected("missing_required_scope")
    subject = str(principal.get("unionId") or principal.get("id") or "")
    if not subject:
        raise CentralAuthRejected("missing_stable_subject")
    return {
        "subject": subject,
        "principal_id": str(principal.get("principalId") or ""),
        "union_id": str(principal.get("unionId") or ""),
        "open_id": str(principal.get("openId") or ""),
        "name": str(principal.get("name") or "统一认证用户")[:128],
        "avatar": str(principal.get("avatar") or "")[:2048],
        "scopes": sorted(scopes),
    }


class CentralOAuthClient:
    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        audience: str,
        scopes: tuple[str, ...],
        redirect_uri: str,
        post_logout_redirect_uri: str,
        backchannel_ip: str = "",
        timeout: float = 10.0,
        metadata_ttl_seconds: int = 300,
        opener: Callable[..., Any] = urlopen,
    ):
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.audience = audience
        self.scopes = tuple(dict.fromkeys(scopes))
        self.redirect_uri = redirect_uri
        self.post_logout_redirect_uri = post_logout_redirect_uri
        self.backchannel_ip = backchannel_ip.strip()
        self.timeout = timeout
        self.metadata_ttl_seconds = max(metadata_ttl_seconds, 1)
        self._opener = opener
        self._metadata: dict[str, Any] | None = None
        self._metadata_expires_at = 0.0
        self._metadata_lock = threading.Lock()
        self._validate_configuration()

    def _validate_configuration(self) -> None:
        issuer = urlsplit(self.issuer)
        if (
            issuer.scheme != "https"
            or not issuer.netloc
            or issuer.username
            or issuer.password
            or issuer.query
            or issuer.fragment
        ):
            raise ValueError("AUTH_ISSUER must be an HTTPS URL")
        if not self.client_id or not self.audience or not self.scopes:
            raise ValueError("central OAuth client configuration is incomplete")
        if self.backchannel_ip:
            try:
                self.backchannel_ip = str(ipaddress.ip_address(self.backchannel_ip))
            except ValueError as exc:
                raise ValueError(
                    "AUTH_BACKCHANNEL_IP must be a literal IP address"
                ) from exc
        for name, value in (
            ("AUTH_REDIRECT_URI", self.redirect_uri),
            ("AUTH_POST_LOGOUT_REDIRECT_URI", self.post_logout_redirect_uri),
        ):
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.fragment
                or (name == "AUTH_REDIRECT_URI" and parsed.query)
            ):
                raise ValueError(f"{name} must be an HTTPS URL without a fragment")

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        form: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expect_json: bool = True,
    ) -> dict[str, Any]:
        transport_url, transport_host = self._transport_url(url)
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "sms-relay-central-oauth/1.0",
            **(headers or {}),
        }
        if transport_host:
            request_headers["Host"] = transport_host
        body = None
        if form is not None:
            body = urlencode(form).encode("ascii")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(
            transport_url,
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            error_code = ""
            try:
                error_payload = json.loads(exc.read(4096).decode("utf-8"))
                if isinstance(error_payload, dict):
                    error_code = str(error_payload.get("error") or "")
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            if error_code == "invalid_grant":
                raise CentralAuthInvalidGrant() from exc
            if exc.code >= 500 or exc.code in {408, 429}:
                raise CentralAuthUnavailable("authorization_server_unavailable") from exc
            raise CentralAuthRejected(
                "authorization_server_rejected_request",
                error_code=error_code,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise CentralAuthUnavailable("authorization_server_unavailable") from exc
        if not expect_json and not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CentralAuthUnavailable("authorization_server_invalid_response") from exc
        if not isinstance(parsed, dict):
            raise CentralAuthUnavailable("authorization_server_invalid_response")
        return parsed

    def _transport_url(self, url: str) -> tuple[str, str]:
        if not self.backchannel_ip:
            return url, ""
        issuer = urlsplit(self.issuer)
        endpoint = urlsplit(url)
        if endpoint.scheme != issuer.scheme or endpoint.netloc != issuer.netloc:
            raise CentralAuthRejected("invalid_backchannel_endpoint")
        address = self.backchannel_ip
        if ":" in address:
            address = f"[{address}]"
        if issuer.port and issuer.port != 443:
            address = f"{address}:{issuer.port}"
        return (
            urlunsplit(
                (
                    endpoint.scheme,
                    address,
                    endpoint.path,
                    endpoint.query,
                    "",
                )
            ),
            issuer.netloc,
        )

    def metadata(self) -> dict[str, Any]:
        now = time.time()
        with self._metadata_lock:
            if self._metadata is not None and now < self._metadata_expires_at:
                return dict(self._metadata)
            metadata = self._request(
                self.issuer + "/.well-known/oauth-authorization-server"
            )
            if metadata.get("issuer") != self.issuer:
                raise CentralAuthRejected("metadata_issuer_mismatch")
            required = (
                "authorization_endpoint",
                "token_endpoint",
                "revocation_endpoint",
            )
            metadata.setdefault(
                "introspection_endpoint", self.issuer + "/auth/introspect"
            )
            issuer_origin = urlsplit(self.issuer)[:2]
            for name in (*required, "introspection_endpoint"):
                endpoint = metadata.get(name)
                parsed = urlsplit(str(endpoint or ""))
                if parsed.scheme != "https" or parsed[:2] != issuer_origin:
                    raise CentralAuthRejected("invalid_metadata_endpoint")
            methods = metadata.get("code_challenge_methods_supported")
            if not isinstance(methods, list) or "S256" not in methods:
                raise CentralAuthRejected("pkce_s256_not_supported")
            self._metadata = metadata
            self._metadata_expires_at = now + self.metadata_ttl_seconds
            return dict(metadata)

    def authorization_url(self, state: str, code_challenge: str) -> str:
        endpoint = str(self.metadata()["authorization_endpoint"])
        return endpoint + "?" + urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": " ".join(self.scopes),
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )

    def exchange_code(self, code: str, verifier: str) -> dict[str, Any]:
        response = self._request(
            str(self.metadata()["token_endpoint"]),
            method="POST",
            form={
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "code": code,
                "code_verifier": verifier,
            },
        )
        return validate_token_response(response, set(self.scopes))

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        response = self._request(
            str(self.metadata()["token_endpoint"]),
            method="POST",
            form={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": refresh_token,
            },
        )
        return validate_token_response(response, set(self.scopes))

    def introspect(self, access_token: str) -> dict[str, Any]:
        return self._request(
            str(self.metadata()["introspection_endpoint"]),
            method="POST",
            payload={"issuer": self.issuer, "audience": self.audience},
            headers={"Authorization": f"Bearer {access_token}"},
        )

    def revoke(self, refresh_token: str) -> None:
        self._request(
            str(self.metadata()["revocation_endpoint"]),
            method="POST",
            form={
                "client_id": self.client_id,
                "token": refresh_token,
                "token_type_hint": "refresh_token",
            },
            expect_json=False,
        )
