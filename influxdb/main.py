"""
An InfluxDB plugin, for sending statistics to InfluxDB
"""

import time
import requests
import simplejson as json
from threading import Thread
from collections import deque
from plugins.base import om_expose, OMPluginBase, PluginConfigChecker, om_metric_receive


class InfluxDB(OMPluginBase):
    """
    An InfluxDB plugin, for sending statistics to InfluxDB
    """

    name = 'InfluxDB'
    version = '2.0.44'
    interfaces = [('config', '1.0')]

    config_description = [{'name': 'url',
                           'type': 'str',
                           'description': 'The enpoint for the InfluxDB using HTTP. E.g. http://1.2.3.4:8086'},
                          {'name': 'username',
                           'type': 'str',
                           'description': 'Optional username for InfluxDB authentication.'},
                          {'name': 'password',
                           'type': 'str',
                           'description': 'Optional password for InfluxDB authentication.'},
                          {'name': 'database',
                           'type': 'str',
                           'description': 'The InfluxDB database name to witch statistics need to be send.'},
                          {'name': 'batch_size',
                           'type': 'int',
                           'description': 'The maximum batch size of grouped metrics to be send to InfluxDB.'}]

    default_config = {'url': '', 'database': 'openmotics'}

    def __init__(self, webinterface, logger):
        super(InfluxDB, self).__init__(webinterface, logger)
        self.logger('Starting InfluxDB plugin...')

        self._config = self.read_config(InfluxDB.default_config)
        self._config_checker = PluginConfigChecker(InfluxDB.config_description)
        self._pending_metrics = {}
        self._send_queue = deque()
        self._batch_sizes = []
        self._queue_sizes = []
        self._stats_time = 0
        self._definitions = None

        self._send_thread = Thread(target=self._sender)
        self._send_thread.setName('InfluxDB batch sender')
        self._send_thread.daemon = True
        self._send_thread.start()
        self._definition_thread = Thread(target=self._load_definitions)
        self._definition_thread.setName('InfluxDB definition loader')
        self._definition_thread.daemon = True
        self._definition_thread.start()

        self._read_config()
        self.logger("Started InfluxDB plugin")

    def _read_config(self):
        self._url = self._config['url']
        self._database = self._config['database']
        self._batch_size = self._config.get('batch_size', 10)
        username = self._config.get('username', '')
        password = self._config.get('password', '')
        self._auth = None if username == '' else (username, password)

        self._endpoint = '{0}/write?db={1}'.format(self._url, self._database)
        self._query_endpoint = '{0}/query?db={1}&epoch=ns'.format(self._url, self._database)
        self._headers = {'X-Requested-With': 'OpenMotics plugin: InfluxDB'}

        self._enabled = self._url != '' and self._database != ''

        self.logger('InfluxDB is {0}'.format('enabled' if self._enabled else 'disabled'))

    def _load_definitions(self):
        while self._definitions is None:
            try:
                result = json.loads(self.webinterface.get_metric_definitions(None))
                if result['success'] is True:
                    self._definitions = result['definitions']
                    self.logger('Definitions loaded')
            except Exception as ex:
                self.logger('Error loading definitions, retrying'.format(ex))
            time.sleep(5)

    @om_metric_receive(interval=10)
    def _receive_metric_data(self, metric):
        """
        All metrics are collected, as filtering is done more finegraded when mapping to tables
        > example_definition = {"type": "energy",
        >                       "name": "power",
        >                       "description": "Total energy consumed (in kWh)",
        >                       "mtype": "counter",
        >                       "unit": "Wh",
        >                       "tags": ["device", "id"]}
        > example_metric = {"source": "OpenMotics",
        >                   "type": "energy",
        >                   "metric": "power",
        >                   "timestamp": 1497677091,
        >                   "device": "OpenMotics energy ID1",
        >                   "id": 0,
        >                   "value": 1234}
        All metrics are grouped by their source, type and tags. As soon as a new timestamp is received, the pending
        group is send to InfluxDB and a new group is started. The tags from a group are based on the metric's definition's
        tags. The fields (values) are the metric > value pairs.
        > example_group = ["energy",
        >                  {"device": "OpenMotics energy ID1",
        >                   "id": 0},
        >                  {"power": 1234,
        >                   "power_counter": 1234567,
        >                   "voltage": 234}]
        """
        try:
            if self._enabled is False or self._definitions is None:
                return

            metric_type = metric['type']
            source = metric['source'].lower()
            timestamp = metric['timestamp'] * 1000000000
            value = metric['value']

            definition = self._definitions.get(metric['source'], {}).get(metric_type, {}).get(metric['metric'])
            if definition is None:
                return

            if isinstance(value, basestring):
                value = '"{0}"'.format(value)
            if isinstance(value, bool):
                value = str(value)
            if isinstance(value, int):
                value = '{0}i'.format(value)
            tags = {'type': source}
            for tag in definition['tags']:
                if isinstance(metric[tag], basestring):
                    tags[tag] = metric[tag].replace(' ', '\ ')
                else:
                    tags[tag] = metric[tag]

            entries = self._pending_metrics.setdefault(source, {}).setdefault(metric_type, [])
            found = False
            for pending in entries[:]:
                this_one = True
                for tag in definition['tags']:
                    if tags[tag] != pending[1].get(tag):
                        this_one = False
                        break
                if this_one is True:
                    if pending[0] == timestamp:
                        found = True
                        pending[2][metric['metric']] = value
                    else:
                        data = self._build_entry(metric_type, pending[1], pending[2], pending[0])
                        self._send_queue.appendleft(data)
                        entries.remove(pending)
                    break
            if found is False:
                entries.append([timestamp, tags, {metric['metric']: value}])
        except Exception as ex:
            self.logger('Error receiving metrics: {0}'.format(ex))

    @staticmethod
    def _build_entry(key, tags, value, timestamp):
        if isinstance(value, dict):
                values = ','.join('{0}={1}'.format(vname, vvalue)
                                  for vname, vvalue in value.iteritems())
        else:
            values = 'value={0}'.format(value)
        return '{0},{1} {2}{3}'.format(key,
                                       ','.join('{0}={1}'.format(tname, tvalue)
                                                for tname, tvalue in tags.iteritems()),
                                       values,
                                       '' if timestamp is None else ' {:.0f}'.format(timestamp))

    def _sender(self):
        while True:
            try:
                data = []
                try:
                    while True:
                        data.append(self._send_queue.pop())
                        if len(data) == self._batch_size:
                            raise IndexError()
                except IndexError:
                    pass
                if len(data) > 0:
                    self._batch_sizes.append(len(data))
                    self._queue_sizes.append(len(self._send_queue))
                    response = requests.post(url=self._endpoint,
                                             data='\n'.join(data),
                                             headers=self._headers,
                                             auth=self._auth,
                                             verify=False)
                    if response.status_code != 204:
                        self.logger('Send failed, received: {0} ({1})'.format(response.text, response.status_code))
                    if self._stats_time < time.time() - 1800:
                        self._stats_time = time.time()
                        self.logger('Queue size stats: {0:.2f} min, {1:.2f} avg, {2:.2f} max'.format(
                            min(self._queue_sizes),
                            sum(self._queue_sizes) / float(len(self._queue_sizes)),
                            max(self._queue_sizes)
                        ))
                        self.logger('Batch size stats: {0:.2f} min, {1:.2f} avg, {2:.2f} max'.format(
                            min(self._batch_sizes),
                            sum(self._batch_sizes) / float(len(self._batch_sizes)),
                            max(self._batch_sizes)
                        ))
                        self._batch_sizes = []
                        self._queue_sizes = []
            except Exception as ex:
                self.logger('Error sending from queue: {0}'.format(ex))
            time.sleep(0.1)

    @om_expose
    def get_config_description(self):
        return json.dumps(InfluxDB.config_description)

    @om_expose
    def get_config(self):
        return json.dumps(self._config)

    @om_expose
    def set_config(self, config):
        config = json.loads(config)
        for key in config:
            if isinstance(config[key], basestring):
                config[key] = str(config[key])
        self._config_checker.check_config(config)
        self._config = config
        self._read_config()
        self.write_config(config)
        return json.dumps({'success': True})
