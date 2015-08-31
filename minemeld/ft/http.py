import requests
import logging
import copy
import gevent
import random

from . import base
from . import table
from .utils import utc_millisec

LOG = logging.getLogger(__name__)


class HttpFT(base.BaseFT):
    _ftclass = 'HttpFT'

    def __init__(self, name, chassis, config, reinit=True):
        self.glet = None

        self.table = table.Table(name)
        self.table.create_index('_updated')
        self.active_requests = []

        super(HttpFT, self).__init__(name, chassis, config, reinit=reinit)

    def configure(self):
        super(HttpFT, self).configure()

        self.source_name = self.config.get('source_name', self.name)
        self.url = self.config.get('url', None)
        self.cchar = self.config.get('cchar', None)
        self.split_char = self.config.get('split_char', None)
        self.split_pos = self.config.get('split_pos', 0)
        self.attributes = self.config.get('attributes', {})
        self.interval = self.config.get('interval', 86400)
        self.force_updates = self.config.get('force_updates', False)
        self.polling_timeout = self.config.get('polling_timeout', 20)
        self.num_retries = self.config.get('num_retries', 2)

    def _values_compare(self, d1, d2):
        return True

    def _process_line(self, line):
        attributes = copy.deepcopy(self.attributes)
        return line.split()[0], attributes

    def _polling_loop(self):
        LOG.info("Polling %s", self.name)

        now = utc_millisec()

        r = requests.get(
            self.url,
            stream=True,
            verify=True,
            timeout=self.polling_timeout
        )
        r.raise_for_status()

        for line in r.iter_lines():
            line = line.strip()
            if not line:
                continue

            if self.cchar is not None and \
               line.startswith(self.cchar):
                continue

            if self.split_char is not None:
                toks = line.split(self.split_char)
                if len(toks) < self.split_pos+1:
                    continue
                line = toks[self.split_pos].strip()

            indicator, attributes = self._process_line(line)
            if indicator is None:
                continue

            attributes['sources'] = [self.source_name]
            attributes['_updated'] = utc_millisec()

            ev = self.table.get(indicator)
            if ev is not None:
                attributes['first_seen'] = ev['first_seen']
            else:
                attributes['first_seen'] = utc_millisec()
            attributes['last_seen'] = utc_millisec()

            send_update = True
            if ev is not None and not self.force_updates:
                if self._values_compare(ev, attributes):
                    send_update = False

            LOG.debug("%s - Updating %s %s", self.name, indicator, attributes)
            self.table.put(indicator, attributes)

            if send_update:
                LOG.debug("%s - Emitting update for %s", self.name, indicator)
                self.emit_update(indicator, attributes)

        for i, v in self.table.query('_updated', from_key=0, to_key=now-1,
                                     include_value=True):
            LOG.debug("%s - Removing old %s - %s", self.name, i, v)
            self.table.delete(i)
            self.emit_withdraw(i, value={'sources': [self.source_name]})

        LOG.debug("%s - End of polling #indicators: %d",
                  self.name, self.table.num_indicators)

    def _run(self):
        tryn = 0

        if not self.reinit_flag:
            LOG.debug("reinit flag set, resending current indicators")
            # reinit flag is set, emit update for all the known indicators
            for i, v in self.table.query('_updated', include_value=True):
                self.emit_update(i, v)

        while True:
            lastrun = utc_millisec()

            try:
                self._polling_loop()
            except gevent.GreenletExit:
                return
            except:
                LOG.exception("Exception in polling loop for %s", self.name)
                tryn += 1
                if tryn < self.num_retries:
                    gevent.sleep(random.randint(1, 5))
                    continue

            tryn = 0

            now = utc_millisec()
            deltat = (lastrun+self.interval*1000)-now

            while deltat < 0:
                LOG.warning("Time for processing exceeded interval for %s",
                            self.name)
                deltat += self.interval*1000

            gevent.sleep(deltat/1000.0)

    def _send_indicators(self, source=None, from_key=None, to_key=None):
        q = self.table.query(
            '_updated',
            from_key=from_key,
            to_key=to_key,
            include_value=True
        )
        for i, v in q:
            self.do_rpc(source, "update", indicator=i, value=v)

    def get(self, source=None, indicator=None):
        if not type(indicator) in [str, unicode]:
            raise ValueError("Invalid indicator type")

        value = self.table.get(indicator)

        return value

    def get_all(self, source=None):
        self._send_indicators(source=source)
        return 'OK'

    def get_range(self, source=None, index=None, from_key=None, to_key=None):
        if index is not None and index != '_updated':
            raise ValueError('Index not found')

        self._send_indicators(
            source=source,
            from_key=from_key,
            to_key=to_key
        )

        return 'OK'

    def length(self, source=None):
        return self.table.num_indicators

    def start(self):
        if self.glet is not None:
            return

        self.glet = gevent.spawn_later(random.randint(0, 2), self._run)

    def stop(self):
        if self.glet is None:
            return

        for g in self.active_requests:
            g.kill()

        self.glet.kill()

        LOG.info("%s - # indicators: %d", self.name, self.table.num_indicators)