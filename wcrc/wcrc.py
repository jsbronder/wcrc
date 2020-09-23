import asyncio
import concurrent
import contextlib
import enum
import json
import logging
import math
import uuid

import websockets

import weechat

loop = None
plugin = None


def tick(*args):
    global loop
    loop.tick()
    return weechat.WEECHAT_RC_OK


class WeechatLoop(asyncio.SelectorEventLoop):
    class SynchronousExecutor(concurrent.futures.ThreadPoolExecutor):
        def __init__(self):
            return

        def submit(self, fn, *args, **kwds):
            f = concurrent.futures.Future()
            f.set_result(fn(*args, **kwds))
            return f

        def map(self, func, *iterables, timeout=None, chunksize=1):
            f = concurrent.futures.Future()
            f.set_result(map(func, *iterables))
            return f

        def shutdown(self, wait=True):
            return

    def __init__(self):
        super().__init__()
        self._hook = None
        self._next_hook = None
        self.set_default_executor(WeechatLoop.SynchronousExecutor())

    def connect(self):
        self._hook = weechat.hook_fd(self._selector.fileno(), 1, 1, 0, "tick", "")

    def disconnect(self):
        weechat.unhook(self._hook)
        self._hook = None

    def tick(self):
        if self._next_hook is not None:
            weechat.unhook(self._next_hook)
            self._next_hook = None

        if self.is_running():
            self._next_hook = weechat.hook_timer(10, 0, 0, "tick", "")
            return

        self.call_soon(self.stop)
        self.run_forever()

        if self._ready:
            self._next_hook = weechat.hook_timer(1, 0, 0, "tick", "")
        elif self._scheduled:
            when = self._scheduled[0]._when
            timeout = max(0, (when - self.time() + self._clock_resolution))
            self._next_hook = weechat.hook_timer(
                math.ceil(timeout * 1000), 0, 0, "tick", ""
            )

    def create_task(self, *args):
        r = super().create_task(*args)
        if not self.is_running():
            tick()
        return r


def setup_logging(wbuf, level=logging.WARNING):
    class WeechatLoggingHandler(logging.Handler):
        def emit(self, record):
            m = self.format(record)
            weechat.prnt(wbuf, f"{m}")

    log = logging.getLogger()
    for h in log.handlers:
        log.removeHandler(h)

    h = WeechatLoggingHandler()
    h.setFormatter(
        logging.Formatter(fmt="%(name)s %(levelname)s[%(lineno)d]: %(message)s")
    )
    log.setLevel(level)
    log.addHandler(h)

    logging.getLogger("asyncio").setLevel(logging.INFO)


class ServerState(enum.Enum):
    DISCONNECTED = 0
    CONNECTED = 1


class Server:
    def __init__(self, name, loop, plugin):
        self._name = name
        self._loop = loop
        self._plugin = plugin

        self._state = ServerState.DISCONNECTED
        self._username = "weechat"
        self._uid = None
        self._ws = None
        self._main_loop = None
        self._rooms = {}

        self._method_ids = set()

    @property
    def name(self):
        return self._name

    async def disconnect(self):
        if self._main_loop is not None:
            with contextlib.suppress(asyncio.CancelledError):
                self._main_loop.cancel()
                await self._main_loop
            self._main_loop = None

        await self._ws.close()

        rids = list(self._rooms.keys())
        for rid in rids:
            room = self._rooms.pop(rid)
            room.disconnect()

        self._ws = None
        self._state = ServerState.DISCONNECTED

    def close_room(self, rid):
        if rid not in self._rooms:
            return

        if rid in self._rooms:
            self._rooms.pop(rid)

    async def recv(self):
        while True:
            data = await self._ws.recv()
            jd = json.loads(data)

            if jd.get("msg") == "ping":
                await self._ws.send(json.dumps({"msg": "pong"}))
                continue

            if jd.get("msg") == "added" and (
                jd.get("collection").startswith("stream-notify-")
                or jd.get("collection") == "stream-room-messages"
            ):
                continue

            if jd.get("msg") == "updated" and jd.get("methods", []):
                continue

            return jd

    async def send(self, msg):
        return await self._ws.send(json.dumps(msg))

    def send_message(self, rid, msg):
        msg_id = f"{hash(msg)}"
        method_id = f"{rid}.{msg_id}"
        self._method_ids.add(method_id)

        self._plugin.create_task(
            self.send(
                {
                    "msg": "method",
                    "method": "sendMessage",
                    "id": method_id,
                    "params": [
                        {
                            "_id": uuid.uuid4().hex,
                            "rid": rid,
                            "msg": msg,
                        }
                    ],
                }
            )
        )

    async def connect(self, uri, token):
        self._ws = await websockets.connect(uri, loop=loop)

        jd = await self.recv()
        assert jd == {"server_id": "0"}

        await self.send({"msg": "connect", "version": "1", "support": ["1"]})
        jd = await self.recv()
        assert jd.get("msg") == "connected"

        await self.send(
            {
                "msg": "method",
                "method": "login",
                "id": f"{self._name}.login",
                "params": [{"resume": token}],
            }
        )
        while True:
            jd = await self.recv()
            if jd.get("id") == f"{self._name}.login":
                self._uid = jd["result"]["id"]
                break

            assert jd.get("msg") == "added" and jd.get("collection") == "users"

        await self.send(
            {
                "msg": "method",
                "method": "rooms/get",
                "id": f"{self._name}.rooms/get",
                "params": [],
            }
        )
        while True:
            jd = await self.recv()
            if jd.get("id") == f"{self._name}.rooms/get":
                break

            assert jd.get("msg") == "updated"

        for room in jd.get("result", []):
            if room["t"] in ("c", "p"):
                r = Room(self, room["_id"], f"#{room['name']}")
            elif room["t"] == "d":
                r = Room(
                    self,
                    room["_id"],
                    " ".join(n for n in room["usernames"] if n != self._username),
                )

            self._rooms[room["_id"]] = r
            # r.get_history()

        await self.send(
            {
                "msg": "sub",
                "id": f"{self._name}.stream-room-messages",
                "name": "stream-room-messages",
                "params": ["__my_messages__", False],
            }
        )
        while True:
            jd = await self.recv()
            if jd.get(
                "msg"
            ) == "ready" and f"{self._name}.stream-room-messages" in jd.get("subs", []):
                break

            logging.debug(jd)
            assert jd.get("msg") == "updated"

        self._main_loop = asyncio.create_task(self._main())

    async def _main(self):
        self._state = ServerState.CONNECTED

        while True:
            try:
                jd = await self.recv()
                await self._process_message(jd)
            except asyncio.CancelledError:
                break
            except Exception:
                logging.exception("Exception in main loop - restarting")

    async def _process_message(self, jd):
        if (
            jd.get("msg") == "changed"
            and jd.get("collection") == "stream-room-messages"
        ):
            msg, room_meta = jd["fields"]["args"]

            room = self._rooms.get(msg["rid"])
            if room is None:
                if room_meta["roomType"] in ("c", "p"):
                    name = f"#{room_meta['roomName']}"
                    room = Room(self, msg["rid"], name)
                    self._rooms[msg["rid"]] = room
                    logging.debug("New room %s %s", msg["rid"], name)
                else:
                    method_id = f"new-room.{msg['rid']}"
                    await self.send(
                        {
                            "msg": "method",
                            "method": "rooms/get",
                            "id": method_id,
                            "params": [msg["ts"]],
                        }
                    )
                    return
            weechat.prnt(room._buffer, f"{msg['u']['username']}\t{msg['msg']}")

        elif jd.get("msg") == "updated" and jd.get("methods", []):
            # Nothing to do with this right now
            pass
        elif jd.get("msg") == "result":
            if jd.get("id") in self._method_ids:
                # We called a method and it worked
                self._method_ids.remove(jd["id"])

            elif jd.get("id").startswith("new-room."):
                # New private chat
                _, rid = jd["id"].split(".", 1)
                room_data = next(r for r in jd["result"]["update"] if r["_id"] == rid)
                name = ", ".join(
                    n for n in room_data["usernames"] if n != self._username
                )
                room = Room(self, rid, name)
                self._rooms[rid] = room
                logging.debug("New room %s %s", rid, name)

                msg = room_data["lastMessage"]
                weechat.prnt(room._buffer, f"{msg['u']['username']}\t{msg['msg']}")
        else:
            logging.warning(
                "Unhandled message:\n%s", json.dumps(jd, sort_keys=True, indent=2)
            )


class Room:
    def __init__(self, server, id_, name):
        self._server = server
        self._id = id_
        self._name = name
        self._got_history = False

        self._buffer = weechat.buffer_new(
            name,
            "on_buf_input",
            f"{server.name}.{id_}",
            "on_buf_closed",
            f"{server.name}.{id_}",
        )

        weechat.buffer_set(self._buffer, "short_name", name)
        weechat.buffer_set(self._buffer, "notify", "3")

    def disconnect(self):
        weechat.buffer_close(self._buffer)

    def get_history(self):
        self._got_history = False
        self._server.ws.send(
            json.dumps(
                {
                    "msg": "method",
                    "method": "loadHistory",
                    "id": f"{self._server.name}.{self._name}.loadHistory",
                    "params": [self._id, None, 200, None],
                }
            )
        )


def on_buf_input(data, buf, c):
    server_name, rid = data.split(".")
    server = plugin.server(server_name)
    server.send_message(rid, c)
    return weechat.WEECHAT_RC_OK


def on_buf_closed(data, buf):
    if data == "debug-buffer":
        return weechat.WEECHAT_RC_OK

    server_name, rid = data.split(".")
    server = plugin.server(server_name)
    server.close_room(rid)
    return weechat.WEECHAT_RC_OK


class Plugin:
    def __init__(self, buf, loop):
        self._buf = buf
        self._loop = loop
        self._servers = {}

        for server in weechat.config_get_plugin("servers").split():
            autoconnect = bool(
                int(weechat.config_get_plugin(f"servers.{server}.autoconnect"))
            )
            if autoconnect:
                self._connect(server)
            logging.debug("Loaded config for %s", server)

        weechat.hook_command_run("/connect", "rc_command_run_cb", "connect")

    def server(self, name):
        return self._servers[name]

    def create_task(self, task):
        return self._loop.create_task(task)

    def cmd_help(self, buf, *args):
        weechat.prnt(buf, f"help: command: {args}")
        return weechat.WEECHAT_RC_OK

    def cmd_connect(self, buf, *args):
        if not args:
            weechat.prnt(buf, "Missing server name")
            return weechat.WEECHAT_RC_OK

        if args[0] not in weechat.config_get_plugin("servers").split():
            weechat.prnt(buf, f"Unknown server '{args[0]}'")
            return weechat.WEECHAT_RC_OK

        self._connect(args[0])
        return weechat.WEECHAT_RC_OK_EAT

    def cmd_disconnect(self, buf, *args):
        if not args:
            weechat.prnt(buf, "Missing server name")
            return weechat.WEECHAT_RC_OK

        name = args[0]
        if name not in weechat.config_get_plugin("servers").split():
            weechat.prnt(buf, f"Unknown server '{args[0]}'")
            return weechat.WEECHAT_RC_OK

        if name in self._servers:
            t = self.create_task(self._servers[name].disconnect())
            t.add_done_callback(lambda _: self._servers.pop(name))
            return weechat.WEECHAT_RC_OK
        else:
            weechat.prnt(buf, f"server '{args[0]}' not connected")
            return weechat.WEECHAT_RC_OK

        return weechat.WEECHAT_RC_OK_EAT

    def cmd_server(self, buf, *args):
        if not args:
            self.cmd_help(buf, *args)

        elif args[0] == "add":
            server = args[1]
            host, port = args[2].split(":")
            token = args[3]

            servers = weechat.config_get_plugin("servers").split()
            if server in servers:
                logging.error("Server %s already exists", server)
                return weechat.WEECHAT_RC_OK

            servers.append(server)
            weechat.config_set_plugin("servers", f'{" ".join(servers)}')
            weechat.config_set_plugin(f"servers.{server}.host", host)
            weechat.config_set_plugin(f"servers.{server}.port", str(port))
            weechat.config_set_plugin(f"servers.{server}.token", token)
            weechat.config_set_plugin(f"servers.{server}.autoconnect", "0")

            weechat.prnt(buf, f"Added rocketchat server {server} at {host}:{port}")

        elif args[0] == "ls":
            weechat.prnt(buf, "Servers:")
            for server in weechat.config_get_plugin("servers").split():
                state = ServerState.DISCONNECTED.name
                if server in self._servers:
                    state = self._servers[server]._state.name
                weechat.prnt(
                    buf,
                    "\t%-30s%s:%s - %s"
                    % (
                        server,
                        weechat.config_get_plugin(f"servers.{server}.host"),
                        weechat.config_get_plugin(f"servers.{server}.port"),
                        state,
                    ),
                )

        elif args[0] == "rm":
            server = args[1]
            servers = weechat.config_get_plugin("servers").split()
            if server not in servers:
                logging.error("Server %s does not exist", server)
                return weechat.WEECHAT_RC_OK

            servers.remove(server)
            weechat.config_set_plugin("servers", f'{" ".join(servers)}')
            weechat.config_unset_plugin(f"servers.{server}.host")
            weechat.config_unset_plugin(f"servers.{server}.port")
            weechat.config_unset_plugin(f"servers.{server}.token")
            weechat.config_unset_plugin(f"servers.{server}.autoconnect")

            weechat.prnt(buf, f"Removed rocketchat server {server}")

        return weechat.WEECHAT_RC_OK

    def _connect(self, server):
        host = weechat.config_get_plugin(f"servers.{server}.host")
        port = weechat.config_get_plugin(f"servers.{server}.port")
        token = weechat.config_get_plugin(f"servers.{server}.token")
        logging.debug("Connecting to %s", server)

        self._servers[server] = Server(name=server, loop=self._loop, plugin=self)
        self.create_task(
            self._servers[server].connect(f"ws://{host}:{port}/websocket", token)
        )
        return weechat.WEECHAT_RC_OK


def rc_command_run_cb(cmd, buf, args):
    f = getattr(plugin, f"cmd_{cmd}", None)
    if f is None:
        return weechat.WEECHAT_RC_OK

    return f(buf, *args.split()[1:])


def rc_command_cb(_, buf, args):
    try:
        cmd, *args = args.split()
    except ValueError:
        cmd = "help"
        args = []

    return getattr(plugin, f"cmd_{cmd}", plugin.cmd_help)(buf, *args)


def main():
    weechat.register(
        "wcrc",
        "Justin Bronder <jsbronder@cold-front.org>",
        "0.0.1",
        "BSD",
        "Rocketchat compatibility",
        "",
        "",
    )
    rcbuf = weechat.buffer_new(
        "rocket_chat", "on_buf_input", "debug-buffer", "on_buf_closed", "debug-buffer"
    )
    setup_logging(rcbuf, level=logging.DEBUG)

    global loop
    loop = WeechatLoop()
    loop.connect()

    global plugin
    plugin = Plugin(rcbuf, loop)

    weechat.hook_command(
        "rc",
        "Rocket chat plugin command entry point",
        "<command> [<arguments>]",
        "Commands:\n" "\tcommand here and stuff",
        "",
        "rc_command_cb",
        "",
    )

    return


if __name__ == "__main__":
    main()
