import base64
import logging
import asyncio
import aiohttp
from yarl import URL
from collections import namedtuple

_LOGGER = logging.getLogger(__name__)

ATTR_TOKEN = "token"
ATTR_INSTALLED_APPS = "installed_apps"

WS_ENDPOINT_REMOTE_CONTROL = "/api/v2/channels/samsung.remote.control"
WS_ENDPOINT_APP_CONTROL = "/api/v2"

App = namedtuple("App", ["app_id", "app_name", "app_type"])


def serialize_string(string):
    if isinstance(string, str):
        string = str.encode(string)

    return base64.b64encode(string).decode("utf-8")


def format_websocket_url(host, path, name, token=None):
    url = URL.build(
        scheme="wss",
        host=host,
        port=8002,
        path=path,
        query={"name": serialize_string(name)},
    )

    if token:
        return str(url.update_query({"token": token}))
    return str(url)


def _noop(*args, **kwargs):
    pass


class MemoryDataStore(object):
    def __init__(self):
        self._data = {}

    async def get(self, key, default=None):
        return (await self.get_data()).get(key, default)

    async def set(self, key, value):
        await self.get_data()
        self._data[key] = value
        await self._save_to_store(self._data)

    async def get_data(self):
        if not self._data:
            self._data = await self._load_from_store()
        return self._data

    async def _load_from_store(self):
        return {}

    async def _save_to_store(self, data):
        pass


class TizenWebsocket:
    """Represent a websocket connection to a Tizen TV."""

    def __init__(
        self, name, host, data_store=None, session=None, app_changed_callback=None
    ):
        """Initialize a TizenWebsocket instance."""
        self.host = host
        self.name = name
        self.active = False
        self.session = session or aiohttp.ClientSession()
        self.key_press_delay = 0
        self._store = data_store or MemoryDataStore()
        self._app_changed = app_changed_callback or _noop
        self._current_task = None
        self._ws_remote = None
        self._ws_control = None

    def open(self, loop):
        self.active = True
        loop.create_task(self._open_remote())
        loop.create_task(self._open_control())

    def close(self):
        """Close the listening websocket."""
        _LOGGER.debug("Closing websocket connections")
        self.active = False
        if self._current_remote_task is not None:
            self._current_remote_task.cancel()
        if self._current_control_task is not None:
            self._current_control_task.cancel()

    async def _open_remote(self):
        """Open a persistent websocket connection and act on events."""
        _LOGGER.debug("Opening remote connection")
        url = format_websocket_url(
            self.host,
            WS_ENDPOINT_REMOTE_CONTROL,
            self.name,
            await self._store.get(ATTR_TOKEN),
        )
        self.remote_handshake_errors = 0
        while self.active:
            if self.remote_handshake_errors == 3:
                _LOGGER.error(
                    f"Remote: Handshake failed {self.handshake_errors} times, giving up"
                )
                self.active = False
                break
            _LOGGER.debug(f"Remote: Attempting connection to {url}")
            try:
                async with self.session.ws_connect(
                    url, heartbeat=15, ssl=False
                ) as ws_remote:
                    self._ws_remote = ws_remote
                    self._current_remote_task = asyncio.Task.current_task()
                    failed_attempts = 0
                    _LOGGER.info(f"Remote: Connection established")

                    async for msg in self._ws_remote:
                        _LOGGER.debug(f"Remote: Received message: {msg}")
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._on_message_remote(msg.json())
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                        ):
                            _LOGGER.info(
                                f"Remote: Connection closed by server (Code {msg.data} - {msg.extra})"
                            )
                            break
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            self.remote_handshake_errors += 1
                            _LOGGER.error(f"Remote: Received error {msg.data})")
                            break
            except aiohttp.client_exceptions.ClientConnectorError as e:
                retry_delay = min(2 ** (failed_attempts - 1), 300)
                _LOGGER.error(
                    "Remote: Connection refused, retrying in %ds: %s", retry_delay, e
                )
                await asyncio.sleep(retry_delay)
            else:
                _LOGGER.debug("Remote: disconnected")
            self._ws_remote = None
        _LOGGER.debug("Remote: stopped")

    async def _on_message_remote(self, msg):
        """Determine if messages relate to an interesting player event."""
        event = msg.get("event")

        if event == "ms.channel.connect":
            _LOGGER.debug("Remote: Handshake complete, host has confirmed connection")
            token = msg.get("data", {}).get(ATTR_TOKEN)
            if token:
                await self._store.set(ATTR_TOKEN, token)
            self.remote_handshake_errors = 0
            await self.request_installed_apps()
        elif event == "ed.installedApp.get":
            await self._handle_installed_apps(msg)
        elif event == "ed.edenTV.update":
            # self.get_running_app(force_scan=True)
            pass

    async def _open_control(self):
        """Open a persistent websocket connection and act on events."""
        _LOGGER.debug("Opening control connection")
        url = format_websocket_url(self.host, WS_ENDPOINT_APP_CONTROL, self.name)
        while self.active:
            _LOGGER.debug(f"Control: Attempting connection to {url}")
            try:
                async with self.session.ws_connect(
                    url, heartbeat=15, ssl=False
                ) as ws_control:
                    self._ws_control = ws_control
                    self._current_control_task = asyncio.Task.current_task()
                    failed_attempts = 0
                    _LOGGER.info(f"Control: Connection established")

                    async for msg in self._ws_control:
                        _LOGGER.debug(f"Control: Received message: {msg}")
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._on_message_control(msg.json())
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            _LOGGER.info(
                                f"Control: Connection closed by server (Code {msg.data} - {msg.extra})"
                            )
                            break
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            self.remote_handshake_errors += 1
                            _LOGGER.error(f"Control: Received error {msg.data})")
                            break
            except aiohttp.client_exceptions.ClientConnectorError as e:
                retry_delay = min(2 ** (failed_attempts - 1), 300)
                _LOGGER.error(
                    "Control: Connection refused, retrying in %ds: %s", retry_delay, e
                )
                await asyncio.sleep(retry_delay)
            else:
                _LOGGER.debug("Control: disconnected")
            self._ws_control = None
        _LOGGER.debug("Control: stopped")

    async def _on_message_control(self, msg):
        if msg.get("result"):
            app_id = msg.get("id")
            if app_id:
                self._app_changed(
                    (await self._store.get(ATTR_INSTALLED_APPS, {})).get(app_id)
                )

        event = msg.get("event")
        if event == "ms.channel.connect":
            _LOGGER.debug("Control: Handshake complete, host has confirmed connection")
            # await self.get_running_app()

    async def _handle_installed_apps(self, response):
        _LOGGER.debug("Got list of installed apps")
        list_app = response.get("data", {}).get("data")
        installed_apps = {}
        for app_info in list_app:
            app_id = app_info["appId"]
            app = App(app_id, app_info["name"], app_info["app_type"])
            installed_apps[app_id] = app
        await self._store.set(ATTR_INSTALLED_APPS, installed_apps)

    async def request_installed_apps(self):
        _LOGGER.debug("Requesting list of installed apps")
        try:
            await self._ws_remote.send_json(
                {
                    "method": "ms.channel.emit",
                    "params": {"event": "ed.installedApp.get", "to": "host"},
                }
            )
        except Exception:
            _LOGGER.error("Failed to request installed apps", exc_info=True)

    async def send_key(self, key, key_press_delay=None, cmd="Click"):
        _LOGGER.debug(f"Sending key {key}")
        try:
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
        except:
            _LOGGER.error(f"Failed to send key {key}", exc_info=True)
        else:
            if key_press_delay is None:
                await asyncio.sleep(self.key_press_delay)
            elif key_press_delay > 0:
                await asyncio.sleep(key_press_delay)

    async def run_app(self, app_id, action_type="", meta_tag=""):
        if not action_type:
            app = (await self._store.get(ATTR_INSTALLED_APPS, {})).get(app_id)
            action_type = "DEEP_LINK" if app and app.app_type == 2 else "NATIVE_LAUNCH"

        _LOGGER.debug(
            f"Running app {app.app_name} ({app_id} / {action_type} / {meta_tag})"
        )

        try:
            if action_type == "DEEP_LINK":
                await self._ws_control.send_json(
                    {
                        "id": app_id,
                        "method": "ms.application.start",
                        "params": {"id": app_id},
                    }
                )
            else:
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
        except Exception:
            _LOGGER.error(f"Failed to run app {app_id}", exc_info=True)
