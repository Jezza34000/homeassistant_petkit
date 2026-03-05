"""Adds config flow for Petkit Smart Devices."""

from __future__ import annotations

from typing import Any

from pypetkitapi import (
    PetkitAuthenticationUnregisteredEmailError,
    PetKitClient,
    PetkitRegionalServerNotFoundError,
    PetkitSessionError,
    PetkitSessionExpiredError,
    PetkitTimeoutError,
    PypetkitError,
)
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
    CONF_SCAN_INTERVAL,
    CONF_TIME_ZONE,
    CONF_USERNAME,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import BooleanSelector, BooleanSelectorConfig

from .const import (
    ALL_TIMEZONES_LST,
    BT_SECTION,
    CODE_TO_COUNTRY_DICT,
    CONF_BLE_RELAY_ENABLED,
    CONF_DELETE_AFTER,
    CONF_MEDIA_DL_IMAGE,
    CONF_MEDIA_DL_VIDEO,
    CONF_MEDIA_EV_TYPE,
    CONF_MEDIA_PATH,
    CONF_REALTIME_MQTT,
    CONF_SCAN_INTERVAL_BLUETOOTH,
    CONF_SCAN_INTERVAL_MEDIA,
    CONF_SMART_POLLING,
    COUNTRY_TO_CODE_DICT,
    DEFAULT_BLUETOOTH_RELAY,
    DEFAULT_DELETE_AFTER,
    DEFAULT_DL_IMAGE,
    DEFAULT_DL_VIDEO,
    DEFAULT_EVENTS,
    DEFAULT_MEDIA_PATH,
    DEFAULT_REALTIME_MQTT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_BLUETOOTH,
    DEFAULT_SCAN_INTERVAL_MEDIA,
    DEFAULT_SMART_POLLING,
    DOMAIN,
    LOGGER,
    MEDIA_SECTION,
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
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                    ): vol.All(int, vol.Range(min=15, max=3600)),
                    vol.Required(
                        CONF_SMART_POLLING,
                        default=self.config_entry.options.get(
                            CONF_SMART_POLLING, DEFAULT_SMART_POLLING
                        ),
                    ): BooleanSelector(BooleanSelectorConfig()),
                    vol.Required(
                        CONF_REALTIME_MQTT,
                        default=self.config_entry.options.get(
                            CONF_REALTIME_MQTT, DEFAULT_REALTIME_MQTT
                        ),
                    ): BooleanSelector(BooleanSelectorConfig()),
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
                }
            ),
        )


class PetkitFlowHandler(ConfigFlow, domain=DOMAIN):
    """Config flow for Petkit Smart Devices."""

    VERSION = 7

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

        if user_input is not None:
            user_region = (
                COUNTRY_TO_CODE_DICT.get(user_input.get(CONF_REGION, None))
                or country_from_ha
            )

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
                        timezone=user_input.get(CONF_TIME_ZONE, tz_from_ha),
                    )
                except (
                    PetkitTimeoutError,
                    PetkitSessionError,
                    PetkitSessionExpiredError,
                    PetkitAuthenticationUnregisteredEmailError,
                    PetkitRegionalServerNotFoundError,
                ) as exception:
                    LOGGER.error(exception)
                    _errors["base"] = str(exception)
                except PypetkitError as exception:
                    LOGGER.error(exception)
                    _errors["base"] = "error"
                else:
                    return self.async_create_entry(
                        title=user_input[CONF_USERNAME],
                        data=user_input,
                        options={
                            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                            CONF_SMART_POLLING: DEFAULT_SMART_POLLING,
                            CONF_REALTIME_MQTT: DEFAULT_REALTIME_MQTT,
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
                        },
                    )

        data_schema = {
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
        }

        if _errors:
            data_schema.update(
                {
                    vol.Required(
                        CONF_REGION,
                        default=CODE_TO_COUNTRY_DICT.get(
                            country_from_ha, country_from_ha
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=sorted(CODE_TO_COUNTRY_DICT.values())
                        ),
                    ),
                    vol.Required(
                        CONF_TIME_ZONE, default=tz_from_ha
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=ALL_TIMEZONES_LST),
                    ),
                }
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(data_schema),
            errors=_errors,
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
        LOGGER.debug(f"Testing credentials for {username}")
        await client.login()
