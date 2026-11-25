import os.path
import shutil
from threading import Thread
from time import sleep

import streamonitor.log as log
from parameters import DOWNLOADS_DIR, MIN_FREE_DISK_PERCENT
from streamonitor.clean_exit import CleanExit


class DiskSpaceMonitor(Thread):
    under_threshold_message = 'Free space is under threshold. Exiting.'

    def __init__(self, streamers):
        super().__init__()
        self.streamers = streamers
        self.daemon = True
        self.logger = log.Logger("disk_space_monitor")

    @staticmethod
    def free_space():
        usage = DiskSpaceMonitor.space_usage()
        free_percent = usage.free / usage.total * 100
        return free_percent

    @staticmethod
    def space_usage():
        if os.path.exists(DOWNLOADS_DIR):
            usage = shutil.disk_usage(DOWNLOADS_DIR)
        else:
            usage = shutil.disk_usage('.')
        return usage

    @staticmethod
    def disk_space_good():
        return DiskSpaceMonitor.free_space() > MIN_FREE_DISK_PERCENT

    def run(self):
        while True:
            if not self.disk_space_good():
                self.logger.warning(self.under_threshold_message)
                CleanExit(self.streamers)()
                return
            sleep(5)


# Backwards-compatible name for code and integrations that imported the old
# detector class. New code should use DiskSpaceMonitor.
OOSDetector = DiskSpaceMonitor
