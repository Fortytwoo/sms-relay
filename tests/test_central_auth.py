from __future__ import annotations

import json
import unittest
import urllib.parse

from central_auth import (
    CentralAuthRejected,
    CentralOAuthClient,
    validate_introspection,
    validate_token_response,
)


class FakeResponse:
    def __init__(self, payload) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode()


class QueueOpener:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, **_kwargs):
        self.requests.append(request)
        return FakeResponse(self.responses.pop(0))


class CentralAuthContractTests(unittest.TestCase):
    def test_backchannel_ip_rewrites_transport_without_changing_public_oauth_urls(self) -> None:
        metadata = {
            "issuer": "https://auth.example.test",
            "authorization_endpoint": "https://auth.example.test/oauth/authorize",
            "token_endpoint": "https://auth.example.test/oauth/token",
            "revocation_endpoint": "https://auth.example.test/oauth/revoke",
            "introspection_endpoint": "https://auth.example.test/auth/introspect",
            "code_challenge_methods_supported": ["S256"],
        }
        token = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "sms-relay:access",
        }
        opener = QueueOpener(metadata, token)
        client = CentralOAuthClient(
            issuer="https://auth.example.test",
            client_id="sms-relay-web",
            audience="sms-relay-api",
            scopes=("sms-relay:access",),
            redirect_uri="https://relay.example.test/sms-relay/auth/callback",
            post_logout_redirect_uri="https://relay.example.test/sms-relay/?auto_sso=off",
            backchannel_ip="139.196.114.210",
            opener=opener,
        )

        authorization_url = client.authorization_url("state", "A" * 43)
        client.exchange_code("authorization-code", "pkce-verifier")

        self.assertEqual(
            urllib.parse.urlsplit(authorization_url).netloc,
            "auth.example.test",
        )
        self.assertEqual(
            opener.requests[0].full_url,
            "https://139.196.114.210/.well-known/oauth-authorization-server",
        )
        self.assertEqual(opener.requests[0].get_header("Host"), "auth.example.test")
        self.assertEqual(
            opener.requests[1].full_url,
            "https://139.196.114.210/oauth/token",
        )
        self.assertEqual(opener.requests[1].get_header("Host"), "auth.example.test")

    def test_backchannel_ip_must_be_a_literal_ip_address(self) -> None:
        with self.assertRaisesRegex(ValueError, "AUTH_BACKCHANNEL_IP"):
            CentralOAuthClient(
                issuer="https://auth.example.test",
                client_id="sms-relay-web",
                audience="sms-relay-api",
                scopes=("sms-relay:access",),
                redirect_uri="https://relay.example.test/sms-relay/auth/callback",
                post_logout_redirect_uri="https://relay.example.test/sms-relay/?auto_sso=off",
                backchannel_ip="proxy.example.test",
            )

    def test_metadata_uses_documented_same_issuer_introspection_fallback(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "issuer": "https://auth.example.test",
                        "authorization_endpoint": "https://auth.example.test/oauth/authorize",
                        "token_endpoint": "https://auth.example.test/oauth/token",
                        "revocation_endpoint": "https://auth.example.test/oauth/revoke",
                        "code_challenge_methods_supported": ["S256"],
                    }
                ).encode()

        client = CentralOAuthClient(
            issuer="https://auth.example.test",
            client_id="sms-relay-web",
            audience="sms-relay-api",
            scopes=("sms-relay:access",),
            redirect_uri="https://relay.example.test/sms-relay/auth/callback",
            post_logout_redirect_uri="https://relay.example.test/sms-relay/?auto_sso=off",
            opener=lambda *_args, **_kwargs: Response(),
        )

        self.assertEqual(
            client.metadata()["introspection_endpoint"],
            "https://auth.example.test/auth/introspect",
        )

    def test_backchannel_requests_use_public_pkce_client_and_fixed_introspection(self) -> None:
        metadata = {
            "issuer": "https://auth.example.test",
            "authorization_endpoint": "https://auth.example.test/oauth/authorize",
            "token_endpoint": "https://auth.example.test/oauth/token",
            "revocation_endpoint": "https://auth.example.test/oauth/revoke",
            "introspection_endpoint": "https://auth.example.test/auth/introspect",
            "code_challenge_methods_supported": ["S256"],
        }
        token = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "sms-relay:access",
        }
        opener = QueueOpener(metadata, token, {"active": False}, b"")
        client = CentralOAuthClient(
            issuer="https://auth.example.test",
            client_id="sms-relay-web",
            audience="sms-relay-api",
            scopes=("sms-relay:access",),
            redirect_uri="https://relay.example.test/sms-relay/auth/callback",
            post_logout_redirect_uri="https://relay.example.test/sms-relay/?auto_sso=off",
            opener=opener,
        )

        client.exchange_code("authorization-code", "pkce-verifier")
        client.introspect("access-token")
        client.revoke("refresh-token")

        token_form = urllib.parse.parse_qs(opener.requests[1].data.decode())
        self.assertEqual(token_form["client_id"], ["sms-relay-web"])
        self.assertEqual(token_form["code_verifier"], ["pkce-verifier"])
        self.assertNotIn("client_secret", token_form)
        self.assertEqual(
            json.loads(opener.requests[2].data),
            {
                "issuer": "https://auth.example.test",
                "audience": "sms-relay-api",
            },
        )
        self.assertEqual(
            opener.requests[2].get_header("Authorization"),
            "Bearer access-token",
        )
        revoke_form = urllib.parse.parse_qs(opener.requests[3].data.decode())
        self.assertEqual(revoke_form["token_type_hint"], ["refresh_token"])
        self.assertNotIn("client_secret", revoke_form)

    def test_token_response_requires_bearer_rotating_refresh_and_exact_scope(self) -> None:
        valid = validate_token_response(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "sms-relay:access",
            },
            {"sms-relay:access"},
        )
        self.assertEqual(valid["expires_in"], 3600)
        self.assertEqual(
            validate_token_response(valid, {"sms-relay:access"})["scopes"],
            ["sms-relay:access"],
        )

        invalid_responses = [
            {"refresh_token": "refresh", "token_type": "Bearer", "expires_in": 3600},
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "token_type": "mac",
                "expires_in": 3600,
                "scope": "sms-relay:access",
            },
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "other:scope",
            },
        ]
        for response in invalid_responses:
            with self.subTest(response=response), self.assertRaises(CentralAuthRejected):
                validate_token_response(response, {"sms-relay:access"})

    def test_introspection_returns_stable_subject_and_rejects_wrong_contract(self) -> None:
        principal = validate_introspection(
            {
                "active": True,
                "principal": {
                    "id": "fallback-id",
                    "unionId": "union-id",
                    "openId": "ou_test",
                    "clientId": "sms-relay-web",
                    "scopes": ["sms-relay:access"],
                    "name": "测试用户",
                },
            },
            client_id="sms-relay-web",
            required_scopes={"sms-relay:access"},
        )
        self.assertEqual(principal["subject"], "union-id")
        self.assertEqual(principal["open_id"], "ou_test")

        for response in (
            {"active": False},
            {"active": True, "principal": {"clientId": "sms-relay-web", "scopes": []}},
            {
                "active": True,
                "principal": {
                    "id": "id",
                    "clientId": "wrong-client",
                    "scopes": ["sms-relay:access"],
                },
            },
        ):
            with self.subTest(response=response), self.assertRaises(CentralAuthRejected):
                validate_introspection(
                    response,
                    client_id="sms-relay-web",
                    required_scopes={"sms-relay:access"},
                )


if __name__ == "__main__":
    unittest.main()
