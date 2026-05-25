[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![ha-schluter](https://img.shields.io/github/v/release/JakubOleksy/ha-schluter.svg?1)](https://github.com/JakubOleksy/ha-schluter) ![Maintenance](https://img.shields.io/maintenance/yes/2026.svg)

# About

This is a personal fork of [IngoS11/ha-schluter](https://github.com/IngoS11/ha-schluter) that has been rewritten to work with the modern Schluter cloud at `https://schluterditraheat.com/api`. The upstream project depended on the `aioschluter` PyPI package, which targets a legacy backend that Schluter has decommissioned, so the original integration no longer logs in.

This fork:

- Drops the `aioschluter` dependency and ships an internal `aiohttp`-based client.
- Talks directly to the same endpoints the official web app uses (`/login`, `/connect`, `/logout`, `/locations`, `/devices`, `/device/{id}/attribute`, `/device/{id}/status`).
- Handles the account's per-account concurrent session cap (`ACCSESSEXC`) by minting sessions sparingly and reusing them across Home Assistant restarts.
- Continues to support the [DITRA-HEAT-E-WiFi Thermostat](https://www.schluter.com/schluter-us/en_US/Floor-Warming/Schluter%AE-DITRA-HEAT-E-WiFi/p/product?productCode=DHERT104/BW) sold in North America. European thermostats use a different backend and are not supported.

## What changed compared to upstream

- **New API client** (`custom_components/schluter/api.py`) replacing `aioschluter`. No external runtime dependency.
- **Single login attempt** per setup. The previous code tried multiple payload/header variants which created several sessions per refresh and quickly tripped the server's 3-session-per-account limit (`ACCSESSEXC`).
- **Refresh-token reuse**. On first successful `/login` the integration captures the `refreshToken` and stores it in the config entry. On every subsequent HA restart it calls `POST /connect` with that token to mint a session id without consuming a new login slot.
- **Session id cached in the config entry** so that retries of `async_setup_entry` (which create a fresh API instance) do not re-authenticate.
- **Clean logout on unload**. Removing or reloading the integration calls `GET /logout` so the slot is released back to the account immediately.
- **Parallel device enrichment** (`asyncio.gather`) brings the first refresh down from ~10 s to ~2 s, which also fixed a timeout that was killing setup mid-way through.
- **60 s coordinator timeout** instead of the original 10 s.
- **Sensor warnings fixed**: the `*_price` sensors no longer combine `device_class=monetary` with `state_class=total_increasing`.
- **Repackaged** as a HACS commit-mode integration (no `zip_release`) under `JakubOleksy/ha-schluter` with `domain=schluter`, `version=0.4.x`.

## Getting Started

### Prerequisites

- Use Home Assistant v2024.5.3 or above.
- You need at least one configured [Schluter®-DITRA-HEAT-E-WiFi Thermostat](https://www.schluter.com/schluter-us/en_US/ditra-heat-wifi) in your home, and a working account at [https://schluterditraheat.com/](https://schluterditraheat.com/). The username and password you use to log in to that site are what you enter during integration setup.
- The integration installs into the `custom_components/schluter/` folder.

### HACS Installation

This integration overwrites the standard Schluter integration and is, therefore [not accepted into the default HACS repository](https://hacs.xyz/docs/publish/include). To use the integration with HACS you have to add this repository. Under HACS select Integrations in the overflow menu (three dots in the upper right corner) select `Custom repositories` paste the URL, `https://github.com/JakubOleksy/ha-schluter`, into the `repository` field and select Integration as the Category.

### Manual Installation

1. Open the directory with your Home Assistant configuration (where you find `configuration.yaml`,
   usually `~/.homeassistant/`).
2. If you do not have a `custom_components` directory there, you need to create it.

#### Git clone method

```shell
git clone https://github.com/JakubOleksy/ha-schluter.git
ln -s ha-schluter/custom_components/schluter ~/.homeassistant/custom_components/schluter
```

Then `git pull origin main` to update.

#### Copy method

1. Download [ZIP](https://github.com/JakubOleksy/ha-schluter/archive/main.zip) with the code.
2. Unpack it.
3. Copy `custom_components/schluter/` from the unpacked archive to `custom_components/` in your Home Assistant configuration directory.

### Integration Setup

- Browse to your Home Assistant instance.
- In the sidebar click on [Configuration](https://my.home-assistant.io/redirect/config).
- From the configuration menu select: [Integrations](https://my.home-assistant.io/redirect/integrations).
- In the bottom right, click on the [Add Integration](https://my.home-assistant.io/redirect/config_flow_start?domain=schluter) button.
- From the list, search and select "Schluter DITRA-HEAT (Jakub Custom)".
- Enter the same email/password you use at [schluterditraheat.com](https://schluterditraheat.com/).
- After completing, the integration will create a `climate.*` entity per thermostat plus current temperature, target temperature, power, energy, and price sensors.

## Troubleshooting

### `ACCSESSEXC` on first setup

The Schluter backend rejects new logins with `ACCSESSEXC` when the account already has 3 active sessions. This can happen if other devices (web dashboard, mobile app, an earlier failed HA install) still hold a session.

To free a slot:

1. Log out of [schluterditraheat.com](https://schluterditraheat.com/) in your browser.
2. Log out of the Schluter mobile app, if installed.
3. Wait a few minutes for stale sessions to expire on the server.

Once the integration logs in successfully once, the refresh token is cached and subsequent HA restarts will not consume new login slots.

### Enabling debug logs

Add to your `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.schluter: debug
    custom_components.schluter.api: debug
```

## Development

The original integration was scaffolded from the [dev container template](https://github.com/ludeeus/integration_blueprint) by [Joakim Sorensen](https://github.com/ludeeus). This fork keeps the same layout.

A local credential probe is included at `scripts/probe_login.py`. Copy `scripts/secrets.json.example` to `scripts/secrets.json` (gitignored), fill in your credentials, and run:

```shell
python3 -m venv .probe-venv
.probe-venv/bin/pip install aiohttp yarl
.probe-venv/bin/python scripts/probe_login.py
```

The probe exercises the full lifecycle (`/login` → `/connect` → `/logout`) against the live backend and prints the discovered thermostats, without going through Home Assistant.

## Known Issues

- No UI affordance to update the password from the Integrations view. Remove and re-add the entry to change credentials.
- The Schluter backend enforces a per-account limit of 3 concurrent sessions. See troubleshooting above.
