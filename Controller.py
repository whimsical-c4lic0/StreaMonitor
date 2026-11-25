"""Command-line controller for a running StreamMonitor instance."""

import argparse
import sys

import zmq

DEFAULT_HOST = "127.0.0.1"
CONTROL_PORT = 6969
DEFAULT_TIMEOUT_MS = 10_000

# Commands exposed by streamonitor.manager.Manager.do_* methods.
COMMANDS = ("add", "remove", "start", "stop", "restart", "status", "status2")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="Controller.py",
        description="Send a command to a running StreamMonitor instance over ZeroMQ.",
    )
    parser.add_argument(
        "--host",
        "-H",
        default=DEFAULT_HOST,
        help=f"host running StreamMonitor (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "command",
        choices=COMMANDS,
        help="command to send",
    )
    parser.add_argument("username", nargs="?", help="streamer username, or * for start/stop")
    parser.add_argument("site", nargs="?", help="site name or abbreviation (add only)")
    return parser


def _normalize_legacy_argv(argv):
    """Translate the old attached-host form (-hHOST) to --host HOST.

    The bare "-h" flag is now reserved for help; use --host/-H to select a
    remote instance. This keeps old ``Controller.py -hHOST ...`` invocations
    working while removing the conflict between help and the host option.
    """
    normalized = []
    for argument in argv:
        if argument.startswith("-h") and not argument.startswith("--") and len(argument) > 2:
            normalized.extend(["--host", argument[2:]])
        else:
            normalized.append(argument)
    return normalized


def _require_arguments(parser, args):
    if args.command == "add" and not (args.username and args.site):
        parser.error("add requires a username and a site")
    if args.command in {"remove", "start", "stop", "restart"} and not args.username:
        parser.error(f"{args.command} requires a username")


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(_normalize_legacy_argv(list(argv)))
    _require_arguments(parser, args)

    message = " ".join(part for part in (args.command, args.username, args.site) if part)

    socket = zmq.Context.instance().socket(zmq.REQ)
    socket.setsockopt(zmq.LINGER, 0)
    socket.setsockopt(zmq.RCVTIMEO, DEFAULT_TIMEOUT_MS)
    socket.setsockopt(zmq.SNDTIMEO, DEFAULT_TIMEOUT_MS)
    try:
        socket.connect(f"tcp://{args.host}:{CONTROL_PORT}")
        socket.send_string(message)
        reply = socket.recv_string()
    except zmq.ZMQError as exc:
        print(f"Controller: cannot reach {args.host}:{CONTROL_PORT}: {exc}", file=sys.stderr)
        return 1
    finally:
        socket.close()

    print(reply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
