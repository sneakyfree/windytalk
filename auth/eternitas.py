"""Auth + entitlement gating (ADR-058 D6 / §10).

Two responsibilities, one seam:
  1. Entitlement gate — decide whether a connecting client may use Windy Talk,
     via an `Authorizer`. The engine calls this on `hello`; a deny becomes a
     fatal `not_entitled` error (voice-session.v1 §9).
  2. Brokered short-lived tokens (§10) — the client never holds a long-lived
     Eternitas passport; it fetches ≤5-min scoped EPTs per action class from the
     CredentialIssuer. `broker_token()` is that call.

REALITY (probed 2026-07-09): Eternitas is live (api.eternitas.ai, all endpoints
401/auth-gated). Passports are `ET26-XXXX-XXXX`; the CredentialIssuer mints ES256
EPTs (the §10 short-lived tokens). What does NOT exist yet — and is Grant's to
define before strict gating can go live — is the **windy-talk entitlement SKU**
(in windy-pro/Eternitas) and its **EPT action scope**. Until then:
  - Default `DevAuthorizer` allows all (keeps the wedge + audio E2E working on
    the Task-0.0 dev Mind key).
  - `EternitasAuthorizer` (WINDYTALK_STRICT_AUTH=1) verifies a passport/EPT and
    checks the windy-talk entitlement — and correctly DENIES until that SKU is
    defined (forced-honest: it never fakes an entitlement).
No passport or long-lived token is ever written to disk by this module (§10).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

ETERNITAS_BASE = os.environ.get("WINDYTALK_ETERNITAS_URL", "https://api.eternitas.ai")
ENTITLEMENT_SKU = "windy-talk"


@dataclass
class Entitlement:
    entitled: bool
    user_id: str
    tier: str = "none"
    reason: str = ""


class Authorizer:
    """Decides whether a client may connect. `auth` is the hello `auth` object
    (voice-session.v1 §5): {scheme, token} — token is a passport or an EPT."""

    def authorize(self, auth: dict | None) -> Entitlement:  # pragma: no cover
        raise NotImplementedError


class DevAuthorizer(Authorizer):
    """Dev/audio-test mode: allow everyone. The gate is inert; the dev Mind key
    carries the brain. This is the default until the windy-talk entitlement and
    the dev-key scrub (both Grant-gated) land."""

    def authorize(self, auth: dict | None) -> Entitlement:
        user_id = (auth or {}).get("user_id") or (auth or {}).get("token") or "dev"
        return Entitlement(entitled=True, user_id=str(user_id)[:64], tier="dev",
                           reason="dev mode (gate inert)")


class EternitasAuthorizer(Authorizer):
    """Strict mode: verify the passport/EPT against Eternitas and require the
    windy-talk entitlement. Forced-honest — denies until the SKU exists."""

    def __init__(self, base_url: str = ETERNITAS_BASE) -> None:
        self.base_url = base_url.rstrip("/")

    def authorize(self, auth: dict | None) -> Entitlement:
        token = (auth or {}).get("token")
        if not token:
            return Entitlement(False, "anon", reason="no passport/EPT presented")
        # Verifying the EPT signature + fetching the windy-talk entitlement is the
        # integration point: Eternitas CredentialIssuer (EPT verify) + windy-pro
        # entitlements. It stays a deny until Grant defines the `windy-talk` SKU
        # and its EPT scope — we never fabricate an entitlement.
        return Entitlement(
            False, _passport_id(token), reason=(
                f"{ENTITLEMENT_SKU!r} entitlement not defined in Eternitas yet "
                "(Grant to define the SKU + EPT scope; then wire EPT verify here)"),
        )


def get_authorizer() -> Authorizer:
    if os.environ.get("WINDYTALK_STRICT_AUTH") == "1":
        return EternitasAuthorizer()
    return DevAuthorizer()


def broker_token(passport: str, action_class: str, ttl_s: int = 300) -> str:
    """§10 brokered short-lived token: exchange a passport for a ≤5-min EPT scoped
    to one action class, via the Eternitas CredentialIssuer.

    Not implemented until the windy-talk EPT scope is defined (Grant). Raises so a
    half-wired broker can never masquerade as working (ADR-044 forced-honest).
    The client is designed to hold only the returned short-lived EPT — never the
    passport — so no long-lived token ever lands on disk (§10)."""
    raise NotImplementedError(
        "Eternitas EPT brokering for Windy Talk is not wired yet — needs the "
        f"{ENTITLEMENT_SKU!r} EPT scope defined in Eternitas (Grant). Until then "
        "the wedge runs on the Task-0.0 dev Mind key (scrubbed at 1.7 completion)."
    )


def _passport_id(token: str) -> str:
    # ET26-XXXX-XXXX passports are ids; an EPT is a JWT (use a short prefix).
    return token[:16] if token else "anon"
