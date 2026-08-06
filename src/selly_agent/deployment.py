"""Which deployment profile this process runs under — a host install, or a container.

The program is the same either way; what differs is who owns the machine around it. A host
install launches Chrome itself, registers a login job, and replaces its own code on update. In a
container it does none of those: Chrome runs on the seller's own desktop outside the container,
something else keeps the process alive, and a new version is a new image. Every branch on that
difference reads the one marker resolved here.

The marker is baked into the image with `ENV`, deliberately not read from config.json. The config
file lives in the bind-mounted data directory, so a seller who copied that directory onto a host
install would carry a "container" flag to a machine where every one of these answers is wrong.
An image cannot be copied that way.

What this deliberately does *not* know is how the container is being run. We ship a compose file
and the docs name its commands, but the program is equally happy under `podman compose`, a
hand-written `docker run`, or something we have never heard of — and a message printing
`docker exec -it selly-agent …` at someone whose container is named something else, under an
engine we guessed wrong, is worse than one that says "in the container" and lets them translate.
Starting, stopping and replacing the container is the operator's business; ours is to know that
we are inside one.
"""

from __future__ import annotations

import os

MARKER_VAR = "SELLY_DEPLOYMENT"

CONTAINER = "container"
HOST = "host"


def profile(env=None) -> str:
    """The deployment profile: `container` when the image's marker says so, else `host`.

    Anything other than the container marker reads as a host install: the host profile is the
    default the program was written for, so an unset or misspelled value must not land somewhere
    that manages nothing.
    """
    env = os.environ if env is None else env
    return CONTAINER if env.get(MARKER_VAR, "").strip().lower() == CONTAINER else HOST


def is_container(env=None) -> bool:
    return profile(env) == CONTAINER


def manages_chrome(env=None) -> bool:
    """Whether this deployment may start the browser for itself.

    False in a container: Chrome is on the seller's desktop, outside our process namespace, and
    the only honest thing to do with a closed one is say so.
    """
    return not is_container(env)
