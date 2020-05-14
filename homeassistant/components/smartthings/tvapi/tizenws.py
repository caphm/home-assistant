import base64
import logging
import asyncio
import aiofiles
import aiohttp
import json
from yarl import URL
from collections import namedtuple

_LOGGER = logging.getLogger(__name__)

ATTR_TOKEN = "token"
ATTR_INSTALLED_APPS = "installed_apps"

WS_CONTROL = "control"
WS_REMOTE = "remote"

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


async def retry_delay(num_errors):
    retry_delay = min(2 ** (num_errors - 1) * 5, 300)
    _LOGGER.debug(f"Retrying in {retry_delay}s")
    await asyncio.sleep(retry_delay)


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
        try:
            async with aiofiles.open('token.txt', mode='r') as f:
                data = await f.read()
                return json.loads(data)
        except Exception as exc:
            _LOGGER.error(f"Failed to read storage file: {exc}")
            return {}

    async def _save_to_store(self, data):
        try:
            async with aiofiles.open('token.txt', mode='w') as f:
                await f.write(json.dumps(data))
        except Exception as exc:
            _LOGGER.error(f"Failed to write storage file: {exc}")


class AuthorizationError(Exception):
    def __init__(self):
        super().__init__("TV refused authorization (ms.channel.aunauthorized)")


class TizenWebsocket:
    """Represent a websocket connection to a Tizen TV."""

    def __init__(
        self,
        name,
        host,
        create_task,
        data_store=None,
        session=None,
        update_callback=None,
        token=None,
    ):
        """Initialize a TizenWebsocket instance."""
        self.host = host
        self.name = name
        self.session = session or aiohttp.ClientSession()
        self.key_press_delay = 0
        self.current_app = None
        self.installed_apps = {}
        self._store = data_store or MemoryDataStore()
        self._update = update_callback or _noop
        self._found_running_app = False
        self._ws_remote = None
        self._ws_control = None
        self._create_task = create_task
        self._connected = False
        self._remote_task = None
        self._control_task = None
        self._app_monitor_task = None

    @property
    def connected(self):
        return self._connected

    def open(self, ):
        _LOGGER.debug("Open websocket connections")
        self._remote_task = self._create_task(self._open_connection(WS_REMOTE))

    def close(self):
        """Close the listening websocket."""
        _LOGGER.debug("Closing websocket connections")
        if self._remote_task:
            self._remote_task.cancel()
        if self._control_task:
            self._control_task.cancel()
        if self._app_monitor_task:
            self._app_monitor_task.cancel()

    async def _open_connection(self, conn_name):
        """Open a persistent websocket connection and act on events."""
        path = WS_ENDPOINT_REMOTE_CONTROL if conn_name == WS_REMOTE else WS_ENDPOINT_APP_CONTROL
        token = (await self._store.get(ATTR_TOKEN)) if conn_name == WS_REMOTE else None
        url = format_websocket_url(self.host, path, self.name, token)
        _LOGGER.debug(f"{conn_name}: Attempting connection to {url}")
        try:
            async with self.session.ws_connect(url, ssl=False) as ws:
                setattr(self, f"_ws_{conn_name}", ws)
                _LOGGER.debug(f"{conn_name}: Connection established")

                async for msg in ws:
                    try:
                        await self._handle_message(conn_name, msg)
                    except Exception as exc:
                        _LOGGER.error(f"Error while handling message: {exc}", exc_info=True)
        except (aiohttp.ClientConnectionError, aiohttp.WebSocketError) as exc:
            _LOGGER.error(f"{conn_name}: Connection error: {exc}")
        except AuthorizationError:
            _LOGGER.error(f"{conn_name}: Authorization refused")
        except asyncio.CancelledError:
            _LOGGER.debug(f"{conn_name}: Task was cancelled")
        except Exception as exc:
            _LOGGER.error(
                f"{conn_name}: Unknown error occurred: {exc}", exc_info=True
            )
        finally:
            _LOGGER.debug(f"{conn_name}: disconnected")
            setattr(self, f"_ws_{conn_name}", None)
            self._connected = False

    async def _handle_message(self, conn_name, msg):
        if msg.type == aiohttp.WSMsgType.TEXT:
            payload = msg.json()
            if payload.get("event") == "ms.channel.unauthorized":
                raise AuthorizationError()
            elif payload.get("event") == "ms.channel.connect":
                _LOGGER.debug(f"{conn_name}: Authorization accepted")
                await (getattr(self, f"_on_connect_{conn_name}")(payload))
            else:
                await (getattr(self, f"_on_message_{conn_name}")(payload))
        elif msg.type == aiohttp.WSMsgType.ERROR:
            if issubclass(type(msg.data), Exception):
                raise msg.data
            else:
                _LOGGER.error(f"Received error: {msg.data}")
                await (getattr(self, f"_on_error_{conn_name}")(msg))

    async def _on_connect_remote(self, msg):
        token = msg.get("data", {}).get(ATTR_TOKEN)
        if token:
            _LOGGER.debug(f"Got token: {token}")
            await self._store.set(ATTR_TOKEN, token)
        await self.request_installed_apps()
        self._control_task = self._create_task(self._open_connection(WS_CONTROL))

    async def _on_connect_control(self, msg):
        self._app_monitor_task = self._create_task(self._monitor_running_app())
        self._connected = True

    async def _on_message_remote(self, msg):
        event = msg.get("event")

        if event == "ed.installedApp.get":
            self._build_app_list(msg)
        elif event == "ed.edenTV.update":
            # self.get_running_app(force_scan=True)
            pass

    async def _on_message_control(self, msg):
        app_id = None
        result = msg.get("result")
        if result:
            if type(result) is bool:
                app_id = msg.get("id")
                self._found_running_app = True
            elif type(result) is dict:
                if result.get("running") and result.get("visible"):
                    app_id = result.get("id")
                    self._found_running_app = True
        if app_id:
            self._update_current_app(app_id)

    def _update_current_app(self, app_id):
        new_current_app = self.installed_apps.get(app_id) if app_id else None
        if new_current_app != self.current_app:
            self.current_app = new_current_app
            _LOGGER.debug(f"Running app is: {self.current_app}")
            self._update()

    def _build_app_list(self, response):
        list_app = response.get("data", {}).get("data")
        installed_apps = {}
        for app_info in list_app:
            if "waipu" in app_info["name"]:
                continue
            app_id = app_info["appId"]
            app = App(app_id, app_info["name"], app_info["app_type"])
            installed_apps[app_id] = app
        self.installed_apps = installed_apps
        _LOGGER.debug("Installed apps:\n\t{}".format("\n\t".join([f"{app.app_name}: {app.app_id}" for app in installed_apps.values()])))
        self._update()

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

    async def _monitor_running_app(self):
        _LOGGER.debug("App monitor: starting")
        while self._ws_control and not self._ws_control.closed:
            self._found_running_app = False
            for app in self.installed_apps.values():
                if not self._ws_control or self._ws_control.closed:
                    break
                try:
                    await self._ws_control.send_json(
                        {
                            "id": app.app_id,
                            "method": (
                                "ms.webapplication.get"
                                if app.app_type == 4
                                else "ms.application.get"
                            ),
                            "params": {"id": app.app_id},
                        }
                    )
                except Exception as exc:
                    _LOGGER.error(f"Error while querying app status: {exc}", exc_info=True)
                else:
                    await asyncio.sleep(0.1)
            if self.current_app and not self._found_running_app:
                self._update_current_app(None)
        _LOGGER.debug("App monitor: stopping")

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
        except Exception:
            _LOGGER.error(f"Failed to send key {key}", exc_info=True)
        else:
            if key_press_delay is None:
                await asyncio.sleep(self.key_press_delay)
            elif key_press_delay > 0:
                await asyncio.sleep(key_press_delay)

    async def run_app(self, app_id, action_type="", meta_tag=""):
        if not action_type:
            app = self.installed_apps.get(app_id)
            action_type = "NATIVE_LAUNCH" if app and app.app_type != 2 else "DEEP_LINK"

        _LOGGER.debug(f"Running app {app_id} / {action_type} / {meta_tag}")

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
