# -*- coding: utf-8 -*-
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import os
import time
import random

from sqlalchemy import event, exc, select

from airflow.utils.log.logging_mixin import LoggingMixin

log = LoggingMixin().log


def setup_event_handlers(
        engine,
        reconnect_timeout_seconds,
        initial_backoff_seconds=0.2,
        max_backoff_seconds=120):

    @event.listens_for(engine, "engine_connect")
    def ping_connection(connection, branch):
        """
        Pessimistic SQLAlchemy disconnect handling. Ensures that each
        connection returned from the pool is properly connected to the database.

        http://docs.sqlalchemy.org/en/rel_1_1/core/pooling.html#disconnect-handling-pessimistic
        """
        if branch:
            # "branch" refers to a sub-connection of a connection,
            # we don't want to bother pinging on these.
            return

        start = time.time()
        backoff = initial_backoff_seconds

        # turn off "close with result".  This flag is only used with
        # "connectionless" execution, otherwise will be False in any case
        save_should_close_with_result = connection.should_close_with_result

        while True:
            connection.should_close_with_result = False

            try:
                connection.scalar(select([1]))
                # If we made it here then the connection appears to be healty
                break
            except exc.DBAPIError as err:
                if time.time() - start >= reconnect_timeout_seconds:
                    log.error(
                        "Failed to re-establish DB connection within %s secs: %s",
                        reconnect_timeout_seconds,
                        err)
                    raise
                if err.connection_invalidated:
                    log.warning("DB connection invalidated. Reconnecting...")

                    # Use a truncated binary exponential backoff. Also includes
                    # a jitter to prevent the thundering herd problem of
                    # simultaneous client reconnects
                    backoff += backoff * random.random()
                    time.sleep(min(backoff, max_backoff_seconds))

                    # run the same SELECT again - the connection will re-validate
                    # itself and establish a new connection.  The disconnect detection
                    # here also causes the whole connection pool to be invalidated
                    # so that all stale connections are discarded.
                    continue
                else:
                    log.error(
                        "Unknown database connection error. Not retrying: %s",
                        err)
                    raise
            finally:
                # restore "close with result"
                connection.should_close_with_result = save_should_close_with_result


    @event.listens_for(engine, "connect")
    def connect(dbapi_connection, connection_record):
        connection_record.info['pid'] = os.getpid()


    @event.listens_for(engine, "checkout")
    def checkout(dbapi_connection, connection_record, connection_proxy):
        pid = os.getpid()
        if connection_record.info['pid'] != pid:
            connection_record.connection = connection_proxy.connection = None
            raise exc.DisconnectionError(
                "Connection record belongs to pid {}, "
                "attempting to check out in pid {}".format(connection_record.info['pid'], pid)
            )
