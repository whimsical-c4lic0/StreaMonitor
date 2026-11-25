import argparse
import os
import sys

import streamonitor.config as config
import streamonitor.sites  # noqa: F401 - importing registers the site classes
from streamonitor.clean_exit import CleanExit
from streamonitor.managers.bulk_status_manager import BulkStatusManager
from streamonitor.managers.cli_manager import CLIManager
from streamonitor.managers.disk_space_monitor import DiskSpaceMonitor
from streamonitor.managers.httpmanager import HTTPManager
from streamonitor.managers.zmq_manager import ZMQManager


def is_docker():
    if os.path.exists('/.dockerenv'):
        return True
    path = '/proc/self/cgroup'
    if not os.path.isfile(path):
        return False
    with open(path, encoding='utf-8') as handle:
        return any('docker' in line for line in handle)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="Downloader.py",
        description="Monitor and record live streams. Settings are read from environment variables and .env.",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    parser.parse_args(argv)

    if not DiskSpaceMonitor.disk_space_good():
        print(DiskSpaceMonitor.under_threshold_message)
        return 1

    streamers = config.loadStreamers()

    CleanExit(streamers)

    disk_space_monitor = DiskSpaceMonitor(streamers)
    disk_space_monitor.start()

    bulk_status_manager = BulkStatusManager(streamers)
    bulk_status_manager.start()

    if not is_docker():
        console_manager = CLIManager(streamers)
        console_manager.start()

    zmq_manager = ZMQManager(streamers)
    zmq_manager.start()

    http_manager = HTTPManager(streamers)
    http_manager.start()

    return 0


if __name__ == "__main__":
    sys.exit(main())
