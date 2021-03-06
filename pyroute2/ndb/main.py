#
# NDB is NOT a production but a proof-of-concept
#
# It is intended to become IPDB version 2.0 that can handle
# thousands of network objects -- something that IPDB can not
# due to memory consupmtion
#
#
# Proposed design:
#
# 0. multiple event sources -- IPRoute (Linux), RTMSocket (BSD), etc
# 1. the main loop dispatches incoming events to plugins
# 2. plugins store serialized events as records in an internal DB (SQL)
# 3. plugins provide an API to access records as Python objects
# 4. objects are spawned only on demand
# 5. plugins provide transactional API to change objects + OS reflection

import json
import sqlite3
import logging
import threading
from socket import AF_INET
from socket import AF_INET6
from pyroute2 import IPRoute
from pyroute2.ndb import dbschema
from pyroute2.common import AF_MPLS
try:
    import queue
except ImportError:
    import Queue as queue


def target_adapter(value):
    return json.dumps(value)


sqlite3.register_adapter(list, target_adapter)


class ShutdownException(Exception):
    pass


class NDB(object):

    def __init__(self, nl=None, db_uri=':memory:'):

        self._dbm_thread = None
        self._dbm_ready = threading.Event()
        self._event_queue = None
        self._nl = nl
        self._db_uri = db_uri
        self.db = None
        self.initdb()

    def initdb(self):
        # stop DBM if exists
        if self._dbm_thread is not None:
            self._event_queue.put(ShutdownException("restart NDB"))
            self._dbm_thread.join()

        # FIXME
        # stop event sources!
        # FIXME

        # start event sources
        if self._nl is None:
            ipr = IPRoute()
            self.nl = {'localhost': ipr}
        elif isinstance(self._nl, dict):
            self.nl = dict([(x[0], x[1].clone()) for x in self._nl.items()])
        else:
            self.nl = {'localhost': self._nl.clone()}
        for target in self.nl:
            self.nl[target].bind()

        # start the main loop
        self._dbm_ready.clear()
        self._dbm_thread = threading.Thread(target=self.__dbm__,
                                            name='NDB main loop')
        self._dbm_thread.setDaemon(True)
        self._dbm_thread.start()

    def close(self):
        if self.db:
            self._event_queue.put(('localhost', (ShutdownException(), )))
            self.db.commit()
            self.db.close()
            for (target, channel) in self.nl.items():
                channel.close()

    def __dbm__(self):
        ##
        # Database management thread
        ##
        event_map = {type(self._dbm_ready): [lambda t, x: x.set()]}
        self._event_queue = event_queue = queue.Queue()
        #
        # ACHTUNG!
        # check_same_thread=False
        #
        # Do NOT write into the DB from ANY other thread!
        #
        self.db = sqlite3.connect(self._db_uri, check_same_thread=False)

        def default_handler(target, event):
            if isinstance(event, Exception):
                raise event
            logging.warning('unsupported event ignored: %s' % type(event))

        self.dbschema = dbschema.init(self.db, id(threading.current_thread()))
        for (event, handler) in self.dbschema.event_map.items():
            if event not in event_map:
                event_map[event] = []
            event_map[event].append(handler)

        # initial load
        for (target, channel) in tuple(self.nl.items()):
            event_queue.put((target, channel.get_links()))
            event_queue.put((target, channel.get_neighbours()))
            event_queue.put((target, channel.get_routes(family=AF_INET)))
            event_queue.put((target, channel.get_routes(family=AF_INET6)))
            event_queue.put((target, channel.get_routes(family=AF_MPLS)))
            event_queue.put((target, channel.get_addr()))
        event_queue.put(('localhost', (self._dbm_ready, ), ))
        #
        for (target, channel) in tuple(self.nl.items()):
            def t(event_queue, target, channel):
                while True:
                    event_queue.put((target, channel.get()))

            th = threading.Thread(target=t,
                                  args=(event_queue, target, channel),
                                  name='NDB event source: %s' % (target))
            th.setDaemon(True)
            th.start()

        while True:
            target, events = event_queue.get()
            for event in events:
                handlers = event_map.get(event.__class__, [default_handler, ])
                for handler in handlers:
                    try:
                        handler(target, event)
                    except ShutdownException:
                        return
                    except:
                        import traceback
                        traceback.print_exc()
