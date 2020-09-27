import asyncio
import concurrent
import contextlib
import enum
import json
import logging
import math
import uuid

import aiohttp

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
    _nick_groups = {
        "0|online": weechat.color("nicklist_group"),
        "1|busy": weechat.color("nicklist_away"),
        "2|away": weechat.color("nicklist_away"),
        "3|offline": weechat.color("chat_nick_offline"),
    }

    def __init__(self, name, uri, ssl, loop, plugin):
        self._name = name
        self._loop = loop
        self._plugin = plugin
        self._ws_uri = "ws%s://%s/websocket" % ("s" if ssl else "", uri)
        self._rest_uri = "http%s://%s/api/v1" % ("s" if ssl else "", uri)

        self._state = ServerState.DISCONNECTED
        self._buffers = {}
        self._method_ids = set()

        # All set during connect()
        self._username = None
        self._uid = None
        self._main_loop = None
        self._session = None
        self._ws = None
        self._users = {}

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
        await self._session.close()

        rids = list(self._buffers.keys())
        for rid in rids:
            buf = self._buffers.pop(rid)
            weechat.buffer_close(buf)

        self._ws = None
        self._state = ServerState.DISCONNECTED

    async def recv(self):
        while True:
            jd = await self._ws.receive_json()

            if jd.get("msg") == "ping":
                await self._ws.send_json({"msg": "pong"})
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
        return await self._ws.send_json(msg)

    @contextlib.asynccontextmanager
    async def rest_get(self, page, **kwds):
        async with self._session.get(f"{self._rest_uri}/{page}", **kwds) as resp:
            yield resp

    @contextlib.asynccontextmanager
    async def rest_post(self, page, **kwds):
        async with self._session.post(f"{self._rest_uri}/{page}", **kwds) as resp:
            yield resp

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

    async def _update_buffer_from_sub(self, sub):
        rid = sub["rid"]
        buf = self._buffers.get(rid)
        if buf is None:
            if sub["t"] in ("c", "p"):
                name = f"#{sub['name']}"
            elif sub["t"] == "d":
                name = sub["name"]

            buf = weechat.buffer_new(name, "on_buf_input", "", "on_buf_closed", "")

            weechat.buffer_set(buf, "notify", "2")
            weechat.buffer_set(buf, "short_name", name)
            weechat.buffer_set(buf, "nicklist", "1")
            weechat.buffer_set(buf, "localvar_set_server", self._name)
            weechat.buffer_set(buf, "localvar_set_rid", rid)
            weechat.buffer_set(buf, "localvar_set_type", sub["t"])

            for ng, color in self._nick_groups.items():
                weechat.nicklist_add_group(buf, "", ng, color, 1)

        weechat.buffer_set(buf, "hidden", "0" if sub["open"] else "1")
        return buf

    async def _set_room_members(self, buf, rid):
        rtype = weechat.buffer_get_string(buf, "localvar_type")
        groups = {
            n.split("|")[1]: weechat.nicklist_search_group(buf, "", n)
            for n in self._nick_groups
        }

        if rtype == "c":
            async with self.rest_get(
                "channels.members", params={"roomId": rid}
            ) as resp:
                jd = await resp.json()
                assert jd["success"], json.dumps(jd, sort_keys=True, indent=2)
                for member in jd["members"]:
                    nicklist = groups[member["status"]]
                    weechat.nicklist_add_nick(
                        buf, nicklist, member["username"], "", "", "", 1
                    )

        elif rtype == "d":
            async with self.rest_get("rooms.info", params={"roomId": rid}) as resp:
                jd = await resp.json()
                assert jd["success"], json.dumps(jd, sort_keys=True, indent=2)
                for uid in jd["room"]["uids"]:
                    user = self._users[uid]
                    nicklist = groups[user._status]
                    weechat.nicklist_add_nick(
                        buf, nicklist, user._username, "", "", "", 1
                    )

        elif rtype == "p":
            async with self.rest_get("groups.members", params={"roomId": rid}) as resp:
                jd = await resp.json()
                assert jd["success"], json.dumps(jd, sort_keys=True, indent=2)
                for name, status in (
                    (m["username"], m["status"]) for m in jd["members"]
                ):
                    nicklist = groups[status]
                    weechat.nicklist_add_nick(buf, nicklist, name, "", "", "", 1)

    async def connect(self, token):
        async with aiohttp.request(
            "post", f"{self._rest_uri}/login", json={"resume": token}
        ) as resp:
            jd = await resp.json()
            assert jd["status"] == "success", json.dumps(jd, sort_keys=True, indent=2)
            self._username = jd["data"]["me"]["username"]
            self._uid = jd["data"]["me"]["_id"]
            self._http_token = jd["data"]["authToken"]
            self._session = aiohttp.ClientSession(
                headers={
                    "X-User-Id": self._uid,
                    "X-Auth-Token": jd["data"]["authToken"],
                }
            )
            logging.info("REST login to %s as %s", self._name, self._username)

        async with self.rest_get("users.list") as resp:
            jd = await resp.json()
            assert jd["success"], json.dumps(jd, sort_keys=True, indent=2)
            self._users = {
                u["_id"]: User(u["username"], u["_id"], u["status"])
                for u in jd["users"]
            }

        async with self.rest_get("subscriptions.get") as resp:
            jd = await resp.json()
            assert jd["success"], json.dumps(jd, sort_keys=True, indent=2)
            for sub in jd["update"]:
                buf = await self._update_buffer_from_sub(sub)
                self._buffers[sub["rid"]] = buf

        for rid, buf in self._buffers.items():
            await self._set_room_members(buf, rid)
            # get_history()
            # await room.connect()

        self._ws = await self._session.ws_connect(f"{self._ws_uri}")

        jd = await self.recv()
        assert jd == {"server_id": "0"}, json.dumps(jd, sort_keys=True, indent=2)

        await self.send({"msg": "connect", "version": "1", "support": ["1"]})
        jd = await self.recv()
        assert jd.get("msg") == "connected", json.dumps(jd, sort_keys=True, indent=2)

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

            assert (
                jd.get("msg") == "added" and jd.get("collection") == "users"
            ), json.dumps(jd, sort_keys=True, indent=2)

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
            assert jd.get("msg") == "updated", json.dumps(jd, sort_keys=True, indent=2)

        self._main_loop = asyncio.create_task(self._main())

    async def _main(self):
        self._state = ServerState.CONNECTED
        logging.info("Connected to %s as %s", self._name, self._username)

        await self.send(
            {
                "msg": "sub",
                "id": "stream-notify-logged",
                "name": "stream-notify-logged",
                "params": ["user-status", True],
            }
        )

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

            buf = self._buffers.get(msg["rid"])
            if buf is None:
                async with self.rest_get(
                    "subscriptions.getOne",
                    params={"roomId": msg["rid"]},
                ) as resp:
                    jd = await resp.json()
                    assert jd["success"], json.dumps(jd, sort_keys=True, indent=2)

                    if jd == {"success": True}:
                        # Not actually subscribed to this channel.  Which begs
                        # the question why the message was sent in the first
                        # place...
                        return

                    sub = jd["subscription"]
                    rid = sub["rid"]
                    buf = await self._update_buffer_from_sub(sub)
                    self._buffers[rid] = buf
                    await self._set_room_members(buf, rid)
                    logging.debug(
                        "New room %s %s",
                        rid,
                        weechat.buffer_get_string(buf, "name"),
                    )

            weechat.prnt(buf, f"{msg['u']['username']}\t{msg['msg']}")

        elif (
            jd.get("msg") == "changed"
            and jd.get("collection") == "stream-notify-logged"
            and jd["fields"]["eventName"] == "user-status"
        ):
            int_to_status = ["offline", "online", "away", "busy"]
            uid, username, int_status, _ = jd["fields"]["args"][0]
            status = int_to_status[int_status]

            user = self._users.get(uid)
            if user is not None:
                user._status = status
            else:
                self._users[uid] = User(username, uid, status)

            for buf in self._buffers.values():
                nick = weechat.nicklist_search_nick(buf, "", username)
                if not nick:
                    continue

                weechat.nicklist_remove_nick(buf, nick)
                group_name = next(
                    n for n in self._nick_groups if n.split("|")[1] == status
                )
                group = weechat.nicklist_search_group(buf, "", group_name)
                weechat.nicklist_add_nick(buf, group, username, "", "", "", 1)

        elif jd.get("msg") == "updated" and jd.get("methods", []):
            # Nothing to do with this right now
            pass
        elif jd.get("msg") == "result":
            if jd.get("id") in self._method_ids:
                # We called a method and it worked
                self._method_ids.remove(jd["id"])

        elif jd.get("msg") == "ready" and jd.get("subs", []):
            # Successful subscription
            pass
        else:
            logging.warning(
                "Unhandled message:\n%s", json.dumps(jd, sort_keys=True, indent=2)
            )


class User:
    def __init__(self, username, uid, status):
        self._username = username
        self._uid = uid
        self._status = status


def on_buf_input(data, buf, c):
    if data == "debug-buffer":
        return weechat.WEECHAT_RC_ERROR

    server_name = weechat.buffer_get_string(buf, "localvar_server")
    server = plugin.server(server_name)

    rid = weechat.buffer_get_string(buf, "localvar_rid")
    server.send_message(rid, c)
    return weechat.WEECHAT_RC_OK


def on_buf_closed(data, buf):
    if data == "debug-buffer":
        return weechat.WEECHAT_RC_OK

    # TODO part from the channel

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

        self._servers[server] = Server(
            name=server, uri=f"{host}:{port}", ssl=False, loop=self._loop, plugin=self
        )
        self.create_task(self._servers[server].connect(token))
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
