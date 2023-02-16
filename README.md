# Status
This plugin is somewhere between a proof-of-concept and an alpha release.
While I am using it as my primary interface with rocketchat every day, I also
have to load the web UI depending on what sort of messages are being sent.
While the UI isn't great, the plugin should be stable enough that you're not
going to lose any data or miss notifications.

The structure of the code is, well, crap.  There's either too many or not
enough abstractions.  There's not even a faint glimmer of the possibility of
adding unittests.  Documentation is an afterthought and is liable to be
completely out of date.

This is not where I want to be, but
[RERO](https://en.wikipedia.org/wiki/Release_early,_release_often) I guess
[[1]](#Footnote1).  Hey, maybe someone will see the `WeechatLoop`, run with it
and create a plugin that I can use instead of this one.  In any case, what I'm
saying is that the code definitely isn't locked down and if it continues to
hold my interest there's some major refactoring coming.  Or maybe not.  I mean,
if it's good enough that I'm only opening the web UI once a week and am not
constantly chasing Rocket.Chat API changes then maybe I'll spend some more time
with the kids?

# Getting Started
To begin, add a new filter that ignores the empty status lines added for each
message in order to reserve space in the future for reactions/edits, etc.
    `/filter add empty_rc_statusline * rc_statusline ^$`

Get a new Personal Access Token by:

1. Click on your profile picture on the top left and select 'My Account'.
1. Click 'Personal Access Tokens'.
1. Enter 'weechat' into the text box and click 'Add'.
1. Record the 'Token', you may ignore the 'user id'.

With the token in hand, you can now add the server to weechat.
    `/rc server add <servername> <hostname>:<port> <token>`
Where `<servername>` is a name you choose for this server, `<hostname>` is the
hostname of the server, `<port>` is the server port and `<token>` is the
Personal Access Token you recorded above.

# Development
## Setting up a local server
The following worked last week and again today, so it's definitely going to
keep working.  Once you have the pod ready to go, you can simply `podman pod
start rc` to start hacking and then `podman pod stop rc` when you want 5 cores
and 4Gb of RAM back in order to feed Chrome.
```bash
podman pull docker.io/library/mongo:4.4.18
podman pull docker.io/rocketchat/rocket.chat:3.18.7

podman pod create -n rc -p 3000:3000
podman run  --name mongo --pod rc -d docker.io/library/mongo:4.4.18  --replSet rs0 --oplogSize 128
podman exec -ti mongo mongo --eval "printjson(rs.initiate())"
podman run --name rocketchat --pod rc \
    --env MONGO_URL=mongodb://localhost:27017/meteor \
    --env MONGO_OPLOG_URL=mongodb://localhost:27017/local \
    -d docker.io/rocketchat/rocket.chat
```


#### Footnote1
Huh, does that rhyme with YOLO?
