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
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import BooleanSelector, BooleanSelectorConfig

from .const import (
    ALL_TIMEZONES_LST,
    CODE_TO_COUNTRY_DICT,
    CONF_BLE_RELAY_ENABLED,
    CONF_MEDIA_DL_IMAGE,
    CONF_MEDIA_DL_VIDEO,
    CONF_MEDIA_EV_TYPE,
    CONF_SCAN_INTERVAL_BLUETOOTH,
    COUNTRY_TO_CODE_DICT,
    DEFAULT_EVENTS,
    DOMAIN,
    LOGGER,
)


class PetkitOptionsFlowHandler(OptionsFlow):
    """Handle Petkit options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the Reolink options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=self.config_entry.options.get(CONF_SCAN_INTERVAL, 30),
                    ): vol.All(int, vol.Range(min=5, max=3600)),
                    vol.Required(
                        CONF_BLE_RELAY_ENABLED,
                        default=self.config_entry.options.get(
                            CONF_BLE_RELAY_ENABLED, True
                        ),
                    ): BooleanSelector(BooleanSelectorConfig()),
                    vol.Required(
                        CONF_SCAN_INTERVAL_BLUETOOTH,
                        default=self.config_entry.options.get(
                            CONF_SCAN_INTERVAL_BLUETOOTH, 15
                        ),
                    ): vol.All(int, vol.Range(min=2, max=60)),
                    vol.Required(
                        CONF_MEDIA_DL_VIDEO,
                        default=self.config_entry.options.get(
                            CONF_MEDIA_DL_VIDEO, True
                        ),
                    ): BooleanSelector(BooleanSelectorConfig()),
                    vol.Required(
                        CONF_MEDIA_DL_IMAGE,
                        default=self.config_entry.options.get(
                            CONF_MEDIA_DL_IMAGE, True
                        ),
                    ): BooleanSelector(BooleanSelectorConfig()),
                    vol.Optional(
                        CONF_MEDIA_EV_TYPE,
                        default=self.config_entry.options.get(
                            CONF_MEDIA_EV_TYPE, DEFAULT_EVENTS
                        ),
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
                            ],
                        )
                    ),
                }
            ),
        )


class PetkitFlowHandler(ConfigFlow, domain=DOMAIN):
    """Config flow for Petkit Smart Devices."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> PetkitOptionsFlowHandler:
        """Options callback for Reolink."""
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
                            CONF_SCAN_INTERVAL: 30,
                            CONF_BLE_RELAY_ENABLED: True,
                            CONF_SCAN_INTERVAL_BLUETOOTH: 15,
                            CONF_MEDIA_DL_VIDEO: True,
                            CONF_MEDIA_DL_IMAGE: True,
                            CONF_MEDIA_EV_TYPE: ["Pet", "Eat", "Feed", "Toileting"],
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
