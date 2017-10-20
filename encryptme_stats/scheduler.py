"""Probe scheduler and sender."""

import datetime
import logging
import random
import time
import uuid
from functools import partial

import requests
import schedule

from encryptme_stats import metrics


class Message(object):
    """A message to send.

    Handles own rescheduling.
    """

    data = None
    retries = 0
    max_retries = 0
    retry_interval = 0
    rescheduled = False
    server = None

    def __init__(self, data, max_retries=0, retry_interval=30, server=None):
        self.data = data
        self.max_retries = max_retries
        self.retry_interval = retry_interval
        self.server = server

    def send(self):
        """Initiate sending of this message."""
        try:
            response = requests.post(self.server, json=self.data)
            if not response.status_code == 200:
                raise Exception('Retry required: %s' % response.status_code)
        except Exception as exc:
            logging.error("Failed to send message: %s", exc)
            self.retry()

    def retry(self):
        """Retry sending until max_retries is hit."""
        if self.retries >= self.max_retries:
            logging.error("Failed to send message: %s", self.data)
            return

        def resend():
            """Scheduler endpoint for resending."""
            logging.warning("Retry %d of %d", self.retries, self.max_retries)
            try:
                response = requests.post(self.server, json=self.data)
                if not response.status_code == '200':
                    raise Exception('Retry required')
                return schedule.CancelJob
            except Exception as exc:
                logging.error("Failed to send message - retrying: %s", exc)

                if self.retries >= self.max_retries:
                    logging.error("Failed to send message (%d retries): %s",
                                  self.retries,
                                  self.data)
                    return schedule.CancelJob

            self.retries += 1
            self.rescheduled = True

        schedule.every(self.retry_interval).seconds.do(resend)


class Scheduler(object):
    """Singleton that sets up scheduling and starts sending metrics"""

    server_id = None
    server = None
    config = None

    @classmethod
    def start(cls, server_id, config, now=False):
        """Start the scheduler, and run forever."""
        cls.server_id = server_id
        cls.config = config

        cls.server = config['encryptme_stats']['server']

        cls.parse_schedule(config, now=now)

        while True:
            schedule.run_pending()
            time.sleep(1)

    @classmethod
    def parse_schedule(cls, config, now=False):
        """Parse config to build a schedule."""
        start_offset = random.randint(0, 60)
        if now:
            start_offset = 1

        for method in metrics.__all__:
            interval = float(config[method]['interval'])

            job = schedule.every(interval).seconds.do(
                partial(cls.gather, method, getattr(metrics, method)))

            # Make the next call be sooner
            job.next_run = datetime.datetime.now() + datetime.timedelta(
                seconds=start_offset)

    @classmethod
    def gather(cls, method, metric):
        """Gather statistics from the specified metric callable and send."""

        def make_message(item, retries, interval):
            """Creates a Message class."""

            item['@timestamp'] = datetime.datetime.utcnow().isoformat()
            item['server_id'] = cls.server_id
            item['@id'] = str(uuid.uuid4())

            return Message(item, retries, interval, cls.server)

        try:
            result = metric()
            if isinstance(result, list):
                result = [result]

            for doc in result:
                make_message(doc,
                             int(cls.config[method]['max_retries']),
                             int(cls.config[method]['retry_interval'])).send()
        except Exception as exc:
            logging.exception("Failed to gather data: %s", exc)