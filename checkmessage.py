#!/usr/bin/python
import asyncio
import json
import os
import sys
import time
from os.path import exists
from pathlib import Path, PosixPath
from re import sub
from sys import stderr

import aiohttp
import appdirs
import pickledb
import systemd_watchdog
from asyncinotify import Inotify, Mask
from limit import limit
from loguru import logger

import subprocess

wd = systemd_watchdog.watchdog()

#Trigger pep8speaks

QUIETF='/.quiet'
appname = "CheckMessage"
appauthor = "AliceDTRH"
statedir = appdirs.user_state_dir(appname, appauthor)
db = pickledb.load(f'{statedir}/pickledb.db', False)
logger.debug(f"Opening database: {statedir}/pickledb.db")
db.auto_dump = True
alarmcommand = ["/usr/bin/mpv", "/usr/share/sounds/freedesktop/stereo/dialog-warning.oga", "--volume=200"]

if db.exists('since'):
    url = "https://ntfy.sh/{id}/json?since={since}".format(
        since=db.get('since'),
        id=os.environ['ID'])
    logger.debug(db.get('since'))
else:
    url = "https://ntfy.sh/{id}/json".format(id=os.environ['ID'])

@logger.catch()
class AlertSystem:
    alert = False
    title = ""

    def __init__(self) -> None:
        self._message = str()
        self._urgency = int()
        if exists(Path(statedir+QUIETF)):
            os.remove(Path(statedir+QUIETF))
            logger.error("Quiet file existed, removed. Not taking other action.")
        if db.get('alert') == "True":
            self.enable()
            self.report_status("Existing alert file found")

    def get_message(self) -> str:
        return sub(r'[^a-zA-Z0-9 \.]', '', self._message)

    def set_message(self, msg) -> None:
        self._message = msg

    def enable(self) -> None:
        self.alert = True
        db.set('alert', "True")

    def disable(self) -> None:
        logger.info("Alert disabled!")
        self.alert = False
        if db.exists('alert'):
            db.rem('alert')

    def set_urgency(self, urgency):
        self._urgency = urgency

    def get_urgency(self):
        if self._urgency in [3, 4]:
            return "normal"
        if self._urgency == 5:
            return "critical"
        return "low"

    def run_alert(self, newnotification=False) -> None:
        if not self.alert:
            return
        for _ in range(5):
            if(self.alert):
                subprocess.run(alarmcommand)
        if newnotification:
            # Not working:
            subprocess.run(
                [
                    "/usr/bin/notify-send",
                    f"-u {self.urgency}",
                    f"-t 0 {self.title}",
                    self.message,
                ]
            ) 
        elif self.alert:
            subprocess.run(alarmcommand)

        logger.info(f"New message: {self.message}")
        with open(f'{statedir}/messages', 'a+') as f:
            f.write(self._message + '\n')

    def prepare_state_folder(self) -> None:
        Path(statedir).mkdir(parents=True, exist_ok=True)

    def report_status(self, msg) -> None:
        wd.status(str(msg))
        logger.info('<<< "{msg}"'.format(msg=msg))

    message = property(get_message, set_message)
    urgency = property(get_urgency, set_urgency)

asys = AlertSystem()

@logger.catch()
@limit(2, 30)
async def check_message(session, r):
    result = []
    try:
        while len(r.content._buffer) > 1:
            result.append(await r.content.readline())
        result.append(await r.content.readline())
        wd.beat()
    except TimeoutError:
        logger.exception('TimeoutError occured')
    

    return result

@logger.catch()
@limit(1, 30)
async def check_files() -> None:
    logger.debug("Started file checking.")
    with Inotify() as inotify:
        inotify.add_watch(statedir, Mask.CREATE)
        async for event in inotify:
            if event.name == PosixPath('.quiet') and event.mask == Mask.CREATE:
                asys.disable()
                if exists(Path(statedir+QUIETF)):
                    os.remove(Path(statedir+QUIETF))
                    return

@logger.catch()
@limit(2, 30)
async def handle_messages(msgs):
    for msg in msgs:
        msg = json.loads(msg)
        db.set('since', msg['time'])
        db.dump()
        if msg['event'] == "open":
            wd.ready()
        if msg['event'] == "message":
            if "priority" in msg and msg['priority'] >= 5:
                logger.debug(msg['priority'])
                asys.set_message(msg['message'])
                asys.enable()
                asys.run_alert(True)
            logger.info(msg['message'])
        else:
            logger.debug(msg['event'])

@logger.catch()
@limit(2, 30)
async def msgs_done(fut):
    await handle_messages(fut.result())
    
@logger.catch()
async def main():
    logger.remove()
    logger.add(sys.stderr, backtrace=True, diagnose=True, level="INFO")
    logger.add("log.txt", colorize=True, backtrace=True, diagnose=True, rotation="500 MB")
    logger.warning("Log started")
    async with aiohttp.ClientSession(raise_for_status=True) as session:
        timeout = aiohttp.ClientTimeout(total=0, connect=None,
                      sock_connect=None, sock_read=80)
        async with session.get(url, timeout=timeout) as r:
            msgs = asyncio.create_task(check_message(session, r))
            filewatcher = asyncio.create_task(check_files())

            while True:

                await asyncio.wait([msgs, filewatcher], return_when=asyncio.FIRST_COMPLETED)

                if filewatcher.done():
                    logger.warning("Filewatcher died")
                    filewatcher = asyncio.create_task(check_files())

                if msgs.done():
                    logger.info("Messages checked")
                    logger.debug(msgs)
                    logger.debug(f"Cancelled? {str(msgs.cancelled())}")
                    try:
                      await handle_messages(msgs.result())
                    except aiohttp.client_exceptions.ServerTimeoutError:
                      logger.exception("Server timed out.")
                    msgs = asyncio.create_task(check_message(session, r))

                if asys.alert:
                    asys.run_alert()
                #await asyncio.sleep(1)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.error("Shutting down - KeyboardInterrupt")
        db.dump()
    except:
        logger.exception("Uncaught exception")
        raise
        
