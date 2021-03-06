import json
from pprint import pprint, pformat
from collections import defaultdict, deque
import time
import logging
from importlib import import_module
from threading import Thread
from copy import deepcopy
import sys
from datetime import datetime

from ioweb.error import IowebConfigError

logger = logging.getLogger('ioweb.stat')


class Stat(object):
    default_key_aliases = {
        'crawler:request-processed': 'req',
        'crawler:request-proxy-processed': 'req-proxy',
        'crawler:request-ok': 'req-ok',
        'crawler:request-retry': 'req-retry',
        'crawler:request-fail': 'req-fail',
        'crawler:request-rejected': 'req-rejected',
    }
    ignore_prefixes = (
        'http:',
        'network-error:',
    )
    def __init__(
            self,
            # logging
            speed_keys=None,
            logging_enabled=True,
            logging_interval=3,
            logging_format='text',
            key_aliases=None,
            # export
            #shard_interval = 10,
            export=None,
            export_interval=5,
            # fatalq
            fatalq=None,
        ):
        # Arg: speed_keys
        if speed_keys is None:
            speed_keys = []
        elif isinstance(speed_keys, str):
            speed_keys = [speed_keys]
        self.speed_keys = speed_keys
        # Arg: logging_enabled
        self.logging_enabled = logging_enabled
        # Arg: logging_interval
        self.logging_interval = logging_interval
        # Arg: logging_format
        self.logging_format = logging_format
        # Arg: key_aliases
        self.key_aliases = deepcopy(self.default_key_aliases)
        if key_aliases:
            self.key_aliases.update(key_aliases)

        # Arg: fatalq
        self.fatalq = fatalq

        # Arg: shard_interval
        #self.shard_interval = shard_interval

        # Logging
        self.total_counters = defaultdict(int)
        self.moment_counters = {}
        self.logging_time = 0
        if self.logging_enabled:
            self.th_logging = Thread(target=self.thread_logging)
            self.th_logging.daemon = True
            self.th_logging.start()

        # Setup exporting in last case
        # Arg: export
        self.th_export = None
        self.export_config = export
        self.export_driver = None
        if self.export_config:
            self.setup_export_driver(self.export_config)

        # Args: export_interval
        self.export_interval = export_interval

        # Export
        #self.shard_counters = {}
        if self.export_driver:
            self.start_export_thread()

    def start_export_thread(self):
        if self.export_driver and not self.th_export:
            self.th_export = Thread(target=self.thread_export)
            self.th_export.daemon = True
            self.th_export.start()

        # Internal
        self.service_time = 0
        self.service_interval = 1

    def setup_export_driver(self, cfg):
        self.export_config = cfg
        mod_path, cls_name = cfg['driver'].split(':', 1)
        driver_mod = import_module(mod_path)
        driver_cls = getattr(driver_mod, cls_name)
        self.export_driver = driver_cls(
            tags=cfg.get('tags', {}),
            connect_options=cfg.get('connect_options', {}),
            measurement=cfg.get('measurement', {})
        )
        if self.th_export:
            raise Exception('Stat export thread already created')
        else:
            self.start_export_thread()

    def build_eps_data(self, now, interval):
        """
        Args:
            interval - number of recent seconds for
            mean value calculation
        """
        now_int = int(now)
        eps = defaultdict(int)
        for ts in range(now_int - interval, now_int):
            for key in sorted(self.speed_keys):
                try:
                    eps[key] += self.moment_counters[ts][key]
                except KeyError:
                    eps[key] += 0
        return eps

    def build_eps_string(self, now):
        interval=30
        eps = self.build_eps_data(now, interval)
        ret = []
        for key, val in eps.items():
            label = self.key_aliases.get(key, key)
            val_str = '%.1f' % (val / interval)
            if val_str == '0.0' and val > 0:
                val_str = '0.0+'
            ret.append('%s: %s' % (label, val_str))
        ret = sorted(ret, key=lambda x: x[0])
        return ', '.join(ret)

    def build_counter_data(self, ignore=True):
        ret = {}
        for key in self.total_counters.keys():
            if not key.startswith(self.ignore_prefixes):
                val = self.total_counters[key]
                ret[key] = val
        return ret

    def build_counter_string(self):
        data = self.build_counter_data()
        ret = []
        for key in sorted(list(data.keys())):
            label = self.key_aliases.get(key, key)
            val = data[key]
            ret.append('%s: %d' % (label, val))
        return ', '.join(ret)

    def render_moment_json(self, now):
        interval = 30
        return json.dumps({
            'eps': self.build_eps_data(now, interval),
            'counter': self.build_counter_data(),
        })

    def render_moment(self, now=None):
        if now is None:
            now = time.time()
        if self.logging_format == 'json':
            return self.render_moment_json(now)
        else:
            eps_str = self.build_eps_string(now)
            counter_str = self.build_counter_string()
            return 'EPS: %s | TOTAL: %s' % (eps_str, counter_str)

    def thread_logging(self):
        try:
            while True:
                now = time.time()
                logger.debug(self.render_moment(now))
                # Sleep `self.logging_interval` seconds minus time spent on logging
                sleep_time = (
                    self.logging_interval + (time.time() - now)
                )
                time.sleep(sleep_time)
        except (KeyboardInterrupt, Exception) as ex:
            if self.fatalq:
                self.fatalq.put((sys.exc_info(), None))
            else:
                raise

    def thread_export(self):
        try:
            prev_counters = None
            while True:
                now = time.time()
                counters = deepcopy(self.total_counters)
                if prev_counters:
                    delta_counters = dict(
                        (x, counters[x] - prev_counters.get(x, 0))
                        for x in counters.keys()
                    )
                else:
                    delta_counters = counters
                prev_counters = counters
                self.export_driver.write_events(delta_counters)
                sleep_time = (
                    self.export_interval + (time.time() - now)
                )
                time.sleep(sleep_time)
        except (KeyboardInterrupt, Exception) as ex:
            if self.fatalq:
                self.fatalq.put((sys.exc_info(), None))
            else:
                raise

    def inc(self, key, count=1):
        now_int = int(time.time())
        #shard_ts = now_int - now_int % self.shard_interval
        #shard_slot = self.shard_counters.setdefault(shard_ts, defaultdict(int))
        moment_slot = self.moment_counters.setdefault(now_int, defaultdict(int))

        moment_slot[key] += count
        #shard_slot[key] += count
        self.total_counters[key] += count


class InfluxdbExportDriver(object):
    def __init__(self, connect_options, tags, measurement='crawler_stats'):
        self.connect_options = deepcopy(connect_options)
        self.client = None
        self.measurement = measurement
        self.tags = deepcopy(tags)
        self.database_created = False
        self.connect()

    def connect(self):
        from influxdb import InfluxDBClient

        self.client = InfluxDBClient(**self.connect_options)

    def write_events(self, snapshot):
        from requests import RequestException

        if not self.database_created:
            self.client.create_database(self.connect_options['database'])
            self.database_created = True
        if snapshot:
            data = {
                "measurement": self.measurement,
                "tags": self.tags,
                "time": datetime.utcnow().isoformat(),
                "fields": dict((
                    (x, y) for x, y in snapshot.items()
                )),
            }
            while True:
                try:
                    self.client.write_points([data])
                except RequestException:
                    logger.exception('Failed to send metrics')
                    time.sleep(1)
                    # reconnecting
                    while True:
                        try:
                            self.connect()
                        except RequestException:
                            logger.exception(
                                'Failed to reconnect to metrics database'
                            )
                            time.sleep(1)
                        else:
                            break
                else:
                    break


class CrawlerInfluxdbExportDriver(InfluxdbExportDriver):
    def __init__(self, connect_options, tags, *args, **kwargs):
        for key in ['hostname', 'project', 'crawler_id']:
            if key not in tags:
                raise IowebConfigError(
                    'Tag %s is required to use CalwerInfluxdbExportDriver'
                    % key
                )
        super(CrawlerInfluxdbExportDriver, self).__init__(
            connect_options, tags, *args, **kwargs
        )
