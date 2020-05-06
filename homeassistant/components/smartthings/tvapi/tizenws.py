import base64
import logging
import asyncio
import aiohttp
from yarl import URL
from collections import namedtuple

_LOGGER = logging.getLogger(__name__)

WS_ENDPOINT_REMOTE_CONTROL = "/api/v2/channels/samsung.remote.control"
WS_ENDPOINT_APP_CONTROL = "/api/v2"

App = namedtuple("App", ["app_id", "app_name", "app_type"])


def serialize_string(string):
    if isinstance(string, str):
        string = str.encode(string)

    return base64.b64encode(string).decode("utf-8")


def format_websocket_url(host, name, token):
    url = URL.build(
        scheme="wss",
        host=host,
        port=8002,
        path=WS_ENDPOINT_REMOTE_CONTROL,
        query={"name": serialize_string(name)},
    )

    if token:
        return str(url.update_query({"token": token}))
    return str(url)


class TizenWebsocket:
    """Represent a websocket connection to a Tizen TV."""

    def __init__(self, name, host, token_file, session=None):
        """Initialize a TizenWebsocket instance."""
        self.active = False
        self.session = session or aiohttp.ClientSession()
        self.installed_apps = {}
        self.key_press_delay = 0
        self._token_file = token_file
        self._current_task = None
        self._ws_remote = None
        self._ws_control = None
        self._token = None
        self.url = format_websocket_url(host, name, self.token)

    @property
    def token(self):
        if self._token is None:
            try:
                with open(self._token_file, "r") as token_file:
                    self._token = token_file.readline()
            except (TypeError, FileNotFoundError):
                _LOGGER.error("Could not load token from file", exc_info=True)
            else:
                _LOGGER.debug(f"Loaded token from {self._token_file}")
        else:
            _LOGGER.debug("Got token from memory")
        return self._token

    @token.setter
    def token(self, token):
        _LOGGER.info(f"Storing new token {token}")
        self._token = token
        try:
            with open(self._token_file, "w") as token_file:
                token_file.write(self._token)
        except FileNotFoundError:
            _LOGGER.error("Could not save token to file", exc_info=True)
        else:
            _LOGGER.debug("Saved token to file")

    async def open(self):
        """Open a persistent websocket connection and act on events."""
        _LOGGER.debug("Opening TizenWS connection")
        self.active = True
        failed_attempts = 0
        while self.active:
            if failed_attempts == 3:
                self.active = False
                break

            _LOGGER.debug(f"Attempting websocket connection ({failed_attempts}) to {self.url}")
            try:
                async with self.session.ws_connect(
                    self.url, heartbeat=15, ssl=False
                ) as ws_remote:
                    self._ws_remote = ws_remote
                    self._current_task = asyncio.Task.current_task()
                    _LOGGER.info(f"Websocket to {self.url} established")

                    async for msg in self._ws_remote:
                        _LOGGER.debug(f"Received websocket message: {msg}")
                        # if msg.type == aiohttp.WSMSgType.PING:
                        #     await ws_remote.send_pong()
                        # elif msg.type == aiohttp.WSMSgType.PONG:
                        #     await ws_remote.send_ping()
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._on_message(msg.json())
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            _LOGGER.info(
                                f"Websocket connection closed by server (Code {msg.data} - {msg.extra})"
                            )
                            break
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            raise aiohttp.client_exceptions.ClientResponseError(
                                f"Received an error: {msg.data}"
                            )
            except aiohttp.client_exceptions.ClientConnectorError as e:
                retry_delay = min(2 ** (failed_attempts - 1) * 5, 300)
                failed_attempts += 1
                self._ws_remote = None
                _LOGGER.error(
                    "Websocket connection refused, retrying in %ds: %s", retry_delay, e
                )
                await asyncio.sleep(retry_delay)
            except aiohttp.client_exceptions.ClientResponseError as e:
                retry_delay = min(2 ** (failed_attempts - 1) * 5, 300)
                failed_attempts += 1
                self._ws_remote = None
                _LOGGER.error(
                    "Websocket connection failed, retrying in %ds: %s", retry_delay, e
                )
                await asyncio.sleep(retry_delay)
            else:
                self._ws_remote = None
                failed_attempts = 0
                _LOGGER.debug("Websocket disconnected")
        _LOGGER.debug("Websocket connection closed")

    async def _on_message(self, msg):
        """Determine if messages relate to an interesting player event."""
        event = msg.get("event")
        if not event:
            return

        if event == "ms.channel.connect":
            _LOGGER.debug("Handshake complete, host has confirmed connection")
            token = msg.get("data", {}).get("token")
            if token:
                self.token = token
            await self._request_apps_list()
        elif event == "ed.installedApp.get":
            self._handle_installed_app(msg)
        elif event == "ed.edenTV.update":
            # self.get_running_app(force_scan=True)
            pass

    async def _request_apps_list(self):
        _LOGGER.debug("Requesting list of installed apps")
        await self._ws_remote.send_json(
            {
                "method": "ms.channel.emit",
                "params": {"event": "ed.installedApp.get", "to": "host"},
            }
        )

    def _handle_installed_app(self, response):
        _LOGGER.debug("Got list of installed apps")
        list_app = response.get("data", {}).get("data")
        installed_apps = {}
        for app_info in list_app:
            app_id = app_info["appId"]
            app = App(app_id, app_info["name"], app_info["app_type"])
            _LOGGER.debug("Found app: %s", app)
            installed_apps[app_id] = app
        self.installed_apps = installed_apps

    def close(self):
        """Close the listening websocket."""
        _LOGGER.debug("Closing websocket connection")
        self.active = False
        if self._current_task is not None:
            self._current_task.cancel()

    async def send_key(self, key, key_press_delay=None, cmd="Click"):
        if not self._ws_remote:
            _LOGGER.error("Cannot send key, not connected")
            return

        await self._ws_remote.send_json(
            {
                "method": "ms.remote.control",
                "params": {
                    "Cmd": cmd,
                    "DataOfCmd": key,
                    "Option": "false",
                    "TypeOfRemote": "SendRemoteKey",
                },
            }
        )

        if key_press_delay is None:
            await asyncio.sleep(self.key_press_delay)
        elif key_press_delay > 0:
            await asyncio.sleep(key_press_delay)

    async def run_app(self, app_id, action_type="", meta_tag=""):
        if not action_type:
            app = self.installed_apps.get(app_id)
            action_type = "DEEP_LINK" if app and app.app_type == 2 else "NATIVE_LAUNCH"

        _LOGGER.debug(
            "Sending run app app_id: %s app_type: %s meta_tag: %s",
            app_id,
            action_type,
            meta_tag,
        )

        if self._ws_control and action_type == "DEEP_LINK":
            await self._ws_control.send_json(
                {
                    "id": app_id,
                    "method": "ms.application.start",
                    "params": {"id": app_id},
                }
            )
        elif self._ws_remote:
            await self._ws_remote.send_json(
                {
                    "method": "ms.channel.emit",
                    "params": {
                        "event": "ed.apps.launch",
                        "to": "host",
                        "data": {
                            "action_type": action_type,
                            "appId": app_id,
                            "metaTag": meta_tag,
                        },
                    },
                }
            )
        else:
            _LOGGER.error("Cannot send key, not connected")
