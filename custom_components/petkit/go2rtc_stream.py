"""Helpers for exposing PetKit streams through a shared go2rtc instance."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from http import HTTPStatus
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from aiohttp import ClientError, ClientSession, ClientTimeout

from homeassistant.components.go2rtc.const import HA_MANAGED_URL
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import DOMAIN, LOGGER

_GO2RTC_DOMAIN = "go2rtc"
_HA_MANAGED_URL_ALIASES = {
    HA_MANAGED_URL,
    "http://127.0.0.1:11984/",
}
_SIGN_EXPIRATION = timedelta(days=365)
_GO2RTC_API_PATH = "api/streams"
_REQUEST_TIMEOUT = ClientTimeout(total=10)
_INFO_TIMEOUT = ClientTimeout(total=5)


class PetkitGo2RTCStreamManager:
    """Manage fixed go2rtc streams per PetKit device."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize shared stream bookkeeping for this Home Assistant instance."""
        self.hass = hass
        self._locks: dict[str, asyncio.Lock] = {}
        self._server_info: dict[str, dict] = {}

    @property
    def _session(self) -> ClientSession:
        """Return the aiohttp session for go2rtc API calls."""
        return async_get_clientsession(self.hass)

    @property
    def _go2rtc_data(self):
        """Return the Home Assistant go2rtc integration runtime data."""
        return self.hass.data.get(_GO2RTC_DOMAIN)

    @property
    def _configured_url(self) -> str | None:
        """Return the go2rtc URL stored by Home Assistant."""
        go2rtc_data = self.hass.data.get(_GO2RTC_DOMAIN)
        return getattr(go2rtc_data, "url", go2rtc_data)

    def stream_name(self, device_id: str) -> str:
        """Return the deterministic go2rtc stream name for one device."""
        return f"petkit_{device_id}"

    def configured_url(self) -> str | None:
        """Return the preferred go2rtc API base URL."""
        url = self._configured_url
        return self._normalize_url(url) if url is not None else None

    def is_available(self) -> bool:
        """Return whether a shared go2rtc instance is configured."""
        return self.configured_url() is not None

    def api_session(self) -> ClientSession:
        """Return the aiohttp session that can reach the configured go2rtc API."""
        base_url = self.configured_url()
        if base_url is None:
            return self._session
        return self._session_for_base_url(base_url)

    async def rtsp_url(self, target) -> str | None:
        """Return the RTSP URL exposed by the shared go2rtc stream."""
        camera = self._resolve_camera(target)
        if camera is None:
            return None

        base_url = self.configured_url()
        if base_url is None:
            return None

        rtsp_base = await self._rtsp_base_url(base_url)
        if rtsp_base is None:
            return None
        return f"{rtsp_base}/{self.stream_name(str(camera.device.id))}"

    def internal_webrtc_source(self, target) -> str | None:
        """Return the signed HA H.264 source URL consumed by the shared go2rtc."""
        camera = self._resolve_camera(target)
        if camera is None:
            return None

        source_base_url = self._ha_source_base_url()
        if source_base_url is None:
            return None

        # Raw H.264 from the aiortc receiver (see aiortc_upstream.py), not
        # go2rtc's own WebRTC client, which cannot recover lost packets.
        signed_path = async_sign_path(
            self.hass,
            f"/api/petkit/h264/{camera.device.id}",
            _SIGN_EXPIRATION,
        )
        return f"{source_base_url}{signed_path}"

    async def async_ensure_stream(
        self,
        target,
        *,
        raise_on_failure: bool = False,
    ) -> str | None:
        """Ensure the shared go2rtc stream exists and return its RTSP URL."""
        camera = self._resolve_camera(target)
        if camera is None:
            return None

        base_url = self.configured_url()
        if base_url is None:
            return None

        source = self.internal_webrtc_source(camera)
        if source is None:
            message = (
                f"PetKit go2rtc stream {camera.device.id} unavailable:"
                " HA HTTP server not reachable from go2rtc"
            )
            LOGGER.debug(message)
            if raise_on_failure:
                raise RuntimeError(message)
            return None

        device_id = str(camera.device.id)
        stream_name = self.stream_name(device_id)
        lock = self._locks.setdefault(stream_name, asyncio.Lock())
        async with lock:
            if await self._async_stream_matches(base_url, stream_name, source):
                await self._async_migrate_legacy_streams(base_url, camera)
                return await self.rtsp_url(camera)

            methods: tuple[tuple[str, dict[str, str]], ...] = (
                ("post", {"dst": stream_name, "src": source}),
                ("put", {"name": stream_name, "src": source}),
                ("patch", {"name": stream_name, "src": source}),
                ("patch", {"dst": stream_name, "src": source}),
            )

            statuses: list[str] = []
            for method, params in methods:
                status, detail = await self._async_call_api(base_url, method, params)
                detail_suffix = f" ({detail})" if detail else ""
                statuses.append(f"{method.upper()}={status}{detail_suffix}")
                if status in (
                    HTTPStatus.OK,
                    HTTPStatus.CREATED,
                    HTTPStatus.NO_CONTENT,
                ):
                    await self._async_migrate_legacy_streams(base_url, camera)
                    return await self.rtsp_url(camera)
                if await self._async_stream_matches(base_url, stream_name, source):
                    await self._async_migrate_legacy_streams(base_url, camera)
                    return await self.rtsp_url(camera)

            LOGGER.warning(
                "Failed to register PetKit go2rtc stream %s (%s)",
                device_id,
                ", ".join(statuses),
            )
            if raise_on_failure:
                raise RuntimeError(
                    f"Failed to register PetKit go2rtc stream {device_id}"
                    f" ({', '.join(statuses)})"
                )
            return None

    async def async_remove_stream(self, target) -> bool:
        """Remove the shared go2rtc stream if it exists."""
        camera = self._resolve_camera(target)
        if camera is None:
            return False

        base_url = self.configured_url()
        if base_url is None:
            return False

        device_id = str(camera.device.id)
        stream_name = self.stream_name(device_id)
        lock = self._locks.setdefault(stream_name, asyncio.Lock())
        async with lock:
            for params in ({"dst": stream_name}, {"name": stream_name}):
                status, _ = await self._async_call_api(base_url, "delete", params)
                if status in (HTTPStatus.OK, HTTPStatus.NO_CONTENT):
                    return True
                if status == HTTPStatus.NOT_FOUND:
                    continue
        return False

    def api_base_url(self) -> str | None:
        """Return the normalized go2rtc API base URL."""
        return self.configured_url()

    async def _async_stream_matches(
        self,
        base_url: str,
        stream_name: str,
        source: str,
    ) -> bool:
        """Return whether go2rtc already has the expected producer source."""
        streams = await self._async_get_streams(base_url)
        if streams is None:
            return False

        stream = streams.get(stream_name)
        if not isinstance(stream, dict):
            return False

        producers = stream.get("producers") or []
        normalized_source = self._normalize_source_url(source)
        return any(
            isinstance(producer, dict)
            and self._normalize_source_url(str(producer.get("url", "")))
            == normalized_source
            for producer in producers
        )

    async def _async_get_streams(self, base_url: str) -> dict[str, dict] | None:
        """Fetch the current go2rtc streams payload."""
        session = self._session_for_base_url(base_url)
        try:
            async with session.get(
                urljoin(base_url, _GO2RTC_API_PATH),
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                if response.status != HTTPStatus.OK:
                    return None
                payload = await response.json()
        except (ClientError, TimeoutError, ValueError) as err:
            LOGGER.debug("Failed to query HA-managed go2rtc streams: %s", err)
            return None

        if not isinstance(payload, dict):
            return None
        return payload

    async def _async_call_api(
        self, base_url: str, method: str, params: dict[str, str]
    ) -> tuple[int, str | None]:
        """Call the go2rtc API and return the HTTP status code plus error detail."""
        request: Callable[..., object] = getattr(
            self._session_for_base_url(base_url),
            method,
        )
        try:
            async with request(
                urljoin(base_url, _GO2RTC_API_PATH),
                params=params,
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                await response.read()
                return response.status, None
        except (ClientError, TimeoutError) as err:
            LOGGER.debug("go2rtc %s failed for %s: %s", method.upper(), params, err)
            return 0, str(err)

    async def _rtsp_base_url(self, base_url: str) -> str | None:
        """Return the RTSP base URL exposed by one configured go2rtc instance."""
        server_info = await self._async_get_server_info(base_url)
        if server_info is None:
            return None

        api_parts = urlsplit(base_url)
        host = api_parts.hostname
        if not host:
            return None

        rtsp_config = server_info.get("rtsp") or {}
        listen_value = str(rtsp_config.get("listen", ":8554"))
        if listen_value.startswith(":"):
            return f"rtsp://{host}{listen_value}"

        listen_parts = urlsplit(f"rtsp://{listen_value.lstrip('/')}")
        rtsp_host = listen_parts.hostname or host
        rtsp_port = listen_parts.port or 8554
        return f"rtsp://{rtsp_host}:{rtsp_port}"

    async def _async_get_server_info(self, base_url: str) -> dict | None:
        """Fetch and cache the go2rtc server metadata."""
        if base_url in self._server_info:
            return self._server_info[base_url]

        session = self._session_for_base_url(base_url)
        try:
            async with session.get(
                urljoin(base_url, "api"),
                timeout=_INFO_TIMEOUT,
            ) as response:
                if response.status != HTTPStatus.OK:
                    return None
                payload = await response.json()
        except (ClientError, TimeoutError, ValueError) as err:
            LOGGER.debug(
                "Failed to query go2rtc server info from %s: %s", base_url, err
            )
            return None

        if not isinstance(payload, dict):
            return None

        self._server_info[base_url] = payload
        return payload

    def _ha_source_base_url(self) -> str | None:
        """Return the Home Assistant base URL reachable from go2rtc."""
        for prefer_external in (False, True):
            try:
                url = get_url(self.hass, prefer_external=prefer_external)
            except NoURLAvailableError:
                continue
            if url:
                return url.rstrip("/")
        return None

    def _resolve_camera(self, target):
        """Resolve a camera entity from a camera object or device id."""
        if hasattr(target, "device") and hasattr(target, "coordinator"):
            return target

        cameras = self.hass.data.get(DOMAIN, {}).get("cameras", {})
        return cameras.get(str(target))

    def _session_for_base_url(self, base_url: str) -> ClientSession:
        """Return the correct aiohttp session for one go2rtc base URL."""
        go2rtc_data = self._go2rtc_data
        configured_url = getattr(go2rtc_data, "url", go2rtc_data)
        configured_session = getattr(go2rtc_data, "session", None)
        if (
            configured_session is not None
            and isinstance(configured_url, str)
            and self._normalize_url(configured_url) == self._normalize_url(base_url)
        ):
            return configured_session
        return self._session

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Normalize one go2rtc base URL for API calls."""
        return url.rstrip("/") + "/"

    @staticmethod
    def _normalize_source_url(source: str) -> str:
        """Normalize go2rtc producer URLs for stable comparisons."""
        if not source:
            return source

        raw_url = source
        if ":" in source:
            prefix, remainder = source.split(":", 1)
            if urlsplit(remainder).scheme in {"http", "https"}:
                raw_url = remainder
                # go2rtc stores HTTP-backed WebRTC producers as plain HTTP URLs in
                # /api/streams, so the transport wrapper must not participate in
                # equality checks.
                if prefix != "webrtc":
                    raw_url = f"{prefix}:{remainder}"

        parts = urlsplit(raw_url)
        if not parts.scheme:
            return source.rstrip("/")

        filtered_query = urlencode(
            [
                (key, value)
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
                if key != "authSig"
            ],
            doseq=True,
        )
        normalized = urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path.rstrip("/"),
                filtered_query,
                "",
            )
        )
        return normalized.rstrip("/")

    async def _async_migrate_legacy_streams(self, base_url: str, camera) -> None:
        """Rewrite deprecated stream sources to the canonical shared stream."""
        streams = await self._async_get_streams(base_url)
        if streams is None:
            return

        device_id = str(camera.device.id)
        canonical_name = self.stream_name(device_id)
        canonical_rtsp = await self.rtsp_url(camera)
        if canonical_rtsp is None:
            return

        deprecated_sources = self._deprecated_sources(camera)
        if not deprecated_sources:
            return

        for stream_name, stream in streams.items():
            if not self._is_legacy_stream_candidate(
                stream_name,
                stream,
                canonical_name,
                deprecated_sources,
            ):
                continue

            if await self._async_update_legacy_stream(
                base_url,
                stream_name,
                canonical_rtsp,
            ):
                LOGGER.debug(
                    "Migrated legacy go2rtc stream %s for %s to shared source %s",
                    stream_name,
                    device_id,
                    canonical_rtsp,
                )

    @staticmethod
    def _is_legacy_stream_candidate(
        stream_name: str,
        stream: object,
        canonical_name: str,
        deprecated_sources: set[str],
    ) -> bool:
        """Return whether a stream still points at an outdated PetKit source."""
        if stream_name == canonical_name or not isinstance(stream, dict):
            return False

        producers = stream.get("producers") or []
        return any(
            isinstance(producer, dict)
            and str(producer.get("url", "")).rstrip("/") in deprecated_sources
            for producer in producers
        )

    async def _async_update_legacy_stream(
        self,
        base_url: str,
        stream_name: str,
        canonical_rtsp: str,
    ) -> bool:
        """Point a legacy stream at the canonical shared RTSP source."""
        if await self._async_stream_matches(base_url, stream_name, canonical_rtsp):
            return False

        methods: tuple[tuple[str, dict[str, str]], ...] = (
            ("put", {"name": stream_name, "src": canonical_rtsp}),
            ("patch", {"name": stream_name, "src": canonical_rtsp}),
            ("patch", {"dst": stream_name, "src": canonical_rtsp}),
        )
        for method, params in methods:
            status, _ = await self._async_call_api(base_url, method, params)
            if status in (
                HTTPStatus.OK,
                HTTPStatus.CREATED,
                HTTPStatus.NO_CONTENT,
            ) or await self._async_stream_matches(
                base_url, stream_name, canonical_rtsp
            ):
                return True
        return False

    def _deprecated_sources(self, camera) -> set[str]:
        """Return legacy source URLs for one camera that should point at the shared stream."""
        device_id = str(camera.device.id)
        deprecated: set[str] = set()
        for base_url in self._ha_base_urls():
            deprecated.add(f"webrtc:{base_url}/api/petkit/whep_direct/{device_id}")
            deprecated.add(f"{base_url}/api/petkit/whep_direct/{device_id}")
            deprecated.add(f"webrtc:{base_url}/api/petkit/whep_upstream/{device_id}")
            deprecated.add(f"webrtc:{base_url}/api/petkit/whep_mirror/{device_id}")

        return deprecated

    def _ha_base_urls(self) -> set[str]:
        """Return reachable Home Assistant base URLs without trailing slashes."""
        base_urls: set[str] = set()
        for prefer_external in (False, True):
            try:
                base_url = get_url(self.hass, prefer_external=prefer_external)
            except NoURLAvailableError:
                continue
            if base_url:
                base_urls.add(base_url.rstrip("/"))
        return base_urls


def get_go2rtc_stream_manager(hass: HomeAssistant) -> PetkitGo2RTCStreamManager:
    """Return the shared HA-managed go2rtc helper."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    manager = domain_data.get("go2rtc_stream_manager")
    if manager is None:
        manager = PetkitGo2RTCStreamManager(hass)
        domain_data["go2rtc_stream_manager"] = manager
    return manager
