import asyncio
import collections
import concurrent
import contextlib
import datetime
import enum
import json
import logging
import math
import uuid

import aiohttp

import weechat

loop = None
plugin = None
hdata = None


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
        if self._hook is None:
            logging.error("tick after unhook")
            return

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

    logfile = weechat.config_get_plugin("logfile")
    if logfile:
        h = logging.FileHandler(logfile, mode="a")
        h.setFormatter(
            logging.Formatter(
                fmt="%(asctime)-23s %(name)s %(levelname)s[%(lineno)d]: %(message)s"
            )
        )
        log.addHandler(h)

    logging.getLogger("asyncio").setLevel(logging.INFO)


class ServerState(enum.Enum):
    DISCONNECTED = 0
    DISCONNECTING = 1
    CONNECTED = 2


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

        self._buffer = None
        self._state = ServerState.DISCONNECTED
        self._buffers = {}
        self._buffer_hooks = collections.defaultdict(list)
        self._method_ids = set()

        # All set during connect()
        self._username = None
        self._uid = None
        self._main_loop = None
        self._session = None
        self._ws = None
        self._users = {}

    @property
    def buffers(self):
        return self._buffers

    @property
    def name(self):
        return self._name

    @property
    def users(self):
        return self._users

    @property
    def username(self):
        return self._username

    async def create_im(self, nick):
        async with self.rest_post("im.create", json={"username": nick}) as resp:
            jd = await resp.json()
            assert jd["success"], json.dumps(jd, sort_keys=True, indent=2)
            buf = await self._handle_new_subscription(jd["room"]["rid"])
            weechat.buffer_set(buf, "display", "auto")

    def close_buffer(self, buf):
        if self._state == ServerState.DISCONNECTING:
            return

        rid = weechat.buffer_get_string(buf, "localvar_rid")

        method_id = f"closing-buffer-{rid}"
        if method_id in self._method_ids:
            # Minimal effort debounce as hook_signal will make multiple calls
            return

        for hook in self._buffer_hooks[rid]:
            weechat.unhook(hook)

        del self._buffers[rid]
        del self._buffer_hooks[rid]

        self._method_ids.add(method_id)
        self._plugin.create_task(
            self.send(
                {
                    "msg": "method",
                    "method": "hideRoom",
                    "id": method_id,
                    "params": [
                        rid,
                    ],
                }
            )
        )

    async def mark_read(self, rid):
        if self._ws is None:
            return

        method_id = uuid.uuid4().hex
        self._method_ids.add(method_id)
        await self.send(
            {
                "msg": "method",
                "method": "readMessages",
                "id": method_id,
                "params": [
                    rid,
                ],
            }
        )

    async def disconnect(self):
        self._state = ServerState.DISCONNECTING

        rids = list(self._buffers.keys())
        for rid in rids:
            for hook in self._buffer_hooks[rid]:
                weechat.unhook(hook)
            del self._buffer_hooks[rid]

            buf = self._buffers.pop(rid)
            weechat.buffer_close(buf)

        if self._main_loop is not None:
            self._main_loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._main_loop
            self._main_loop = None

        await self._ws.close()
        self._ws = None

        await self._session.close()
        self._session = None

        if self._buffer is not None:
            weechat.buffer_close(self._buffer)
            self._buffer = None

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
            else:
                assert False, json.dumps(sub, sort_keys=True, indent=2)

            buf = weechat.buffer_new(name, "on_buf_input", "", "on_buf_closed", "")

            weechat.buffer_set(buf, "notify", "2")
            weechat.buffer_set(buf, "short_name", name)
            weechat.buffer_set(buf, "nicklist", "1")
            weechat.buffer_set(
                buf,
                "highlight_words_add",
                ",".join((self._username, f"@{self._username}", "@all", "@here")),
            )
            weechat.buffer_set(buf, "localvar_set_server", self._name)
            weechat.buffer_set(buf, "localvar_set_nick", self._username)

            if sub["t"] == "c":
                weechat.buffer_set(buf, "localvar_set_type", "channel")
                weechat.buffer_set(buf, "localvar_set_channel", name)
            else:
                weechat.buffer_set(buf, "localvar_set_type", "private")

            weechat.buffer_set(buf, "localvar_set_rid", rid)
            weechat.buffer_set(buf, "localvar_set_rc_type", sub["t"])

            self._buffer_hooks[rid].extend(
                [
                    weechat.hook_signal(
                        "buffer_closing", "rc_signal_buffer_closing", "wcrc"
                    ),
                    weechat.hook_signal(
                        "buffer_switch", "rc_signal_buffer_switch", "wcrc"
                    ),
                ]
            )

            for ng, color in self._nick_groups.items():
                weechat.nicklist_add_group(buf, "", ng, color, 1)

            self._buffers[rid] = buf
            await self._set_room_members(buf, rid)

            ls = sub["ls"]
            if isinstance(ls, dict):
                ls = datetime.datetime.fromtimestamp(ls["$date"] / 1000)
            else:
                ls = datetime.datetime.fromisoformat(ls.rstrip("Z"))
                ls += ls.astimezone().utcoffset()

            await self._set_room_history(buf, rid, ls)

        return buf

    async def _set_room_members(self, buf, rid):
        rtype = weechat.buffer_get_string(buf, "localvar_rc_type")
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
                for i, uid in enumerate(jd["room"]["uids"]):
                    try:
                        user = self._users[uid]
                    except KeyError:
                        username = jd["room"]["usernames"][i]
                        nicklist = groups["offline"]
                    else:
                        username = user._username
                        nicklist = groups[user._status]

                    weechat.nicklist_add_nick(buf, nicklist, username, "", "", "", 1)

        elif rtype == "p":
            async with self.rest_get("groups.members", params={"roomId": rid}) as resp:
                jd = await resp.json()
                assert jd["success"], json.dumps(jd, sort_keys=True, indent=2)
                for name, status in (
                    (m["username"], m["status"]) for m in jd["members"]
                ):
                    nicklist = groups[status]
                    weechat.nicklist_add_nick(buf, nicklist, name, "", "", "", 1)

    async def _set_room_history(self, buf, rid, last_seen):
        rtype = weechat.buffer_get_string(buf, "localvar_rc_type")

        url = {
            "c": "channels.history",
            "d": "im.history",
            "p": "groups.history",
        }[rtype]

        init_tags = ["notify_none", "no_highlight", "logger_backlog"]
        stack = []
        params = {"roomId": rid, "unreads": "true", "count": 50}
        params["oldest"] = last_seen.isoformat()

        # Get chunks of messages until we've pulled the last one seen by the
        # user.  After that get one more chunk just to ensure we have the
        # backlog reasonably filled.
        while True:
            async with self.rest_get(url, params=params) as resp:
                jd = await resp.json()

                assert jd["success"], json.dumps(jd, sort_keys=True, indent=2)
                msgs = jd.get("messages", [])
                stack.extend(msgs)

                if "unreadNotLoaded" not in jd:
                    break

                if jd["unreadNotLoaded"] == 0:
                    del params["unreads"]
                    del params["oldest"]

                params["offset"] = len(stack)

        stack.reverse()
        seen = True
        for msg in stack:
            # Value can be null
            edited = msg.get("editedAt") is not None

            ts = datetime.datetime.fromisoformat(
                (msg["editedAt"] if edited else msg["ts"]).rstrip("Z")
            )
            ts = ts + ts.astimezone().utcoffset()

            if seen:
                seen = ts < last_seen
                if not seen:
                    weechat.buffer_set(buf, "unread", "")

            if seen:
                tags = init_tags[:]
            else:
                tags = []

                level = weechat.WEECHAT_HOTLIST_MESSAGE
                if rtype in ("p", "d"):
                    level = weechat.WEECHAT_HOTLIST_PRIVATE

                mentioned = [u["username"] for u in msg.get("mentions", [])]
                if mentioned:
                    highlight_on = [self._username, "all", "here"]
                    if any(u in mentioned for u in highlight_on):
                        level = weechat.WEECHAT_HOTLIST_HIGHLIGHT

                weechat.buffer_set(buf, "hotlist", level)

            tags.extend(
                (
                    f"nick_{msg['u']['username']}",
                    f"rcid_{msg['_id']}",
                )
            )

            weechat.prnt_date_tags(
                buf,
                int(ts.timestamp()),
                ",".join(tags),
                f"{msg['u']['username']}\t{msg['msg']}",
            )

            msgs = []
            tags.append("rc_statusline")
            if edited:
                msgs.append("[edited]")
                tags.append(f"rcedit_{int(ts.timestamp() * 1000)}")

            # Value can be null
            if msg.get("reactions") is not None:
                msgs.extend(
                    f"{k}{len(v['usernames'])}" for k, v in msg["reactions"].items()
                )

            weechat.prnt_date_tags(
                buf,
                int(ts.timestamp()),
                ",".join(tags),
                f"\t\t{weechat.prefix('network')}{' '.join(msgs)}" if msgs else "",
            )

        logging.debug(
            "Loaded %s messages to '%s' backlog",
            len(stack),
            weechat.buffer_get_string(buf, "short_name"),
        )

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

        self._buffer = weechat.buffer_new(self._name, "rc_server_run_cb", "", "", "")
        weechat.buffer_set(self._buffer, "localvar_set_server", self._name)
        weechat.buffer_set(self._buffer, "localvar_set_type", "server")
        weechat.buffer_set(self._buffer, "localvar_set_nick", self._username)
        weechat.buffer_set(self._buffer, "short_name", self._name)
        weechat.prnt(self._buffer, f"Logged in as {self._username}")

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
                if not sub["open"]:
                    continue

                await self._update_buffer_from_sub(sub)

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

    async def _handle_new_subscription(self, rid):
        async with self.rest_get(
            "subscriptions.getOne", params={"roomId": rid}
        ) as resp:
            jd = await resp.json()
            assert jd["success"], json.dumps(jd, sort_keys=True, indent=2)

            if jd == {"success": True}:
                # Not actually subscribed which happens as __my_messages__
                # appears to give notifications for every public channel
                # regardless of membership.
                return None

            sub = jd["subscription"]
            buf = await self._update_buffer_from_sub(sub)
            logging.debug(
                "New subscription %s",
                weechat.buffer_get_string(buf, "name"),
            )
            return buf

    async def _process_message(self, jd):
        if jd.get("collection") == "stream-room-messages":
            return await self._handle_stream_room_messages(jd)

        if jd.get("collection") == "stream-notify-logged":
            return await self._handle_stream_notify_logged(jd)

        if jd.get("msg") == "updated" and jd.get("methods", []):
            # Nothing to do with this right now
            return

        if jd.get("msg") == "result":
            # We called a method and it worked
            assert jd.get("id") in self._method_ids, logging.info(
                json.dumps(jd, sort_keys=True, indent=2)
            )
            self._method_ids.remove(jd["id"])
            return

        if jd.get("msg") == "ready" and jd.get("subs", []):
            # Successful subscription
            return

        logging.warning(
            "Unhandled message:\n%s", json.dumps(jd, sort_keys=True, indent=2)
        )

    async def _handle_stream_room_messages(self, jd):
        msg, room_meta = jd["fields"]["args"]

        buf = self._buffers.get(msg["rid"])
        if buf is None:
            buf = await self._handle_new_subscription(msg["rid"])
            # History was loaded which includes the lastest message
            return

        # TODO:  This is probably not the most performant thing to do, if that
        # becomes an issue this is a good place to look.  But, until then,
        # keeping as much data in weechat as possible is definitely convenient.
        def is_msg_update():
            def match_msg_id(ptr):
                ptr = weechat.hdata_pointer(hdata.line, ptr, "data")
                for i in range(
                    weechat.hdata_integer(hdata.line_data, ptr, "tags_count")
                ):
                    tag = weechat.hdata_string(hdata.line_data, ptr, f"{i}|tags_array")
                    if tag == f"rcid_{msg['_id']}":
                        return True
                return False

            lines = weechat.hdata_pointer(hdata.buffer, buf, "lines")
            ptr = weechat.hdata_pointer(hdata.lines, lines, "last_line")

            while ptr and not match_msg_id(ptr):
                ptr = weechat.hdata_move(hdata.line, ptr, -1)

            return bool(ptr)

        if not is_msg_update():
            ts = msg["ts"]["$date"] // 1000

            weechat.prnt_date_tags(
                buf,
                ts,
                f"rcid_{msg['_id']},nick_{msg['u']['username']}",
                f"{msg['u']['username']}\t{msg['msg']}",
            )

            weechat.prnt_date_tags(
                buf,
                ts,
                f"rcid_{msg['_id']},rc_statusline",
                "\t\t",
            )

            if weechat.current_buffer() == buf:
                await self.mark_read(msg["rid"])

            level = weechat.WEECHAT_HOTLIST_MESSAGE
            if weechat.buffer_get_string(buf, "localvar_rc_type") in ("p", "d"):
                level = weechat.WEECHAT_HOTLIST_PRIVATE

            mentioned = [u["username"] for u in msg.get("mentions", [])]
            if mentioned:
                highlight_on = [self._username, "all", "here"]
                if any(u in mentioned for u in highlight_on):
                    level = weechat.WEECHAT_HOTLIST_HIGHLIGHT

            weechat.buffer_set(buf, "hotlist", level)
            return

        action_taken = self._update_msg_reactions(buf, msg)

        # Topic change
        if msg.get("t") == "room_changed_topic":
            prefix = weechat.prefix("network")
            weechat.prnt(
                buf,
                f"{prefix}{msg['u']['username']} changed the topic to {msg['msg']}",
            )
            action_taken = True

        if msg.get("t") == "au":
            prefix = weechat.prefix("network")
            weechat.prnt(
                buf,
                f"{prefix}{msg['u']['username']} added {msg['msg']} to the channel",
            )
            action_taken = True

        if msg.get("t") == "ul":
            prefix = weechat.prefix("network")
            weechat.prnt(buf, f"{prefix}{msg['u']['username']} left the channel")
            action_taken = True

        # Summarize urls
        if msg.get("urls", []) and msg["urls"][0].get("meta") is not None:
            prefix = weechat.prefix("network")
            for url, meta in ((url["url"], url["meta"]) for url in msg["urls"]):
                weechat.prnt(buf, f"{prefix}Link: {meta['pageTitle']}")
            action_taken = True

        if "editedAt" in msg:
            action_taken |= self._update_msg_text(buf, msg)

        if not action_taken:
            prefix = weechat.prefix("network")
            weechat.prnt(buf, f"{prefix}unperfectly handled message in debug buffer")
            logging.debug("%s\n", json.dumps(jd, sort_keys=True, indent=2))

    async def _handle_stream_notify_logged(self, jd):
        if jd["fields"]["eventName"] == "user-status":
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

    def _line_tags(self, buf):
        """
        Iterator yielding line hdata and tags from newest to oldest in a buffer.

        @param buf  - buffer to iterate on

        @yield - tuple of line hdata pointers and tags
        """
        lines = weechat.hdata_pointer(hdata.buffer, buf, "lines")
        line_ptr = weechat.hdata_pointer(hdata.lines, lines, "last_line")

        while line_ptr:
            line_data = weechat.hdata_pointer(hdata.line, line_ptr, "data")
            if not line_data:
                return

            tags = [
                weechat.hdata_string(hdata.line_data, line_data, f"{i}|tags_array")
                for i in range(
                    weechat.hdata_integer(hdata.line_data, line_data, "tags_count")
                )
            ]
            yield (line_data, tags)

            line_ptr = weechat.hdata_move(hdata.line, line_ptr, -1)

    def _update_msg_reactions(self, buf, msg):
        """
        Potentially update the reactions to a message.

        @param buf  - weechat buffer
        @param msg  - rocketchat message json

        @return - True if there was an update, False otherwise
        """
        reactions = {
            k: len(v["usernames"]) for k, v in msg.get("reactions", {}).items()
        }

        for line, tags in self._line_tags(buf):
            if all(k in tags for k in ("rc_statusline", f"rcid_{msg['_id']}")):
                break
        else:
            logging.warning("No match for reactions on msg %s", msg["_id"])
            return False

        new_message = ""
        if reactions:
            new_message = "%s%s%s" % (
                weechat.prefix("network"),
                "[edited] " if any(t.startswith("rcedit_") for t in tags) else "",
                " ".join(f"{k}{v}" for k, v in reactions.items()),
            )

        current_message = weechat.hdata_string(hdata.line_data, line, "message")
        if new_message == current_message:
            return False

        weechat.hdata_update(hdata.line_data, line, {"message": new_message})
        return True

    def _update_msg_text(self, buf, msg):
        """
        Potentially update the contents of message.

        @param buf  - weechat buffer
        @param msg  - rocketchat message json

        @return - True if there was an update, False otherwise
        """
        edited_ts = msg["editedAt"]["$date"]

        for ld, tags in self._line_tags(buf):
            if f"rcid_{msg['_id']}" in tags:
                break
        else:
            logging.warning("No match for edited msg %s", msg["_id"])
            return False

        last_edit = next(
            (t.split("_")[1] for t in tags if t.startswith("rcedit_")), None
        )
        if last_edit is None:
            prefix = weechat.prefix("network")
            current = weechat.hdata_string(hdata.line_data, ld, "message")
            index = current.find(prefix) + len(prefix)
            new_msg = f"{prefix}[edited] {current[index:]}"
            weechat.hdata_update(hdata.line_data, ld, {"message": new_msg})
        elif int(last_edit) == edited_ts:
            return False

        logging.debug("Updating message %s", msg["_id"])

        new_tags = [t for t in tags if not t.startswith("rcedit_")]
        new_tags.append(f"rcedit_{edited_ts}")
        weechat.hdata_update(hdata.line_data, ld, {"tags_array": ",".join(new_tags)})

        lds = []
        for ld, tags in self._line_tags(buf):
            if f"rcid_{msg['_id']}" in tags:
                lds.append(ld)
            elif lds:
                break

        # Drop the first (statusline) and reverse to match message order
        lds = lds[:0:-1]

        # We can't add new lines so tag whatever remains to the last
        # TODO: if this was the last message we can clear them all and re-print
        new_msgs = [
            m.replace("\n", " <cr> ") for m in msg["msg"].split("\n", len(lds) - 1)
        ]

        for m, ld in zip(new_msgs, lds):
            weechat.hdata_update(hdata.line_data, ld, {"message": m})

        return True


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

        weechat.hook_command_run("/connect", "rc_plugin_run_cb", "connect")
        weechat.hook_command_run("/disconnect", "rc_plugin_run_cb", "disconnect")
        weechat.hook_command_run("/users", "rc_server_run_cb", "users")
        weechat.hook_command_run("/query", "rc_command_query", "")

    def server(self, name):
        return self._servers[name]

    def create_task(self, task):
        return self._loop.create_task(task)

    def shutdown(self):
        async def go():
            await asyncio.gather(*[s.disconnect() for s in self._servers.values()])

        self._loop.run_until_complete(go())
        self._loop.disconnect()

    def cmd_help(self, buf, *args):
        weechat.prnt(buf, f"help: command: {args}")
        return weechat.WEECHAT_RC_OK

    def connect(self, buf, *args):
        if not args:
            weechat.prnt(buf, "Missing server name")
            return weechat.WEECHAT_RC_OK

        if args[0] not in weechat.config_get_plugin("servers").split():
            weechat.prnt(buf, f"Unknown server '{args[0]}'")
            return weechat.WEECHAT_RC_OK

        if (
            args[0] in self._servers
            and self._servers[args[0]]._state != ServerState.DISCONNECTED
        ):
            weechat.prnt(
                buf, f"{args[0]}is already {self._servers[args[0]]._state.name}"
            )
            return weechat.WEECHAT_RC_OK_EAT

        self._connect(args[0])
        return weechat.WEECHAT_RC_OK_EAT

    def disconnect(self, buf, *args):
        if not args:
            weechat.prnt(buf, "Missing server name")
            return weechat.WEECHAT_RC_OK

        name = args[0]
        if name not in weechat.config_get_plugin("servers").split():
            weechat.prnt(buf, f"Unknown server '{args[0]}'")
            return weechat.WEECHAT_RC_OK

        if name not in self._servers:
            weechat.prnt(buf, f"server '{args[0]}' not connected")
            return weechat.WEECHAT_RC_OK

        self._loop.run_until_complete(self._servers[name].disconnect())
        self._servers.pop(name)
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
            weechat.config_set_plugin(f"servers.{server}.ssl", "1")

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
            weechat.config_unset_plugin(f"servers.{server}.ssl")

            weechat.prnt(buf, f"Removed rocketchat server {server}")

        return weechat.WEECHAT_RC_OK

    def _connect(self, server):
        host = weechat.config_get_plugin(f"servers.{server}.host")
        port = weechat.config_get_plugin(f"servers.{server}.port")
        token = weechat.config_get_plugin(f"servers.{server}.token")
        ssl = weechat.config_get_plugin(f"servers.{server}.ssl") != "0"
        logging.debug("Connecting to %s", server)

        self._servers[server] = Server(
            name=server, uri=f"{host}:{port}", ssl=ssl, loop=self._loop, plugin=self
        )
        self._loop.run_until_complete(self._servers[server].connect(token))
        return weechat.WEECHAT_RC_OK


def rc_command_query(_, buf, args):
    server_name = weechat.buffer_get_string(buf, "localvar_server")
    server = plugin.server(server_name)

    target = args.split()[1]
    nicks = [user._username for user in server.users.values()]
    if target not in nicks:
        weechat.prnt(
            buf,
            f"%sNo known user {target} on {server_name}" % (weechat.prefix("error")),
        )
        return weechat.WEECHAT_RC_ERROR

    # If we fetch all subscriptions on connect and then keep up with the
    # message stream, the server should always know about all of its rooms.  So
    # we can just look there and schedule a new room to be created if
    # necessary.
    for buf in server.buffers.values():
        rtype = weechat.buffer_get_string(buf, "localvar_rc_type")
        if rtype != "d":
            continue

        userlist = set()
        infolist = weechat.infolist_get("nicklist", buf, "")
        while True:
            if not weechat.infolist_next(infolist):
                break

            type_ = weechat.infolist_string(infolist, "type")
            if type_ != "nick":
                continue

            name = weechat.infolist_string(infolist, "name")
            userlist.add(name)

        if userlist != {server.username, target}:
            continue

        name = weechat.buffer_get_string(buf, "short_name")
        weechat.command("", f"/buffer {name}")
        break
    else:
        plugin.create_task(server.create_im(target))

    return weechat.WEECHAT_RC_OK_EAT


def rc_command_run_cb(cmd, buf, args):
    f = getattr(plugin, f"cmd_{cmd}", None)
    if f is None:
        return weechat.WEECHAT_RC_OK

    return f(buf, *args.split()[1:])


def rc_server_run_cb(cmd, buf, args):
    logging.debug("rc_server_run_cb: cmd: %s, buf: %s, args: %s", cmd, buf, args)
    server_name = weechat.buffer_get_string(buf, "localvar_server")
    if not server_name:
        return weechat.WEECHAT_RC_ERROR

    server = plugin.server(server_name)

    if cmd == "users":
        colors = {
            "online": weechat.color("nicklist_group"),
            "busy": weechat.color("nicklist_away"),
            "away": weechat.color("nicklist_away"),
            "offline": weechat.color("chat_nick_offline"),
        }
        prefix = weechat.prefix("network")

        weechat.prnt(buf, f"{prefix}Users on {server.name}:")
        for uid, user in server.users.items():
            weechat.prnt(
                buf,
                f"{prefix}  {user._username:<30}{colors[user._status]}{user._status}",
            )

        return weechat.WEECHAT_RC_OK_EAT


def rc_signal_buffer_closing(cb_data, _, buf):
    if cb_data != "wcrc":
        return weechat.WEECHAT_RC_OK

    server_name = weechat.buffer_get_string(buf, "localvar_server")
    if not server_name:
        return weechat.WEECHAT_RC_OK

    try:
        server = plugin.server(server_name)
    except KeyError:
        return weechat.WEECHAT_RC_OK

    server = plugin.server(server_name)
    server.close_buffer(buf)
    return weechat.WEECHAT_RC_OK


def rc_signal_buffer_switch(cb_data, _, buf):
    if cb_data != "wcrc":
        return weechat.WEECHAT_RC_OK

    server_name = weechat.buffer_get_string(buf, "localvar_server")
    if not server_name:
        return weechat.WEECHAT_RC_OK

    try:
        server = plugin.server(server_name)
    except KeyError:
        return weechat.WEECHAT_RC_OK

    rid = weechat.buffer_get_string(buf, "localvar_rid")
    plugin.create_task(server.mark_read(rid))
    return weechat.WEECHAT_RC_OK


def rc_plugin_run_cb(cmd, buf, args):
    args = args.split()[1:]
    if cmd == "connect":
        return plugin.connect(buf, *args)
    elif cmd == "disconnect":
        return plugin.disconnect(buf, *args)
    else:
        return weechat.WEECHAT_RC_OK


def rc_command_cb(_, buf, args):
    try:
        cmd, *args = args.split()
    except ValueError:
        cmd = "help"
        args = []

    return getattr(plugin, f"cmd_{cmd}", plugin.cmd_help)(buf, *args)


def unload_plugin():
    global plugin
    try:
        plugin.shutdown()
    except Exception:
        logging.exception("Error during plugin unloading")
        return weechat.WEECHAT_RC_ERROR
    return weechat.WEECHAT_RC_OK


def main():
    weechat.register(
        "wcrc",
        "Justin Bronder <jsbronder@cold-front.org>",
        "0.0.1",
        "BSD",
        "Rocketchat compatibility",
        "unload_plugin",
        "",
    )
    rcbuf = weechat.buffer_new(
        "wcrc-debug", "on_buf_input", "debug-buffer", "on_buf_closed", "debug-buffer"
    )
    setup_logging(rcbuf, level=logging.DEBUG)

    global hdata
    hdata = collections.namedtuple("hdata", "buffer lines line line_data message")(
        **{
            n: weechat.hdata_get(n)
            for n in ("buffer", "lines", "line", "line_data", "message")
        }
    )

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

    # TODO: can we automatically add a filter for empty reaction lines?
    # /filter add empty_rc_statusline * rc_statusline ^$

    return


if __name__ == "__main__":
    main()
