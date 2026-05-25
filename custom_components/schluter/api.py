"""API client for Schluter Smart Thermostat web backend."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
from typing import Any

from aiohttp import ClientResponse, ClientSession
from yarl import URL

_LOGGER = logging.getLogger(__name__)

API_BASE_URL = "https://schluterditraheat.com/api"

REGULATION_MODE_AWAY = "away"
REGULATION_MODE_MANUAL = "manual"
REGULATION_MODE_SCHEDULE = "schedule"


class ApiError(Exception):
    """Raised when the API call failed."""


class InvalidUserPasswordError(ApiError):
    """Raised when credentials are invalid."""


class InvalidSessionIdError(ApiError):
    """Raised when the session token is invalid or expired."""


@dataclass
class Thermostat:
    """Normalized thermostat model consumed by entities."""

    thermostat_id: str
    serial_number: str
    name: str
    sw_version: str
    temperature: float
    set_point_temp: float
    min_temp: float
    max_temp: float
    is_heating: bool
    load_measured_watt: int
    kwh_charge: float
    is_online: bool
    regulation_mode: str
    raw: dict[str, Any]


class SchluterApi:
    """Client for Schluter web endpoints used by the SPA."""

    def __init__(self, websession: ClientSession, base_url: str = API_BASE_URL) -> None:
        self._session = websession
        self._base_url = base_url.rstrip("/")
        self._sessionid: str | None = None
        self._sessionid_timestamp: datetime | None = None

    @property
    def sessionid(self) -> str:
        """Return active session id."""
        if self._sessionid is None:
            raise InvalidSessionIdError("Session is not initialized")
        return self._sessionid

    @property
    def sessionid_timestamp(self) -> datetime:
        """Return timestamp of current session acquisition."""
        return self._sessionid_timestamp or datetime.now()

    async def async_get_sessionid(self, username: str, password: str) -> str:
        """Authenticate and obtain session id."""
        requester = {
            "web-app": {
                "interface": "schluter",
                "app-version": "ha-custom-integration",
            }
        }
        headers = {
            "Content-Type": "application/json",
            "SWS-Requester": json.dumps(requester, separators=(",", ":")),
        }
        payload = {
            "username": username,
            "password": password,
            "interface": "schluter",
            "stayConnected": True,
        }

        data, response = await self._request(
            "POST",
            "/login",
            headers=headers,
            json_data=payload,
            include_session=False,
        )

        session_id = self._extract_session_id(data, response)
        if not session_id:
            raise ApiError("Login succeeded but no session id found")

        self._sessionid = session_id
        self._sessionid_timestamp = datetime.now()
        return session_id

    async def async_get_current_thermostats(self, sessionid: str) -> dict[str, Thermostat]:
        """Fetch and normalize thermostat data."""
        self._sessionid = sessionid
        payload, _ = await self._request("GET", "/devices")
        devices = self._extract_device_list(payload)
        thermostats = {
            item.thermostat_id: item
            for item in (self._to_thermostat(device) for device in devices)
            if item is not None
        }
        if not thermostats:
            raise ApiError("No compatible thermostats found in API response")
        return thermostats

    async def async_set_temperature(
        self, sessionid: str, serialnumber: str, target_temp: float
    ) -> None:
        """Set target thermostat temperature."""
        self._sessionid = sessionid
        errors: list[Exception] = []
        attempts: list[tuple[str, str, dict[str, Any]]] = [
            ("PUT", f"/device/{serialnumber}", {"setPointTemp": target_temp}),
            ("PUT", f"/device/{serialnumber}", {"setpoint": target_temp}),
            (
                "POST",
                f"/device/{serialnumber}/attribute",
                {"name": "setPointTemp", "value": target_temp},
            ),
            (
                "POST",
                f"/device/{serialnumber}/attribute",
                {"attribute": "setpoint", "value": target_temp},
            ),
        ]

        for method, path, payload in attempts:
            try:
                await self._request(method, path, json_data=payload)
                return
            except ApiError as err:
                errors.append(err)

        raise ApiError(f"Unable to set temperature for {serialnumber}: {errors[-1]}")

    async def async_set_regulation_mode(
        self, sessionid: str, serialnumber: str, regulation_mode: str
    ) -> None:
        """Set thermostat regulation mode."""
        self._sessionid = sessionid
        errors: list[Exception] = []
        attempts: list[tuple[str, str, dict[str, Any]]] = [
            ("PUT", f"/device/{serialnumber}", {"regulationMode": regulation_mode}),
            ("PUT", f"/device/{serialnumber}", {"mode": regulation_mode}),
            ("PUT", f"/device/{serialnumber}", {"setpointMode": regulation_mode}),
            (
                "POST",
                f"/device/{serialnumber}/attribute",
                {"name": "regulationMode", "value": regulation_mode},
            ),
            (
                "POST",
                f"/device/{serialnumber}/attribute",
                {"attribute": "mode", "value": regulation_mode},
            ),
        ]

        for method, path, payload in attempts:
            try:
                await self._request(method, path, json_data=payload)
                return
            except ApiError as err:
                errors.append(err)

        raise ApiError(f"Unable to set mode for {serialnumber}: {errors[-1]}")

    async def _request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        json_data: dict[str, Any] | None = None,
        include_session: bool = True,
    ) -> tuple[dict[str, Any], ClientResponse]:
        url = str(URL(self._base_url + "/").join(URL(path.lstrip("/"))))
        request_headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if headers:
            request_headers.update(headers)
        if include_session and self._sessionid:
            request_headers.setdefault("session-id", self._sessionid)
            request_headers.setdefault("x-session-id", self._sessionid)

        async with self._session.request(
            method,
            url,
            headers=request_headers,
            json=json_data,
        ) as response:
            data = await self._read_json(response)
            self._raise_for_error(data, response)
            return data, response

    async def _read_json(self, response: ClientResponse) -> dict[str, Any]:
        try:
            payload = await response.json(content_type=None)
            return payload if isinstance(payload, dict) else {"data": payload}
        except Exception:
            text = await response.text()
            _LOGGER.debug("Non-JSON response from Schluter API: %s", text)
            return {}

    def _raise_for_error(self, data: dict[str, Any], response: ClientResponse) -> None:
        code = _extract_error_code(data)

        if response.status in (401, 403) or code in {"USRSESSEXP", "USRINVSESSION"}:
            raise InvalidSessionIdError(code or f"HTTP {response.status}")

        if code in {"USRINVPWD", "USRNOTFOUND", "AUTHFAILED"}:
            raise InvalidUserPasswordError(code)

        if response.status >= 400:
            raise ApiError(code or f"HTTP {response.status}")

        if code and code != "None":
            raise ApiError(code)

    def _extract_session_id(
        self, data: dict[str, Any], response: ClientResponse
    ) -> str | None:
        for key in ("sessionId", "sessionid", "session", "SessionId"):
            value = _find_key(data, key)
            if isinstance(value, str) and value:
                return value

        for header in ("session-id", "x-session-id"):
            value = response.headers.get(header)
            if value:
                return value

        for cookie_name, cookie in response.cookies.items():
            if "session" in cookie_name.lower() and cookie.value:
                return cookie.value

        return None

    def _extract_device_list(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data", payload)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]

        if isinstance(data, dict):
            for key in ("devices", "items", "results"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]

        devices: list[dict[str, Any]] = []

        def _walk(value: Any) -> None:
            if isinstance(value, dict):
                if _looks_like_device(value):
                    devices.append(value)
                for nested in value.values():
                    _walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    _walk(nested)

        _walk(payload)
        return devices

    def _to_thermostat(self, device: dict[str, Any]) -> Thermostat | None:
        if not _is_thermostat(device):
            return None

        thermostat_id = str(
            _pick(
                device,
                "deviceId",
                "id",
                "serialNumber",
                "serial",
                "uuid",
            )
            or ""
        )
        if not thermostat_id:
            return None

        name = str(_pick(device, "name", "deviceName", "label") or thermostat_id)
        sw_version = str(_pick(device, "swVersion", "firmwareVersion", "version") or "")

        current_temp = float(
            _pick(
                device,
                "temperature",
                "currentTemperature",
                "ambientTemperature",
                "floorTemperature",
                "temp",
                default=0.0,
            )
            or 0.0
        )
        set_temp = float(
            _pick(
                device,
                "setPointTemp",
                "setpoint",
                "setPoint",
                "targetTemperature",
                "setpointTemperature",
                default=current_temp,
            )
            or current_temp
        )
        min_temp = float(_pick(device, "minTemp", "minimumTemperature", default=5.0) or 5.0)
        max_temp = float(_pick(device, "maxTemp", "maximumTemperature", default=35.0) or 35.0)

        is_heating = bool(_pick(device, "isHeating", "heating", "heatingOn", default=False))
        load_watt = int(
            _pick(device, "loadMeasuredWatt", "power", "watt", "wattage", default=0) or 0
        )
        kwh_charge = float(_pick(device, "kwhCharge", "kWhCharge", "energyPrice", default=0.0) or 0.0)

        online_raw = _pick(device, "isOnline", "online", "connected", default=True)
        is_online = bool(online_raw)
        if "offline" in device:
            is_online = not bool(device.get("offline"))

        mode = _normalize_regulation_mode(
            _pick(device, "regulationMode", "mode", "setpointMode", "controlMode")
        )

        return Thermostat(
            thermostat_id=thermostat_id,
            serial_number=str(
                _pick(device, "serialNumber", "serial", "deviceId", "id", default=thermostat_id)
            ),
            name=name,
            sw_version=sw_version,
            temperature=current_temp,
            set_point_temp=set_temp,
            min_temp=min_temp,
            max_temp=max_temp,
            is_heating=is_heating,
            load_measured_watt=load_watt,
            kwh_charge=kwh_charge,
            is_online=is_online,
            regulation_mode=mode,
            raw=device,
        )


def _pick(obj: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in obj and obj[key] is not None:
            return obj[key]
    return default


def _extract_error_code(payload: dict[str, Any]) -> str | None:
    err = payload.get("error")
    if isinstance(err, dict):
        code = err.get("code")
        if isinstance(code, str):
            return code
    return None


def _find_key(value: Any, key: str) -> Any:
    key_lc = key.lower()
    if isinstance(value, dict):
        for k, nested in value.items():
            if k.lower() == key_lc:
                return nested
            found = _find_key(nested, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_key(nested, key)
            if found is not None:
                return found
    return None


def _looks_like_device(value: dict[str, Any]) -> bool:
    return any(
        candidate in value
        for candidate in (
            "deviceId",
            "serialNumber",
            "deviceName",
            "currentTemperature",
            "setPointTemp",
        )
    )


def _is_thermostat(device: dict[str, Any]) -> bool:
    type_value = str(
        _pick(device, "type", "deviceType", "category", "productType", default="")
    ).lower()
    if any(token in type_value for token in ("thermostat", "heat", "ditra", "floor")):
        return True

    return any(
        key in device
        for key in (
            "setPointTemp",
            "setpoint",
            "targetTemperature",
            "currentTemperature",
            "ambientTemperature",
        )
    )


def _normalize_regulation_mode(raw_mode: Any) -> str:
    if raw_mode is None:
        return REGULATION_MODE_MANUAL

    value = str(raw_mode).strip().lower()
    if any(token in value for token in ("sched", "auto", "program")):
        return REGULATION_MODE_SCHEDULE
    if any(token in value for token in ("away", "off", "eco")):
        return REGULATION_MODE_AWAY
    return REGULATION_MODE_MANUAL
