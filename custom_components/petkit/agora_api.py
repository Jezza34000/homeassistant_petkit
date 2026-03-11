"""Agora WebRTC API client for PetKit cameras."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import secrets
import time
from typing import Any, Self

import aiohttp

LOGGER = logging.getLogger(__name__)

# Service IDs in request payload (request_bodies[].buffer.service_ids)
SERVICE_IDS: dict[str, int] = {
    "CHOOSE_SERVER": 11,
    "CLOUD_PROXY": 18,
    "CLOUD_PROXY_5": 20,
    "CLOUD_PROXY_FALLBACK": 26,
}

# Flags in response payload (response_body[].buffer.flag)
RESPONSE_FLAGS: dict[str, int] = {
    "CHOOSE_SERVER": 4096,
    "CLOUD_PROXY": 1048576,
    "CLOUD_PROXY_5": 4194304,
    "CLOUD_PROXY_FALLBACK": 4194310,
}


def derive_password(uid: int | str) -> str:
    """Derive TURN password from Agora uid."""
    return hashlib.sha256(str(uid).encode("utf-8")).hexdigest()


@dataclass
class EdgeAddress:
    """Agora edge address entry."""

    ip: str
    port: int
    username: str | None = None
    credentials: str | None = None
    ticket: str | None = None
    fingerprint: str | None = None


@dataclass
class ICEServer:
    """RTCIceServer-like structure."""

    urls: str | list[str]
    username: str | None = None
    credential: str | None = None


@dataclass
class AgoraResponse:
    """Parsed Agora choose-server response."""

    code: int
    addresses: list[EdgeAddress]
    ticket: str
    uid: int
    cid: int
    cname: str
    server_ts: int
    detail: dict[str, Any]
    flag: int
    opid: int
    responses: dict[int, dict[str, Any]] | None = None

    @classmethod
    def from_api_response(cls, response_data: dict[str, Any]) -> AgoraResponse:
        """Parse /api/v2/transpond/webrtc response payload."""
        response_body = response_data.get("response_body", [])
        if not response_body:
            raise ValueError("Agora response_body is empty")

        detail_base = response_data.get("detail", {}) or {}
        responses_by_flag: dict[int, dict[str, Any]] = {}

        for response_item in response_body:
            buffer = response_item.get("buffer", {}) or {}
            code = int(buffer.get("code", -1))
            if code != 0:
                LOGGER.debug(
                    "Skipping Agora response buffer with non-zero code=%s flag=%s",
                    code,
                    buffer.get("flag"),
                )
                continue

            flag = int(buffer.get("flag", 0))
            uid = int(buffer.get("uid", 0))
            ticket = str(buffer.get("cert", ""))
            edges_services = buffer.get("edges_services", []) or []

            detail = {
                **detail_base,
                **(buffer.get("detail", {}) or {}),
            }

            username = str(detail.get("8", "") or "")
            credentials = str(detail.get("4", "") or "")
            if not username:
                username = str(uid)
            if not credentials:
                credentials = derive_password(uid)
            if not username:
                username = "test"
            if not credentials:
                credentials = "111111"

            # detail[19] contains semicolon-separated fingerprints (optional)
            fingerprints: list[str] = []
            fingerprint_str = str(detail.get("19", "") or "")
            if fingerprint_str:
                fingerprints = [
                    fingerprint.strip()
                    for fingerprint in fingerprint_str.split(";")
                    if fingerprint.strip()
                ]

            addresses = [
                EdgeAddress(
                    ip=str(edge.get("ip", "")),
                    port=int(edge.get("port", 0)),
                    username=username,
                    credentials=credentials,
                    ticket=ticket,
                    fingerprint=(
                        fingerprints[index] if index < len(fingerprints) else None
                    ),
                )
                for index, edge in enumerate(edges_services)
                if edge.get("ip") and edge.get("port")
            ]

            responses_by_flag[flag] = {
                "code": code,
                "addresses": addresses,
                "ticket": ticket,
                "uid": uid,
                "cid": int(buffer.get("cid", 0)),
                "cname": str(buffer.get("cname", "")),
                "detail": detail,
                "flag": flag,
            }

        if not responses_by_flag:
            raise ValueError("Agora API response did not contain a successful buffer")

        primary = responses_by_flag.get(RESPONSE_FLAGS["CHOOSE_SERVER"])
        if primary is None:
            primary = next(iter(responses_by_flag.values()))

        return cls(
            code=int(primary.get("code", -1)),
            addresses=primary.get("addresses", []),
            ticket=str(primary.get("ticket", "")),
            uid=int(primary.get("uid", 0)),
            cid=int(primary.get("cid", 0)),
            cname=str(primary.get("cname", "")),
            server_ts=int(response_data.get("enter_ts", int(time.time() * 1000))),
            detail=primary.get("detail", {}) or {},
            flag=int(primary.get("flag", 0)),
            opid=int(response_data.get("opid", 0)),
            responses=responses_by_flag,
        )

    def get_responses_by_flag(self, flag: int) -> dict[str, Any] | None:
        """Return parsed response block for one Agora flag."""
        if not self.responses:
            return None
        return self.responses.get(flag)

    def get_gateway_addresses(self) -> list[EdgeAddress]:
        """Return gateway addresses (flag 4096)."""
        if self.responses:
            response = self.responses.get(RESPONSE_FLAGS["CHOOSE_SERVER"])
            if response:
                return response.get("addresses", [])
        if self.flag == RESPONSE_FLAGS["CHOOSE_SERVER"]:
            return self.addresses
        return []

    def get_turn_addresses(self) -> list[EdgeAddress]:
        """Return TURN addresses (flag 4194310)."""
        if self.responses:
            response = self.responses.get(RESPONSE_FLAGS["CLOUD_PROXY_FALLBACK"])
            if response:
                return response.get("addresses", [])
        if self.flag == RESPONSE_FLAGS["CLOUD_PROXY_FALLBACK"]:
            return self.addresses
        return []

    def get_ice_servers(
        self,
        use_all_turn_servers: bool = False,
        new_turn_mode: int = 4,
    ) -> list[ICEServer]:
        """Convert TURN endpoints into RTCIceServer objects."""
        turn_addresses = self.get_turn_addresses() or self.addresses
        if not turn_addresses:
            return []

        addresses = turn_addresses if use_all_turn_servers else turn_addresses[:1]
        servers: list[ICEServer] = []

        for address in addresses:
            if new_turn_mode in (1, 4):
                servers.append(
                    ICEServer(
                        urls=f"turn:{address.ip}:3478?transport=udp",
                        username=address.username,
                        credential=address.credentials,
                    )
                )
            if new_turn_mode in (2, 4):
                servers.append(
                    ICEServer(
                        urls=f"turn:{address.ip}:3478?transport=tcp",
                        username=address.username,
                        credential=address.credentials,
                    )
                )
            if new_turn_mode in (3, 4):
                servers.append(
                    ICEServer(
                        urls=(
                            "turns:"
                            f"{address.ip.replace('.', '-')}.edge.agora.io:443"
                            "?transport=tcp"
                        ),
                        username=address.username,
                        credential=address.credentials,
                    )
                )

        return servers

    def to_ap_response(self, flag: int | None = None) -> dict[str, Any]:
        """Convert response to join_v3 ap_response payload format."""
        if flag is not None and self.responses:
            response = self.responses.get(flag)
            if response:
                return {
                    "code": response["code"],
                    "server_ts": self.server_ts,
                    "uid": response["uid"],
                    "cid": response["cid"],
                    "cname": response["cname"],
                    "detail": response["detail"],
                    "flag": response["flag"],
                    "opid": self.opid,
                    "cert": response["ticket"],
                    "ticket": response["ticket"],
                }

        return {
            "code": self.code,
            "server_ts": self.server_ts,
            "uid": self.uid,
            "cid": self.cid,
            "cname": self.cname,
            "detail": self.detail,
            "flag": self.flag,
            "opid": self.opid,
            "cert": self.ticket,
            "ticket": self.ticket,
        }


class AgoraAPIClient:
    """Agora choose-server API client."""

    WEBCS_DOMAIN = [
        "webrtc2-ap-web-1.agora.io",
        "webrtc2-ap-web-2.agora.io",
        "webrtc2-ap-web-3.agora.io",
        "webrtc2-ap-web-4.agora.io",
    ]
    WEBCS_DOMAIN_BACKUP = [
        "webrtc2-ap-web-5.agora.io",
        "webrtc2-ap-web-6.agora.io",
    ]

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        """Initialize API client."""
        self.session = session
        self._own_session = session is None

    async def __aenter__(self) -> Self:
        """Context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        """Context manager exit."""
        if self._own_session and self.session:
            await self.session.close()

    async def choose_server(
        self,
        app_id: str,
        token: str,
        channel_name: str,
        user_id: int,
        string_uid: str | None = None,
        role: int = 1,
        area_code: str = "CN,GLOBAL",
        service_flags: list[int] | None = None,
        sid: str | None = None,
        proxy_server: str | None = None,
    ) -> AgoraResponse:
        """Request gateway + TURN servers for a channel/token."""
        if string_uid is None:
            string_uid = str(user_id)
        if service_flags is None:
            service_flags = [
                SERVICE_IDS["CHOOSE_SERVER"],
                SERVICE_IDS["CLOUD_PROXY_FALLBACK"],
            ]
        if sid is None:
            sid = str(secrets.randbelow(2**31))

        payload = self._build_request_payload(
            app_id=app_id,
            token=token,
            channel_name=channel_name,
            user_id=user_id,
            string_uid=string_uid,
            service_flags=service_flags,
            sid=sid,
            uri=22,
            role=role,
            area_code=area_code,
        )
        response = await self._make_api_call(payload, proxy_server=proxy_server)
        return AgoraResponse.from_api_response(response)

    @staticmethod
    def _merge_objects(*objects: dict[str, Any] | None) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for obj in objects:
            if obj is None:
                continue
            merged.update({k: v for k, v in obj.items() if v is not None})
        return merged

    def _build_request_payload(
        self,
        app_id: str,
        token: str,
        channel_name: str,
        user_id: int,
        string_uid: str,
        service_flags: list[int],
        sid: str,
        uri: int,
        role: int,
        area_code: str,
    ) -> dict[str, Any]:
        client_ts = int(time.time() * 1000)
        opid = secrets.randbelow(10**12)

        detail = self._merge_objects(
            {"11": area_code},
            {"17": str(role)} if role else None,
            {"22": area_code},
            # Included to match Agora SDK behavior.
            {"6": string_uid} if string_uid else None,
        )

        return {
            "appid": app_id,
            "client_ts": client_ts,
            "opid": opid,
            "sid": sid,
            "request_bodies": [
                {
                    "uri": uri,
                    "buffer": {
                        "cname": channel_name,
                        "detail": detail,
                        "key": token,
                        "service_ids": service_flags,
                        "uid": user_id,
                    },
                }
            ],
        }

    async def _make_api_call(
        self,
        request_payload: dict[str, Any],
        proxy_server: str | None = None,
    ) -> dict[str, Any]:
        session = self.session
        should_close = False
        if session is None:
            session = aiohttp.ClientSession()
            if self._own_session:
                self.session = session
            else:
                should_close = True

        try:
            for domain in [
                *self.WEBCS_DOMAIN,
                *self.WEBCS_DOMAIN_BACKUP,
            ]:
                try:
                    return await self._call_endpoint(
                        session,
                        domain,
                        request_payload,
                        proxy_server,
                    )
                except (
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    ValueError,
                ) as err:
                    LOGGER.debug("Agora endpoint %s failed: %s", domain, err)
                    continue

            raise RuntimeError("All Agora endpoints failed")
        finally:
            if should_close:
                await session.close()

    async def _call_endpoint(
        self,
        session: aiohttp.ClientSession,
        domain: str,
        request_payload: dict[str, Any],
        proxy_server: str | None = None,
    ) -> dict[str, Any]:
        if proxy_server:
            url = (
                f"https://{proxy_server}/ap/?url="
                f"{domain}/api/v2/transpond/webrtc?v=2"
            )
        else:
            url = f"https://{domain}/api/v2/transpond/webrtc?v=2"

        form_data = aiohttp.FormData()
        form_data.add_field(
            "request",
            json.dumps(request_payload),
            content_type="application/json",
        )

        async with session.post(
            url,
            data=form_data,
            timeout=aiohttp.ClientTimeout(total=10),
            ssl=False,
        ) as response:
            if response.status != 200:
                raise ValueError(
                    f"Agora API returned status={response.status}: "
                    f"{await response.text()}"
                )
            return await response.json()
