# -*- coding: utf-8 -*-
# @author: Tomas Vitvar, https://vitvar.com, tomas@vitvar.com

import time
import logging
import os
import threading

from .writer import Writer, HealthCheckException
from yamc.utils import import_class, randomString
from datetime import datetime

from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

# we need a new manager not to mess up with yamc logging configuration
manager = logging.Manager(logging.RootLogger(logging.INFO))


class CsvRotatingFileHandler(RotatingFileHandler):
    """
    Rotating file handler that writes a header to the file if the file is empty.
    """

    def __init__(self, *args, **kwargs):
        self.header = kwargs.pop("header", None)
        super().__init__(*args, **kwargs)

    def _open(self):
        handler = super()._open()
        if handler.tell() == 0 and self.header is not None:
            handler.write(self.header + "\n")
            handler.flush()
        return handler


class CsvTimeRotatingFileHandler(TimedRotatingFileHandler):
    """
    Time rotating file handler that writes a header to the file if the file is empty.
    """

    def __init__(self, *args, **kwargs):
        self.header = kwargs.pop("header", None)
        super().__init__(*args, **kwargs)

    def _open(self):
        handler = super()._open()
        if handler.tell() == 0 and self.header is not None:
            handler.write(self.header + "\n")
            handler.flush()
        return handler


class CsvWriter(Writer):
    """
    Writer that writes data to a CSV file.
    """

    def __init__(self, config, component_id):
        super().__init__(config, component_id)
        self.handler_def = self.config.value("handler")
        self.handler_class = import_class(self.handler_def["class"])
        self.csv_writer = manager.getLogger(f"CsvWriter_{randomString()}")
        self.csv_writer.setLevel(logging.INFO)
        if "filename" in self.handler_def:
            self.handler_def["filename"] = self.config.get_dir_path(self.handler_def["filename"], check=False)
            os.makedirs(os.path.dirname(self.handler_def["filename"]), exist_ok=True)
        self.initialized = False
        self.disabled = False
        self.lock = threading.Lock()

    def healthcheck(self):
        super().healthcheck()

    def do_write(self, items):
        """
        Writes the data to the CSV file.
        """

        def _format_value(v):
            if isinstance(v, str):
                return '"' + v.replace('"', '\\"').replace("\n", " ") + '"'
            elif isinstance(v, datetime):
                return '"' + str(v) + '"'
            else:
                return str(v)

        with self.lock:
            if not self.initialized and not self.disabled:
                try:
                    handler = self.handler_class(**{k: v for k, v in self.handler_def.items() if k != "class"})
                    self.csv_writer.addHandler(handler)
                    self.initialized = True
                except Exception as e:
                    self.log.error(f"Error initializing the CSV writer: {e}. The writer will be disabled.")
                    self.disabled = True

            if self.initialized:
                self.log.debug(f"Writing {len(items)} rows to {self.handler_def['filename']}")
                for data in items:
                    line = [_format_value(v) for k, v in data.data.items()]
                    self.csv_writer.info(",".join(line))
