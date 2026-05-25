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
API_BASE_URL_FALLBACKS = (
    "https://schluterditraheat.com/api",
    "https://neviweb.com/api",
)
DEFAULT_APP_VERSION = "1.13.2"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)

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
        self._app_version: str = DEFAULT_APP_VERSION
        self._account_id: str | None = None

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
        """Authenticate and obtain a session id.

        The Schluter/Neviweb backend enforces a per-account concurrent session
        limit and returns ``ACCSESSEXC`` once it is exceeded. To avoid quickly
        exhausting that limit we make a single login attempt and reuse the
        resulting session across refresh cycles.
        """
        if self._sessionid:
            return self._sessionid

        payload = {
            "username": username,
            "password": password,
            "interface": "schluter",
            "stayConnected": True,
        }

        await self._async_update_app_version()
        _LOGGER.debug("Login attempt using base URL: %s", self._base_url)

        requester_header = self._build_requester_header(app_version=self._app_version)
        try:
            data, response = await self._request(
                "POST",
                "/login",
                headers={
                    "Content-Type": "application/json",
                    "SWS-Requester": requester_header,
                },
                json_data=payload,
                include_session=False,
                requester_header=requester_header,
            )
        except ApiError as err:
            _LOGGER.debug("Login rejected on %s with error: %s", self._base_url, err)
            raise

        _LOGGER.debug("Login successful on %s", self._base_url)

        session_id = self._extract_session_id(data, response)
        if not session_id:
            raise ApiError("Login succeeded but no session id found")

        account_id = None
        if isinstance(data.get("account"), dict):
            account_id = data["account"].get("id")
        if account_id is not None:
            self._account_id = str(account_id)

        self._sessionid = session_id
        self._sessionid_timestamp = datetime.now()
        return session_id

    def invalidate_session(self) -> None:
        """Discard cached session so the next call re-authenticates."""
        self._sessionid = None
        self._sessionid_timestamp = None

    async def async_get_current_thermostats(self, sessionid: str) -> dict[str, Thermostat]:
        """Fetch and normalize thermostat data."""
        self._sessionid = sessionid
        devices = await self._async_get_devices_for_locations()

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
            ("PUT", f"/device/{serialnumber}/attribute", {"roomSetpoint": target_temp}),
            ("PUT", f"/device/{serialnumber}/attribute", {"setPointTemp": target_temp}),
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
        api_mode = _to_api_setpoint_mode(regulation_mode)
        errors: list[Exception] = []
        attempts: list[tuple[str, str, dict[str, Any]]] = [
            ("PUT", f"/device/{serialnumber}/attribute", {"setpointMode": api_mode}),
            ("PUT", f"/device/{serialnumber}", {"regulationMode": api_mode}),
            ("PUT", f"/device/{serialnumber}", {"mode": api_mode}),
            ("PUT", f"/device/{serialnumber}", {"setpointMode": api_mode}),
            (
                "POST",
                f"/device/{serialnumber}/attribute",
                {"name": "setpointMode", "value": api_mode},
            ),
            (
                "POST",
                f"/device/{serialnumber}/attribute",
                {"attribute": "mode", "value": api_mode},
            ),
        ]

        for method, path, payload in attempts:
            try:
                await self._request(method, path, json_data=payload)
                return
            except ApiError as err:
                errors.append(err)

        raise ApiError(f"Unable to set mode for {serialnumber}: {errors[-1]}")

    async def _async_get_devices_for_locations(self) -> list[dict[str, Any]]:
        """Discover devices and merge runtime attributes for thermostat entities."""
        locations_path = "/locations"
        if self._account_id:
            locations_path += f"?account$id={self._account_id}"

        locations_payload, _ = await self._request("GET", locations_path)
        locations = self._extract_location_ids(locations_payload)
        if not locations and self._account_id:
            account_devices, _ = await self._request("GET", f"/devices?account$id={self._account_id}")
            base_devices = self._extract_device_list(account_devices)
            return await self._async_enrich_devices(base_devices)

        base_devices: list[dict[str, Any]] = []
        for location_id in locations:
            try:
                payload, _ = await self._request("GET", f"/devices?location$id={location_id}")
            except ApiError:
                continue
            base_devices.extend(self._extract_device_list(payload))

        if not base_devices and self._account_id:
            account_devices, _ = await self._request("GET", f"/devices?account$id={self._account_id}")
            base_devices = self._extract_device_list(account_devices)

        if not base_devices:
            payload, _ = await self._request("GET", "/devices")
            base_devices = self._extract_device_list(payload)

        return await self._async_enrich_devices(base_devices)

    async def _async_enrich_devices(self, base_devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fetch live attributes for each device and merge them into a single model."""
        attr_query = (
            "setpointMode,roomSetpoint,roomSetpointMin,roomSetpointMax,"
            "roomTemperatureDisplay,outputPercentDisplay,occupancyMode,"
            "gfciStatus,floorSetpointPwm,floorSetpointPwmMin,floorSetpointPwmMax,airFloorMode"
        )

        enriched: list[dict[str, Any]] = []
        for device in base_devices:
            device_id = _pick(device, "id", "deviceId")
            if device_id is None:
                continue

            merged = dict(device)
            try:
                attrs_payload, _ = await self._request(
                    "GET",
                    f"/device/{device_id}/attribute?attributes={attr_query}",
                )
                attrs = attrs_payload.get("data", attrs_payload)
                if isinstance(attrs, dict):
                    merged.update(attrs)
            except ApiError:
                pass

            try:
                status_payload, _ = await self._request("GET", f"/device/{device_id}/status")
                status_data = status_payload.get("data", status_payload)
                if isinstance(status_data, dict):
                    merged.update(status_data)
            except ApiError:
                pass

            enriched.append(merged)

        return enriched

    async def _request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        json_data: dict[str, Any] | None = None,
        include_session: bool = True,
        requester_header: str | None = None,
    ) -> tuple[dict[str, Any], ClientResponse]:
        url = str(URL(self._base_url + "/").join(URL(path.lstrip("/"))))
        base_origin = URL(self._base_url).origin()
        referer = str(URL(base_origin).join(URL("/login")))
        request_headers: dict[str, str] = {
            "Content-Type": "application/json",
            "SWS-Requester": requester_header
            or self._build_requester_header(app_version=self._app_version),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": str(base_origin),
            "Referer": referer,
            "User-Agent": BROWSER_USER_AGENT,
        }
        if headers:
            request_headers.update(headers)
        if include_session and self._sessionid:
            request_headers.setdefault("Session-Id", self._sessionid)
            request_headers.setdefault("session-id", self._sessionid)
            request_headers.setdefault("x-session-id", self._sessionid)

        async with self._session.request(
            method,
            url,
            headers=request_headers,
            json=json_data,
        ) as response:
            data = await self._read_json(response)
            _LOGGER.debug(
                "Schluter API call %s %s -> status=%s code=%s",
                method,
                path,
                response.status,
                _extract_error_code(data),
            )
            self._raise_for_error(data, response)
            return data, response

    async def _async_update_app_version(self) -> None:
        """Fetch web-app version used by the official frontend."""
        version_url = "https://schluterditraheat.com/assets/version.json"
        try:
            async with self._session.get(version_url) as response:
                payload = await response.json(content_type=None)
                version = payload.get("version") if isinstance(payload, dict) else None
                if isinstance(version, str) and version.strip():
                    self._app_version = version.strip()
        except Exception:
            # Keep default version if the endpoint is unavailable.
            self._app_version = self._app_version or DEFAULT_APP_VERSION

    def _build_requester_header(
        self,
        app_version: str,
        include_mobile: bool = False,
    ) -> str:
        requester: dict[str, Any] = {
            "web-app": {
                "interface": "schluter",
                "app-version": app_version,
            }
        }
        if include_mobile:
            requester["mobile"] = {
                "interface": "schluter",
                "platform": "web",
            }
        return json.dumps(requester, separators=(",", ":"))

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

        if code in {"ACCSESSEXC"}:
            raise ApiError(code)

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

        for header in ("Session-Id", "session-id", "x-session-id"):
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

    def _extract_location_ids(self, payload: dict[str, Any]) -> list[str]:
        """Extract location identifiers from heterogeneous API responses."""
        data = payload.get("data", payload)
        candidates: list[str] = []

        def _walk(value: Any) -> None:
            if isinstance(value, dict):
                location_id = _pick(value, "locationId", "id")
                if location_id is not None and (
                    "location" in value
                    or "address" in value
                    or "name" in value
                    or "timezone" in value
                ):
                    candidates.append(str(location_id))
                for nested in value.values():
                    _walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    _walk(nested)

        _walk(data)
        return list(dict.fromkeys(candidates))

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
        sw_version = str(
            _pick(
                device,
                "swVersion",
                "firmwareVersion",
                "version",
                default=(device.get("signature", {}).get("softVersion") if isinstance(device.get("signature"), dict) else ""),
            )
            or ""
        )
        if isinstance(device.get("signature"), dict):
            soft = device["signature"].get("softVersion")
            if isinstance(soft, dict):
                sw_version = f"{soft.get('major', 0)}.{soft.get('middle', 0)}.{soft.get('minor', 0)}"

        room_temp_obj = _pick(device, "roomTemperatureDisplay")
        if isinstance(room_temp_obj, dict):
            room_temp = room_temp_obj.get("value")
        else:
            room_temp = None

        current_temp = float(
            _pick(
                device,
                "roomTemperatureDisplayValue",
                "temperature",
                "currentTemperature",
                "ambientTemperature",
                "floorTemperature",
                "temp",
                default=room_temp if room_temp is not None else 0.0,
            )
            or (room_temp if room_temp is not None else 0.0)
        )
        set_temp = float(
            _pick(
                device,
                "roomSetpoint",
                "setPointTemp",
                "setpoint",
                "setPoint",
                "targetTemperature",
                "setpointTemperature",
                default=current_temp,
            )
            or current_temp
        )
        min_temp = float(
            _pick(device, "roomSetpointMin", "minTemp", "minimumTemperature", default=5.0)
            or 5.0
        )
        max_temp = float(
            _pick(device, "roomSetpointMax", "maxTemp", "maximumTemperature", default=35.0)
            or 35.0
        )

        output = _pick(device, "outputPercentDisplay")
        output_percent = 0
        if isinstance(output, dict):
            output_percent = int(output.get("percent", 0) or 0)

        is_heating = bool(
            _pick(device, "isHeating", "heating", "heatingOn", default=False)
        ) or output_percent > 0
        load_watt = int(
            _pick(device, "loadMeasuredWatt", "power", "watt", "wattage", default=0) or 0
        )
        kwh_charge = float(_pick(device, "kwhCharge", "kWhCharge", "energyPrice", default=0.0) or 0.0)

        online_raw = _pick(device, "isOnline", "online", "connected", default=True)
        status_value = str(_pick(device, "status", default="")).lower()
        is_online = bool(online_raw)
        if "offline" in device:
            is_online = not bool(device.get("offline"))
        if status_value:
            is_online = status_value == "online"

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
            "identifier",
            "serialNumber",
            "deviceName",
            "currentTemperature",
            "setPointTemp",
            "family",
            "sku",
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
            "family",
            "airFloorMode",
            "roomSetpoint",
            "roomTemperatureDisplay",
            "outputPercentDisplay",
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


def _to_api_setpoint_mode(mode: str) -> str:
    value = str(mode).strip().lower()
    if value == REGULATION_MODE_SCHEDULE:
        return "auto"
    if value == REGULATION_MODE_AWAY:
        return "away"
    return "manual"
