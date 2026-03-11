"""Agora Signaling REST helper for PetKit WebRTC cameras."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote

import aiohttp
from pypetkitapi import LiveFeed

from .const import LOGGER

HEARTBEAT_INTERVAL_SECONDS = 0.5
START_LIVE_RETRIES = 5
START_LIVE_RETRY_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 10.0
HEARTBEAT_MAX_FAILURES = 10

SIGNALING_DOMAINS = [
    "api.agora.io",
    "api.sd-rtn.com",
]
SIGNALING_PATHS = [
    "/dev/v2/project/{app_id}/rtm/users/{user_id}/peer_messages",
]
SUCCESS_CODES = {
    "message_sent",
    "message_delivered",
}
STOP_SUCCESS_CODES = {
    "message_sent",
    "message_delivered",
    "message_offline",
}


class AgoraRTMSignaling:
    """Manage Signaling REST start_live, heartbeat, and stop_live lifecycle."""

    def __init__(self, app_id: str, is_sd: int = 0) -> None:
        """Initialize Signaling REST state."""
        self._app_id = app_id
        self._is_sd = is_sd

        self._app_user_id: str | None = None
        self._device_user_id: str | None = None
        self._token: str | None = None

        self._session: aiohttp.ClientSession | None = None
        self._state_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._preferred_domain: str | None = None
        self._preferred_path: str | None = None

    async def start_live(self, live_feed: LiveFeed) -> bool:
        """Start signaling and begin live heartbeat loop."""
        LOGGER.debug("AgoraRTMSignaling => start_live")
        credentials = self._extract_rtm_credentials(live_feed)
        if credentials is None:
            LOGGER.debug("Live feed missing RTM fields; skipping RTM signaling")
            return False

        app_user_id, device_user_id, token = credentials

        async with self._state_lock:
            await self._ensure_state(
                app_user_id=app_user_id,
                device_user_id=device_user_id,
                token=token,
            )

            started = await self._send_start_live_with_retry()
            if not started:
                return False

            self._ensure_heartbeat_locked()
            return True

    async def stop_live(self, send_stop: bool = True) -> None:
        """Stop heartbeat, send stop_live, and release signaling session."""
        async with self._state_lock:
            await self._teardown_locked(send_stop=send_stop)

    async def update_tokens(self, live_feed: LiveFeed) -> None:
        """Update in-memory signaling token from refreshed live feed."""
        credentials = self._extract_rtm_credentials(live_feed)
        if credentials is None:
            return

        app_user_id, device_user_id, token = credentials
        async with self._state_lock:
            if (
                self._app_user_id == app_user_id
                and self._device_user_id == device_user_id
            ):
                self._token = token

    @staticmethod
    def _extract_rtm_credentials(
        live_feed: LiveFeed,
    ) -> tuple[str, str, str] | None:
        """Return `(app_user_id, device_user_id, token)` if all fields exist."""
        app_user_id = str(live_feed.app_rtm_user_id or "").strip()
        device_user_id = str(live_feed.dev_rtm_user_id or "").strip()
        token = str(live_feed.rtm_token or "").strip()

        if not app_user_id or not device_user_id or not token:
            return None
        return app_user_id, device_user_id, token

    async def _ensure_state(
        self,
        app_user_id: str,
        device_user_id: str,
        token: str,
    ) -> None:
        """Ensure current runtime state and HTTP session are ready."""
        if (
            self._app_user_id == app_user_id
            and self._device_user_id == device_user_id
            and self._token == token
        ):
            await self._ensure_session()
            return

        if self._app_user_id and self._app_user_id != app_user_id:
            await self._teardown_locked(send_stop=False)

        self._app_user_id = app_user_id
        self._device_user_id = device_user_id
        self._token = token
        await self._ensure_session()

    async def _ensure_session(self) -> None:
        """Create aiohttp session lazily."""
        if self._session and not self._session.closed:
            return
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        )

    async def _send_start_live_with_retry(self) -> bool:
        """Send start_live with retries matching app behavior."""
        for attempt in range(START_LIVE_RETRIES):
            sent = await self._send_command(
                command="start_live",
                payload={"isSD": self._is_sd},
                wait_for_ack=True,
                accepted_codes=SUCCESS_CODES,
                suppress_errors=True,
            )
            LOGGER.debug(
                "start_live attempt %d/%d → sent=%s",
                attempt + 1,
                START_LIVE_RETRIES,
                sent,
            )
            if sent:
                LOGGER.debug(
                    "AgoraRTMSignaling => _send_start_live_with_retry is sended"
                )
                return True

            if attempt < START_LIVE_RETRIES - 1:
                await asyncio.sleep(START_LIVE_RETRY_DELAY_SECONDS)

        LOGGER.error(
            "Signaling start_live failed after %d attempts",
            START_LIVE_RETRIES,
        )
        return False

    async def _send_command(
        self,
        command: str,
        payload: dict[str, Any] | None = None,
        wait_for_ack: bool = False,
        accepted_codes: set[str] | None = None,
        suppress_errors: bool = False,
    ) -> bool:
        """Send one signaling command to the PetKit device peer."""
        if (
            self._session is None
            or self._session.closed
            or not self._app_user_id
            or not self._device_user_id
            or not self._token
        ):
            return False

        message_data: dict[str, Any] = {"cmd": command}
        if payload is not None:
            message_data["payload"] = payload

        message = json.dumps(message_data, separators=(",", ":"))
        request_body = {
            "destination": self._device_user_id,
            "enable_offline_messaging": False,
            "enable_historical_messaging": False,
            "payload": message,
        }

        headers = {
            "Content-Type": "application/json",
            "x-agora-token": self._token,
            "x-agora-uid": self._app_user_id,
            "Authorization": f"agora token={self._token}",
        }
        if accepted_codes is None:
            accepted_codes = SUCCESS_CODES

        query = "?wait_for_ack=true" if wait_for_ack else ""

        for domain, path_template in self._iter_endpoints():
            encoded_user_id = quote(self._app_user_id, safe="")
            path = path_template.format(
                app_id=self._app_id,
                user_id=encoded_user_id,
            )
            url = f"https://{domain}{path}{query}"

            LOGGER.debug(f"Send command URL= {url}")

            try:
                async with self._send_lock, self._session.post(
                    url,
                    json=request_body,
                    headers=headers,
                ) as response:
                    response_text = await response.text()
            except (aiohttp.ClientError, TimeoutError) as err:
                LOGGER.debug(
                    "Signaling request failed for %s on %s: %s",
                    command,
                    url,
                    err,
                )
                continue

            data: dict[str, Any] = {}
            if response_text:
                try:
                    data = json.loads(response_text)
                except json.JSONDecodeError:
                    data = {}

            if response.status == 404:
                LOGGER.debug("Send command NOK err 404")
                continue

            if response.status != 200:
                if response.status >= 500 or response.status == 429:
                    LOGGER.debug(
                        "Signaling command %s transient error (%s) on %s",
                        command,
                        response.status,
                        url,
                    )
                    continue

                if not suppress_errors:
                    LOGGER.warning(
                        "Signaling command %s failed (%s) on %s: %s",
                        command,
                        response.status,
                        url,
                        response_text,
                    )
                return False

            result = str(data.get("result", "")).lower()
            code = str(data.get("code", "")).lower()
            LOGGER.debug(
                "RTM response: status=%s result=%s code=%s body=%s",
                response.status,
                result,
                code,
                response_text,
            )
            if result == "success" and code in accepted_codes:
                LOGGER.debug("Send command success !")
                self._preferred_domain = domain
                self._preferred_path = path_template
                return True

            if not suppress_errors:
                LOGGER.warning(
                    "Signaling command %s rejected on %s: result=%s code=%s body=%s",
                    command,
                    url,
                    result,
                    code,
                    response_text,
                )
            return False

        log_fn = LOGGER.debug if suppress_errors else LOGGER.warning
        log_fn("Signaling command %s failed on all endpoints", command)
        return False

    def _iter_endpoints(self) -> list[tuple[str, str]]:
        """Return endpoint candidates, preferring last known good target."""
        domains = [*SIGNALING_DOMAINS]
        if self._preferred_domain in domains:
            domains.remove(self._preferred_domain)
            domains.insert(0, self._preferred_domain)

        paths = [*SIGNALING_PATHS]
        if self._preferred_path in paths:
            paths.remove(self._preferred_path)
            paths.insert(0, self._preferred_path)

        return [(domain, path) for domain in domains for path in paths]

    def _ensure_heartbeat_locked(self) -> None:
        """Start heartbeat task if not already running."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        """Send live_heartbeat every 500ms while streaming."""
        consecutive_failures = 0
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                LOGGER.debug("Send heartbeat...")
                sent = await self._send_command(
                    command="live_heartbeat",
                    payload={"isSD": self._is_sd},
                    wait_for_ack=False,
                    accepted_codes=SUCCESS_CODES,
                    suppress_errors=True,
                )
                if sent:
                    consecutive_failures = 0
                    continue

                consecutive_failures += 1
                if consecutive_failures >= HEARTBEAT_MAX_FAILURES:
                    LOGGER.warning(
                        "Signaling heartbeat failed %d consecutive times; stopping heartbeat",
                        consecutive_failures,
                    )
                    return
        except asyncio.CancelledError:
            LOGGER.debug("Signaling heartbeat loop cancelled")

    async def _teardown_locked(self, send_stop: bool) -> None:
        """Stop heartbeat and release Signaling session."""
        heartbeat_task = self._heartbeat_task
        self._heartbeat_task = None
        if heartbeat_task and not heartbeat_task.done():
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

        if send_stop:
            await self._send_command(
                command="stop_live",
                payload=None,
                wait_for_ack=False,
                accepted_codes=STOP_SUCCESS_CODES,
                suppress_errors=True,
            )

        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._app_user_id = None
        self._device_user_id = None
        self._token = None
        self._preferred_domain = None
        self._preferred_path = None
