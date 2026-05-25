"""The schluter integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from aiohttp.client_exceptions import ClientConnectorError
from .api import (
    ApiError,
    InvalidSessionIdError,
    InvalidUserPasswordError,
    SchluterApi,
)
import async_timeout

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.core_config import Config
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CLIMATE, Platform.SENSOR]
CONF_REFRESH_TOKEN = "refresh_token"


async def async_setup(hass: HomeAssistant, config: Config) -> bool:
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    username: str = entry.data[CONF_USERNAME]
    password: str = entry.data[CONF_PASSWORD]
    refresh_token: str | None = entry.data.get(CONF_REFRESH_TOKEN)

    websession = async_get_clientsession(hass)
    api = SchluterApi(websession)

    coordinator = SchluterDataUpdateCoordinator(
        hass, api, username, password, refresh_token, entry
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = SchluterData(
        api=api,
        coordinator=coordinator,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data: SchluterData | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if data is not None:
        try:
            await data.api.async_logout()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Logout during unload failed: %s", err)

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


class SchluterDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(
        self,
        hass: HomeAssistant,
        api: SchluterApi,
        username: str,
        password: str,
        refresh_token: str | None = None,
        entry: ConfigEntry | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._api = api
        self._sessionid: str | None = None
        self._refresh_token = refresh_token
        self._entry = entry
        self._hass = hass

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=1),
        )

    async def _async_ensure_session(self) -> str:
        """Return a valid session id, preferring /connect over /login to spare slots."""
        if self._sessionid is not None:
            return self._sessionid

        if self._refresh_token:
            try:
                self._sessionid = await self._api.async_connect_with_refresh_token(
                    self._refresh_token
                )
                self._persist_refresh_token()
                return self._sessionid
            except (ApiError, ClientConnectorError) as err:
                _LOGGER.debug("/connect with stored refresh token failed: %s", err)
                self._refresh_token = None

        self._sessionid = await self._api.async_get_sessionid(
            self._username, self._password
        )
        self._persist_refresh_token()
        return self._sessionid

    def _persist_refresh_token(self) -> None:
        token = self._api.refresh_token
        if not token or token == self._refresh_token:
            return
        self._refresh_token = token
        if self._entry is None:
            return
        new_data = {**self._entry.data, CONF_REFRESH_TOKEN: token}
        self._hass.config_entries.async_update_entry(self._entry, data=new_data)

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            async with async_timeout.timeout(10):
                await self._async_ensure_session()

                expiration_timestamp = (
                    self._api.sessionid_timestamp + timedelta(days=1)
                )
                if expiration_timestamp <= datetime.now():
                    self._api.invalidate_session()
                    self._sessionid = None
                    await self._async_ensure_session()

                return await self._api.async_get_current_thermostats(self._sessionid)

        except InvalidSessionIdError:
            self._api.invalidate_session()
            self._sessionid = None
            try:
                await self._async_ensure_session()
                return await self._api.async_get_current_thermostats(self._sessionid)

            except InvalidUserPasswordError as err:
                raise ConfigEntryAuthFailed from err

            except (ApiError, ClientConnectorError) as err:
                raise UpdateFailed(err) from err

        except InvalidUserPasswordError as err:
            raise ConfigEntryAuthFailed from err

        except (ApiError, ClientConnectorError) as err:
            raise UpdateFailed(err) from err


@dataclass
class SchluterData:
    api: SchluterApi
    coordinator: SchluterDataUpdateCoordinator