# -*- coding: utf-8 -*-
# @author: Tomas Vitvar, https://vitvar.com, tomas@vitvar.com

import os
import sys
import time
import threading
import logging
import re
import ast
import pickle

from queue import Queue
from yamc.utils import Map, randomString, PythonExpression, deep_merge
from yamc.component import WorkerComponent


class HealthCheckException(Exception):
    pass


class Writer(WorkerComponent):
    def __init__(self, config, component_id):
        super().__init__(config, component_id)
        self.config = config.writer(component_id)

        self.write_interval = self.config.value_int("write_interval", default=10)
        self.write_empty = self.config.value_int("write_empty", default=True)
        self.healthcheck_interval = self.config.value_int("healthcheck_interval", default=20)
        self.disable_backlog = self.config.value_int("disable_backlog", default=False)
        self.batch_size = self.config.value_int("batch_size", default=100)
        self._is_healthy = False
        self.last_healthcheck = 0
        self.queue = Queue()
        self.backlog = Backlog(self, config)
        self.thread = None
        self.write_event = threading.Event()

    def healthcheck(self):
        pass

    def is_healthy(self):
        if not self._is_healthy and time.time() - self.last_healthcheck > self.healthcheck_interval:
            try:
                self.last_healthcheck = time.time()
                self.healthcheck()
                self._is_healthy = True
                self.log.info("The healthcheck succeeded.")
            except Exception as e:
                self.log.error("The healthcheck failed on %s" % (str(e)))
                self.log.info("The backlog size is %d." % (self.backlog.size()))
                self._is_healthy = False
        return self._is_healthy

    def process_conditional_dict(self, d, scope, path=""):
        def _error(s):
            return Exception(f"Invalid conditional dict. {s}.")

        def _deep_eval(d2, path=""):
            if isinstance(d2, dict):
                for key, value in d2.items():
                    d2[key] = _deep_eval(value, path + "/" + key)
            elif isinstance(d2, list):
                for i, x in enumerate(d2):
                    d2[i] = _deep_eval(x, path + f"[{i}]/")
            elif isinstance(d2, PythonExpression):
                try:
                    d2 = d2.eval(scope)
                except Exception as e:
                    raise _error(f"The Python expression '{d2.expr_str}' failed in {path}. %s." % (str(e)))
            return d2

        def _process_block(c, data, path=""):
            if_expr = c.get("$if")
            if_opts = [x.strip() for x in c.get("$opts", "").split(",")]
            if if_expr is not None:
                path = path + "/$if"
                if not isinstance(if_expr, PythonExpression):
                    raise _error(f"The '$if' expression must be a Python expression in {path}")
            try:
                eval_result = if_expr is None or if_expr.eval(scope)
            except Exception as e:
                raise _error(f"Error: {if_expr.expr_str} in {path}. {str(e)}")
            if eval_result and (
                "$onoff" not in if_opts or c.get("__last_if_eval") is None or eval_result != c.get("__last_if_eval")
            ):
                df2 = c.get("$def")
                if df2 is not None:
                    data = deep_merge(self.process_conditional_dict(c, scope, path + "/$def"), data)
                else:
                    data = deep_merge(
                        _deep_eval(
                            {k: v for k, v in c.items() if k not in ["$if", "$opts", "__last_if_eval"]},
                            path,
                        ),
                        data,
                    )
            if if_expr is not None:
                c["__last_if_eval"] = eval_result
            return data

        data = {}
        df = d.get("$def")
        if df is None:
            raise _error(f"There must be '$def' property in {path}")
        if isinstance(df, list):
            for i, c in enumerate(df):
                data = _process_block(c, data, path + f"/$def[{i}]")
        elif isinstance(df, PythonExpression):
            r = df.eval(scope)
            if not isinstance(r, dict):
                raise _error(f"The '$def' expression must return a dictionary in {path}")
            data = _process_block(r, data, path + "/$def")
        else:
            data = _process_block(df, data, path + "/$def")
        # self.log.debug(f"The conditional dict resulted in the following data: {data}")
        return data

    def write(self, collector_id, data, writer_def, scope=None):
        """
        Non-blocking write operation. This method is called from a collector and must be non-blocking
        so that the collector can process collecting of measurements.
        """
        self.log.debug(f"Writing data using the following writer definition: {writer_def}")
        if len(writer_def) == 0:
            raise Exception("Writer defintion is empty!")

        data_out = []

        def _add_to_queue(data_item):
            _scope = Map() if scope is None else scope
            _scope.data = data_item
            _data = Map(
                collector_id=collector_id,
                data=self.process_conditional_dict(writer_def, self.base_scope(_scope)),
            )
            data_out.append(_data)
            if len(_data["data"]) == 0 and not self.write_empty:
                pass
            elif self.is_healthy():
                self.queue.put(_data)
            else:
                if not self.disable_backlog:
                    self.backlog.put([_data])

        if isinstance(data, list):
            for data_item in data:
                _add_to_queue(data_item)
        else:
            _add_to_queue(data)
        if len(data_out) == 1:
            self.log.debug(f"The following data will be written out: {str(data_out)}")
        else:
            self.log.debug(
                f"The following data will be written out (length={len(data_out)}, stripped): {str(data_out[0])}"
            )
        if self.write_interval == 0:
            self.write_event.set()

    def do_write(self, data):
        """
        Abstract method to write data to a desintation writer.
        """
        pass

    def worker(self, exit_event):
        """
        Thread worker method.
        """

        def _process_qeue():
            if self.is_healthy() and self.queue.qsize() > 0:
                # create the batch
                batch = []
                while self.queue.qsize() > 0 and len(batch) < self.batch_size:
                    batch.append(self.queue.get())
                    self.queue.task_done()

                # write the batch
                try:
                    self.log.debug(
                        "Writing the batch, batch-size=%d, queue-size=%d." % (len(batch), self.queue.qsize())
                    )
                    if not self.base_config.test:
                        self.do_write(batch)
                    else:
                        self.log.debug("Running in test mode, the writing operation is disabled.")
                except HealthCheckException as e:
                    self.log.error(
                        "Cannot write the batch due to writer's problem: %s. The batch will be stored in the backlog."
                        % (str(e))
                    )
                    self._is_healthy = False
                    self.backlog.put(batch)
                except Exception as e:
                    self.log.error(
                        "Cannot write the batch. It will be discarded due to the following error: %s" % (str(e))
                    )

        while not exit_event.is_set():
            _process_qeue()
            if self.is_healthy():
                self.backlog.process()
            exit_event.wait(self.write_interval)

        # process all remaining items in the queue if possible
        self.log.info("Ending the writer thread .")
        _process_qeue()

        # write unprocessed items to the backlog
        if self.queue.qsize() > 0:
            self.log.info(
                "There are %d unprocessed items in the queue of the writer. Writing them all to the backlog."
                % (self.queue.qsize())
            )
            batch = []
            while self.queue.qsize() > 0:
                batch.append(self.queue.get())
                self.queue.task_done()
            self.backlog.put(batch)

        self.log.info("The writer thread ended.")


class Backlog:
    def __init__(self, writer, config):
        self.writer = writer
        self.config = config
        self.log = writer.log
        self.backlog_dir = config.get_dir_path(config.data_dir + "/backlog/" + self.writer.component_id)
        os.makedirs(self.backlog_dir, exist_ok=True)
        self.refresh()

    def refresh(self):
        files = filter(
            lambda x: os.path.isfile(os.path.join(self.backlog_dir, x)) and re.match("items_[a-zA-Z0-9]+.data$", x),
            os.listdir(self.backlog_dir),
        )
        files = [f for f in files]
        files.sort(key=lambda x: os.path.getmtime(os.path.join(self.backlog_dir, x)))
        self.all_files = files

    def put(self, items):
        if self.writer.base_config.test:
            self.log.info("Running in test mode, the backlog item will not be created")
        else:
            file = "items_%s.data" % randomString()
            with open(os.path.join(self.backlog_dir, file), "wb") as f:
                pickle.dump(items, f, protocol=pickle.HIGHEST_PROTOCOL)
            self.all_files.append(file)
            self.log.debug("Writing data to the writer's backlog. The backlog size is %d." % (self.size()))

    def peek(self, size):
        files = self.all_files[: min(size, len(self.all_files))]
        data = []
        for file in files:
            with open(os.path.join(self.backlog_dir, file), "rb") as f:
                data += pickle.load(f)
        return files, data

    def remove(self, files):
        if not self.writer.base_config.test:
            for file in files:
                os.remove(os.path.join(self.backlog_dir, file))
        else:
            self.log.info("Running in test mode, removing of backlog files is disabled.")
        self.all_files = [x for x in self.all_files if x not in files]
        self.log.debug("Removing data from the writer's backlog. The backlog size is %s." % (self.size()))

    def size(self):
        return len(self.all_files)

    def process(self):
        if self.size() > 0:
            self.log.info(
                "There are %d items in the backlog. Writing items in batches of %d..."
                % (self.size(), self.writer.batch_size)
            )
            while self.size() > 0:
                batch_files, batch = self.peek(self.writer.batch_size)
                try:
                    if not self.writer.base_config.test:
                        self.writer.do_write(batch)
                    else:
                        self.log.info(
                            "Running in test mode, writing of backlog files is disabled (the backlog will be removed from memory only)."
                        )
                    self.remove(batch_files)
                except Exception as e:
                    self.log.error(
                        "Cannot write item from the writer's backlog due to: %s" % (str(e)),
                        exc_info=self.writer.base_config.debug,
                    )
                    self.writer._is_healthy = False
                    break
            self.log.info("The processing of the backlog finished. The backlog size is %s." % self.size())
