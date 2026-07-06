"""Adds config flow for Petkit Smart Devices."""

from __future__ import annotations

from typing import Any

from pypetkitapi import (
    PetkitAuthenticationUnregisteredEmailError,
    PetKitClient,
    PetkitSessionError,
    PetkitTimeoutError,
    PypetkitError,
    RegionServerGroup,
)
from pypetkitapi.exceptions import PetkitAuthenticationError, PetkitServerBusyError
import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_REGION,
    CONF_TIME_ZONE,
    CONF_USERNAME,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import BooleanSelector, BooleanSelectorConfig

from .const import (
    ADVANCED_SECTION,
    ALL_TIMEZONES_LST,
    BT_SECTION,
    CODE_TO_COUNTRY_DICT,
    CONF_BLE_RELAY_ENABLED,
    CONF_DELETE_AFTER,
    CONF_ENABLED_NOTIFICATIONS,
    CONF_MEDIA_DL_IMAGE,
    CONF_MEDIA_DL_VIDEO,
    CONF_MEDIA_EV_TYPE,
    CONF_MEDIA_PATH,
    CONF_SCAN_INTERVAL_BLUETOOTH,
    CONF_SCAN_INTERVAL_MEDIA,
    COUNTRY_TO_CODE_DICT,
    DEFAULT_BLUETOOTH_RELAY,
    DEFAULT_DELETE_AFTER,
    DEFAULT_DL_IMAGE,
    DEFAULT_DL_VIDEO,
    DEFAULT_ENABLED_NOTIFICATIONS,
    DEFAULT_EVENTS,
    DEFAULT_MEDIA_PATH,
    DEFAULT_SCAN_INTERVAL_BLUETOOTH,
    DEFAULT_SCAN_INTERVAL_MEDIA,
    DOMAIN,
    LOGGER,
    MEDIA_SECTION,
    NOTIFICATION_CATEGORIES,
    NOTIFICATION_SECTION,
)


class PetkitOptionsFlowHandler(OptionsFlow):
    """Handle Petkit options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(MEDIA_SECTION): section(
                        vol.Schema(
                            {
                                vol.Required(
                                    CONF_MEDIA_PATH,
                                    default=self.config_entry.options.get(
                                        MEDIA_SECTION, {}
                                    ).get(CONF_MEDIA_PATH, DEFAULT_MEDIA_PATH),
                                ): vol.All(str),
                                vol.Required(
                                    CONF_SCAN_INTERVAL_MEDIA,
                                    default=self.config_entry.options.get(
                                        MEDIA_SECTION, {}
                                    ).get(
                                        CONF_SCAN_INTERVAL_MEDIA,
                                        DEFAULT_SCAN_INTERVAL_MEDIA,
                                    ),
                                ): vol.All(int, vol.Range(min=5, max=120)),
                                vol.Required(
                                    CONF_MEDIA_DL_IMAGE,
                                    default=self.config_entry.options.get(
                                        MEDIA_SECTION, {}
                                    ).get(CONF_MEDIA_DL_IMAGE, DEFAULT_DL_IMAGE),
                                ): BooleanSelector(BooleanSelectorConfig()),
                                vol.Required(
                                    CONF_MEDIA_DL_VIDEO,
                                    default=self.config_entry.options.get(
                                        MEDIA_SECTION, {}
                                    ).get(CONF_MEDIA_DL_VIDEO, DEFAULT_DL_VIDEO),
                                ): BooleanSelector(BooleanSelectorConfig()),
                                vol.Optional(
                                    CONF_MEDIA_EV_TYPE,
                                    default=self.config_entry.options.get(
                                        MEDIA_SECTION, {}
                                    ).get(CONF_MEDIA_EV_TYPE, DEFAULT_EVENTS),
                                ): selector.SelectSelector(
                                    selector.SelectSelectorConfig(
                                        multiple=True,
                                        sort=False,
                                        options=[
                                            "Pet",
                                            "Eat",
                                            "Feed",
                                            "Toileting",
                                            "Move",
                                            "Dish_before",
                                            "Dish_after",
                                            "Waste_check",
                                            "Pet_detect",
                                            "Drink_over",
                                        ],
                                    )
                                ),
                                vol.Required(
                                    CONF_DELETE_AFTER,
                                    default=self.config_entry.options.get(
                                        MEDIA_SECTION, {}
                                    ).get(CONF_DELETE_AFTER, DEFAULT_DELETE_AFTER),
                                ): vol.All(int, vol.Range(min=0, max=30)),
                            }
                        ),
                        {"collapsed": False},
                    ),
                    vol.Required(BT_SECTION): section(
                        vol.Schema(
                            {
                                vol.Required(
                                    CONF_BLE_RELAY_ENABLED,
                                    default=self.config_entry.options.get(
                                        BT_SECTION, {}
                                    ).get(
                                        CONF_BLE_RELAY_ENABLED, DEFAULT_BLUETOOTH_RELAY
                                    ),
                                ): BooleanSelector(BooleanSelectorConfig()),
                                vol.Required(
                                    CONF_SCAN_INTERVAL_BLUETOOTH,
                                    default=self.config_entry.options.get(
                                        BT_SECTION, {}
                                    ).get(
                                        CONF_SCAN_INTERVAL_BLUETOOTH,
                                        DEFAULT_SCAN_INTERVAL_BLUETOOTH,
                                    ),
                                ): vol.All(int, vol.Range(min=5, max=120)),
                            }
                        ),
                        {"collapsed": False},
                    ),
                    vol.Required(NOTIFICATION_SECTION): section(
                        vol.Schema(
                            {
                                vol.Optional(
                                    CONF_ENABLED_NOTIFICATIONS,
                                    default=self.config_entry.options.get(
                                        NOTIFICATION_SECTION, {}
                                    ).get(
                                        CONF_ENABLED_NOTIFICATIONS,
                                        DEFAULT_ENABLED_NOTIFICATIONS,
                                    ),
                                ): selector.SelectSelector(
                                    selector.SelectSelectorConfig(
                                        multiple=True,
                                        sort=False,
                                        translation_key="petkit_notification_category",
                                        options=list(NOTIFICATION_CATEGORIES),
                                    )
                                ),
                            }
                        ),
                        {"collapsed": False},
                    ),
                }
            ),
        )


class PetkitFlowHandler(ConfigFlow, domain=DOMAIN):
    """Config flow for Petkit Smart Devices."""

    VERSION = 10

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> PetkitOptionsFlowHandler:
        """Options callback."""
        return PetkitOptionsFlowHandler()

    async def async_step_user(
        self,
        user_input: dict | None = None,
    ) -> data_entry_flow.FlowResult:
        """Handle a flow initialized by the user."""
        _errors = {}

        country_from_ha = self.hass.config.country
        tz_from_ha = self.hass.config.time_zone
        LOGGER.debug(
            f"Country code from HA : {self.hass.config.country} Default timezone: {tz_from_ha}"
        )

        # PetKit's Chinese cloud uses phone/ID logins; the international cloud
        # uses email. Tailor the hint based on the user's HA country so the
        # 99% case isn't shown a confusing "or id if you are a Chinese user".
        if country_from_ha == "CN":
            username_hint = "Enter your PetKit account phone number or PetKit ID."
        else:
            username_hint = "Enter your PetKit account email."

        # Fetch the live PetKit gateway list so we can render a short server
        # dropdown (~5 entries) instead of a 200+ country list. If the call
        # fails we silently fall back to the country list below.
        servers: list[RegionServerGroup] | None
        try:
            servers = await PetKitClient.fetch_region_servers(
                async_get_clientsession(self.hass)
            )
        except PypetkitError as exc:
            LOGGER.debug("Failed to fetch PetKit region servers: %s", exc)
            servers = None

        if user_input is not None:
            advanced = user_input.get(ADVANCED_SECTION, {})
            raw_region = advanced.get(CONF_REGION)
            # Accept either dropdown shape: server-mode delivers an ISO code
            # already, country-mode delivers a country name we look up.
            user_region = (
                COUNTRY_TO_CODE_DICT.get(raw_region) or raw_region or country_from_ha
            )
            user_timezone = advanced.get(CONF_TIME_ZONE) or tz_from_ha

            # Flatten section data for storage so __init__.py keeps reading
            # CONF_REGION/CONF_TIME_ZONE at the top level of entry.data.
            entry_data = {
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_REGION: user_region,
                CONF_TIME_ZONE: user_timezone,
            }

            # Check if the account already exists
            existing_entries = self._async_current_entries()
            for entry in existing_entries:
                if entry.data.get(CONF_USERNAME) == user_input[CONF_USERNAME]:
                    _errors["base"] = "account_exists"
                    break
            else:
                try:
                    await self._test_credentials(
                        username=user_input[CONF_USERNAME],
                        password=user_input[CONF_PASSWORD],
                        region=user_region,
                        timezone=user_timezone,
                    )

                except PetkitServerBusyError:
                    _errors["base"] = "server_busy"

                except PetkitAuthenticationError:
                    _errors["base"] = "invalid_auth"

                except PetkitAuthenticationUnregisteredEmailError:
                    _errors["base"] = "invalid_region"

                except PetkitTimeoutError:
                    _errors["base"] = "cannot_connect"

                except PetkitSessionError:
                    _errors["base"] = "session_error"

                except Exception:  # noqa: BLE001
                    LOGGER.exception("Unexpected error")
                    _errors["base"] = "unknown"
                else:
                    return self.async_create_entry(
                        title=user_input[CONF_USERNAME],
                        data=entry_data,
                        options={
                            MEDIA_SECTION: {
                                CONF_MEDIA_PATH: DEFAULT_MEDIA_PATH,
                                CONF_SCAN_INTERVAL_MEDIA: DEFAULT_SCAN_INTERVAL_MEDIA,
                                CONF_MEDIA_DL_IMAGE: DEFAULT_DL_IMAGE,
                                CONF_MEDIA_DL_VIDEO: DEFAULT_DL_VIDEO,
                                CONF_MEDIA_EV_TYPE: DEFAULT_EVENTS,
                                CONF_DELETE_AFTER: DEFAULT_DELETE_AFTER,
                            },
                            BT_SECTION: {
                                CONF_BLE_RELAY_ENABLED: DEFAULT_BLUETOOTH_RELAY,
                                CONF_SCAN_INTERVAL_BLUETOOTH: DEFAULT_SCAN_INTERVAL_BLUETOOTH,
                            },
                            NOTIFICATION_SECTION: {
                                CONF_ENABLED_NOTIFICATIONS: list(
                                    DEFAULT_ENABLED_NOTIFICATIONS
                                ),
                            },
                        },
                    )

        prev_advanced = (user_input or {}).get(ADVANCED_SECTION, {})
        tz_default = prev_advanced.get(CONF_TIME_ZONE) or tz_from_ha

        if servers:
            region_options = [
                selector.SelectOptionDict(value=s.representative_country, label=s.label)
                for s in servers
            ]
            valid_region_values = {s.representative_country for s in servers}
            # Find the server whose country bucket contains the HA country and
            # default to that server's representative code. If HA's country is
            # not in any bucket, fall back to the first listed server.
            auto_server = next(
                (s for s in servers if country_from_ha in s.countries),
                servers[0],
            )
            auto_default = auto_server.representative_country
            prev_region = prev_advanced.get(CONF_REGION)
            # Drop a previously selected value if it is from the old country
            # dropdown so voluptuous doesn't render an empty selector.
            region_default = (
                prev_region if prev_region in valid_region_values else auto_default
            )
            region_selector = selector.SelectSelector(
                selector.SelectSelectorConfig(options=region_options)
            )
        else:
            # API unreachable: keep the legacy 200-country dropdown so users
            # can still complete setup manually.
            region_default = prev_advanced.get(CONF_REGION) or CODE_TO_COUNTRY_DICT.get(
                country_from_ha, country_from_ha
            )
            region_selector = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=sorted(CODE_TO_COUNTRY_DICT.values())
                ),
            )

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_USERNAME,
                    default=(user_input or {}).get(CONF_USERNAME, vol.UNDEFINED),
                ): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.TEXT,
                    ),
                ),
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.PASSWORD,
                    ),
                ),
                vol.Required(ADVANCED_SECTION): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_REGION, default=region_default
                            ): region_selector,
                            vol.Required(
                                CONF_TIME_ZONE, default=tz_default
                            ): selector.SelectSelector(
                                selector.SelectSelectorConfig(
                                    options=ALL_TIMEZONES_LST
                                ),
                            ),
                        }
                    ),
                    {"collapsed": True},
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=_errors,
            description_placeholders={
                "username_hint": username_hint,
                "wiki_url": "https://github.com/Jezza34000/homeassistant_petkit/wiki/Configuration",
            },
        )

    async def _test_credentials(
        self, username: str, password: str, region: str, timezone: str
    ) -> None:
        """Validate credentials."""
        client = PetKitClient(
            username=username,
            password=password,
            region=region,
            timezone=timezone,
            session=async_get_clientsession(self.hass),
        )
        LOGGER.debug("Testing credentials for %s", username)
        await client.login()
