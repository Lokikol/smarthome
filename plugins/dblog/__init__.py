#!/usr/bin/env python3
# vim: set encoding=utf-8 tabstop=4 softtabstop=4 shiftwidth=4 expandtab
#########################################################################
# Copyright 2013 Marcus Popp                               marcus@popp.mx
#########################################################################
#  This file is part of SmartHome.py.    http://mknx.github.io/smarthome/
#
#  SmartHome.py is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  SmartHome.py is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with SmartHome.py. If not, see <http://www.gnu.org/licenses/>.
#########################################################################

import logging
import datetime
import functools
import time
import threading
import lib.db
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger('')


class DbLog():

    # SQL queries: {item} = item table name, {log} = log table name
    # time, item_id, val_str, val_num, val_bool, changed
    _setup = {
      '1' : "CREATE TABLE {log} (time BIGINT, item_id INTEGER, duration BIGINT, val_str TEXT, val_num REAL, val_bool BOOLEAN, changed BIGINT);",
      '2' : "CREATE TABLE {item} (id INTEGER, name varchar(255), time BIGINT, val_str TEXT, val_num REAL, val_bool BOOLEAN, changed BIGINT);",
      '3' : "CREATE INDEX {log}_{item}_id_time ON {log} (item_id, time);",
      '4' : "CREATE INDEX {log}_{item}_id_changed ON {log} (item_id, changed);",
      '5' : "CREATE INDEX {item}_name ON item (name);"
    }

    def __init__(self, smarthome, db, connect, name= "default", prefix="", cycle=60):
        self._sh = smarthome
        self._dump_cycle = int(cycle)
        self._name = name
        self._tables = {table: table if prefix == "" else prefix + "_" + table for table in ["log", "item"]}
        self._buffer = {}
        self._buffer_lock = threading.Lock()
        self._dump_lock = threading.Lock()

        self._db = lib.db.Database(("" if prefix == "" else prefix.capitalize() + "_") + "DbLog", self._sh.dbapi(db), connect)
        self._db.connect()
        self._db.setup({i: self._prepare(query) for i, query in self._setup.items()})

        smarthome.scheduler.add('DbLog dump ' + name + ("" if prefix == "" else " [" + prefix + "]"), self._dump, cycle=self._dump_cycle, prio=5)

    def parse_item(self, item):
        if 'dblog' in item.conf and item.conf['dblog'] == self._name:
            self._buffer[item] = []
            item.series = functools.partial(self._series, item=item.id())
            item.db = functools.partial(self._single, item=item.id())
            item.dblog = self
            return self.update_item
        else:
            return None

    def run(self):
        self.alive = True

    def stop(self):
        self.alive = False
        self._dump(True)
        self._db.close()

    def update_item(self, item, caller=None, source=None, dest=None):
        start = self._timestamp(item.prev_change())
        end = self._timestamp(item.last_change())

        self._buffer[item].append((start, end - start, item.prev_value()))

    def id(self, item):
        id = self._db.fetchone(self._prepare("SELECT id FROM {item} where name = ?;"), (item.id(),))
        if id == None:
            id = self._db.fetchone(self._prepare("SELECT MAX(id) FROM {item};"))

            cur = self._db.cursor()
            self._db.execute(self._prepare("INSERT INTO {item}(id, name) VALUES(?,?);"), (1 if id[0] == None else id[0]+1, item.id()), cur)
            id = self._db.fetchone(self._prepare("SELECT id FROM {item} where name = ?;"), (item.id(),), cur)
            cur.close()

        return id[0]

    def updateItem(self, id, time, duration=0, val=None, it=None, changed=None, cur=None):
        params = [time]
        params.extend(self._item_value_tuple(it, val))
        params.append(changed)
        params.append(id)
        self._db.execute(self._prepare("UPDATE {item} SET time = ?, val_str = ?, val_num = ?, val_bool = ?, changed = ? WHERE id = ?;"), tuple(params), cur)

    def insertLog(self, id, time, duration=0, val=None, it=None, changed=None, cur=None):
        params = [time, id, duration]
        params.extend(self._item_value_tuple(it, val))
        params.append(changed)
        self._db.execute(self._prepare("INSERT INTO {log} VALUES (?,?,?,?,?,?,?);"), tuple(params), cur)

    def updateLog(self, id, time, duration=0, val=None, it=None, changed=None, cur=None):
        params = [duration]
        params.extend(self._item_value_tuple(it, val))
        params.append(changed)
        params.append(id)
        params.append(time)
        self._db.execute(self._prepare("UPDATE {log} SET duration = ?, val_str = ?, val_num = ?, val_bool = ?, changed = ? WHERE item_id = ? AND time = ?;"), tuple(params), cur)

    def readLog(self, id, time, cur = None):
        params = [id, time]
        return self._db.fetchall(self._prepare("SELECT * FROM {log} WHERE item_id = ? AND time = ?;"), tuple(params), cur)

    def deleteLog(self, id, time = None, time_start = None, time_end = None, changed = None, changed_start = None, changed_end = None, cur = None):
        params = [id]
        params.append(time)
        params.append(1 if time          == None else 0)
        params.append(time_start)
        params.append(1 if time_start    == None else 0)
        params.append(time_end)
        params.append(1 if time_end      == None else 0)
        params.append(changed)
        params.append(1 if changed       == None else 0)
        params.append(changed_start)
        params.append(1 if changed_start == None else 0)
        params.append(changed_end)
        params.append(1 if changed_end   == None else 0)

        self._db.execute(self._prepare(
            "DELETE FROM {log} WHERE "
            "  (item_id = ?         ) AND "
            "  (time    = ? OR 1 = ?) AND "
            "  (time    > ? OR 1 = ?) AND "
            "  (time    < ? OR 1 = ?) AND "
            "  (changed = ? OR 1 = ?) AND "
            "  (changed > ? OR 1 = ?) AND "
            "  (changed < ? OR 1 = ?);    "), tuple(params), cur)

    def db(self):
        return self._db

    def _item_value_tuple(self, item_type, item_val):
        if item_type == 'num':
           val_str = None
           val_num = float(item_val)
           val_bool = None
        elif item_type == 'bool':
           val_str = None
           val_num = None
           val_bool = bool(item_val)
        else:
           val_str = str(item_val)
           val_num = None
           val_bool = None

        return [val_str, val_num, val_bool]

    def _datetime(self, ts):
        return datetime.datetime.fromtimestamp(ts / 1000, self._sh.tzinfo())

    def _prepare(self, query):
        return query.format(**self._tables)

    def _dump(self, finalize=False, items=None):
        if self._dump_lock.acquire(timeout=60) == False:
            logger.warning('Skipping dump, since other dump running!')
            return

        logger.debug('Starting dump')

        if items == None:
            self._buffer_lock.acquire()
            items = list(self._buffer.keys())
            self._buffer_lock.release()

        for item in items:
            self._buffer_lock.acquire()
            tuples = self._buffer[item]
            self._buffer[item] = []
            self._buffer_lock.release()

            if len(tuples) or finalize:

                # Test connectivity
                if self._db.verify(5) == 0:
                    logger.error("DbLog: Connection not recovered, skipping dump");
                    self._dump_lock.release()
                    return

                # Can't lock, restore data
                if not self._db.lock(300):
                    self._buffer_lock.acquire()
                    if item in self._buffer:
                        self._buffer[item] = tuples + self._buffer[item]
                    else:
                        self._buffer[item] = tuples
                    self._buffer_lock.release()
                    if finalize:
                        logger.error("DbLog: can't dump {} items due to fail to acquire lock!".format(len(self._buffer)))
                    else:
                        logger.error("DbLog: can't dump {} items due to fail to acquire lock - will try on next dump".format(len(self._buffer)))
                    self._dump_lock.release()
                    return

                try:
                    changed = self._timestamp(self._sh.now())

                    id = self.id(item)

                    # Get current values of item
                    start = self._timestamp(item.last_change())
                    end = self._timestamp(self._sh.now())
                    val = item()

                    # When finalizing (e.g. plugin shutdown) add current value to item and log
                    if finalize:
                        _update = (end, val, changed)

                        current = (start, end - start, val)
                        tuples.append(current)

                    else:
                        _update = (start, val, changed)

                    # Dump tuples
                    logger.debug('Dumping {}/{} with {} values'.format(item.id(), id, len(tuples)))

                    cur = self._db.cursor()
                    for t in tuples:
                        self.insertLog(id, t[0], t[1], t[2], item.type(), changed, cur)

                    self.updateItem(id, _update[0], None, _update[1], item.type(), _update[2], cur)

                    cur.close()

                    self._db.commit()
                except Exception as e:
                    logger.warning("DbLog: problem updating {}: {}".format(item.id(), e))
                self._db.release()
        logger.debug('Dump completed')
        self._dump_lock.release()

    def _series(self, func, start, end='now', count=100, ratio=1, update=False, step=None, sid=None, item=None):
        init = not update
        if sid is None:
            sid = item + '|' + func + '|' + start + '|' + end
        istart = self._parse_ts(start)
        iend = self._parse_ts(end)
        if step is None:
            if count != 0:
                step = int((iend - istart) / count)
            else:
                step = iend - istart
        reply = {'cmd': 'series', 'series': None, 'sid': sid}
        reply['params'] = {'update': True, 'item': item, 'func': func, 'start': iend, 'end': end, 'step': step, 'sid': sid}
        reply['update'] = self._sh.now() + datetime.timedelta(seconds=int(step / 1000))
        where = self._prepare(" FROM {log} WHERE item_id = ? AND time + duration > ? AND time <= ? GROUP BY ROUND(time / ?)")
        if func == 'avg':
            query = "SELECT MIN(time), ROUND(AVG(val_num * duration) / AVG(duration), 2)" + where + " ORDER BY time ASC"
        elif func == 'min':
            query = "SELECT MIN(time), MIN(val_num)" + where
        elif func == 'max':
            query = "SELECT MIN(time), MAX(val_num)" + where
        elif func == 'on':
            query = "SELECT MIN(time), ROUND(SUM(val_bool * duration) / SUM(duration), 2)" + where + " ORDER BY time ASC"
        else:
            raise NotImplementedError
        _item = self._sh.return_item(item)
        if self._buffer[_item] != []:
            self._dump(items=[_item])
        tuples = self._fetch(query, item, [istart, iend, step])
        if tuples:
            if istart > tuples[0][0]:
                tuples[0] = (istart, tuples[0][1])
            if end != 'now':
                tuples.append((iend, tuples[-1][1]))
        else:
            tuples = []
        item_change = self._timestamp(_item.last_change())
        if item_change < iend:
            value = float(_item())
            if item_change < istart:
                tuples.append((istart, value))
            elif init:
                tuples.append((item_change, value))
            if init:
                tuples.append((iend, value))
        reply['series'] = tuples
        return reply

    def _single(self, func, start, end='now', item=None):
        start = self._parse_ts(start)
        end = self._parse_ts(end)
        where = self._prepare(" FROM {log} WHERE item_id = ? AND time + duration > ? AND time <= ?")
        if func == 'avg':
            query = "SELECT ROUND(AVG(val_num * duration) / AVG(duration), 2)" + where
        elif func == 'min':
            query = "SELECT MIN(val_num)" + where
        elif func == 'max':
            query = "SELECT MAX(val_num)" + where
        elif func == 'on':
            query = "SELECT ROUND(SUM(val_bool * duration) / SUM(duration), 2)" + where
        else:
            logger.warning("Unknown export function: {0}".format(func))
            return
        _item = self._sh.return_item(item)
        if self._buffer[_item] != []:
            self._dump(items=[_item])
        tuples = self._fetch(query, item, [start, end])
        if tuples is None:
            return
        return tuples[0][0]

    def _fetch(self, query, item, params):
        tuples = None
        if not self._db.lock(300):
            logger.error("DbLog: can't fetch data due to fail to acquire lock")
            return None
        try:
            id = self._db.fetchone("SELECT id FROM item where name = ?", (item,))
            params.insert(0, id[0])
            tuples = self._db.fetchall(query, tuple(params))
        except Exception as e:
            logger.warning("DbLog: Error fetching data for {}: {}".format(item, e))
        self._db.release()
        logger.debug("Fetch {} (args {}): {}".format(query, params, tuples))
        return None if tuples is None else list(tuples)

    def _parse_ts(self, frame):
        minute = 60 * 1000
        hour = 60 * minute
        day = 24 * hour
        week = 7 * day
        month = 30 * day
        year = 365 * day

        _frames = {'i': minute, 'h': hour, 'd': day, 'w': week, 'm': month, 'y': year}
        try:
            return int(frame)
        except:
            pass
        ts = self._timestamp(self._sh.now())
        if frame == 'now':
            fac = 0
            frame = 0
        elif frame[-1] in _frames:
            fac = _frames[frame[-1]]
            frame = frame[:-1]
        else:
            return frame
        try:
            ts = ts - int(float(frame) * fac)
        except:
            logger.warning("DbLog: Unknown time frame '{0}'".format(frame))
        return ts

    def _timestamp(self, dt):
        return int(time.mktime(dt.timetuple())) * 1000 + int(dt.microsecond / 1000)
