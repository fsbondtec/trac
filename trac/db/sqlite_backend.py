# -*- coding: utf-8 -*-
#
# Copyright (C) 2005-2020 Edgewall Software
# Copyright (C) 2005 Christopher Lenz <cmlenz@gmx.de>
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution. The terms
# are also available at https://trac.edgewall.org/wiki/TracLicense.
#
# This software consists of voluntary contributions made by many
# individuals. For the exact contribution history, see the revision
# history and logs, available at https://trac.edgewall.org/log/.
#
# Author: Christopher Lenz <cmlenz@gmx.de>

import os
import re
import weakref

from genshi.builder import tag

from trac.config import ConfigurationError, ListOption
from trac.core import Component, TracError, implements
from trac.db.api import IDatabaseConnector
from trac.db.util import ConnectionWrapper, IterableCursor
from trac.util import get_pkginfo, getuser
from trac.util.translation import _, tag_

_like_escape_re = re.compile(r'([/_%])')

_glob_escape_re = re.compile(r'[*?\[]')

try:
    import pysqlite2.dbapi2 as sqlite
    have_pysqlite = 2
except ImportError:
    try:
        import sqlite3 as sqlite
        have_pysqlite = 2
    except ImportError:
        have_pysqlite = 0

if have_pysqlite == 2:
    # Force values to integers because PySQLite 2.2.0 had (2, 2, '0')
    sqlite_version = tuple([int(x) for x in sqlite.sqlite_version_info])
    sqlite_version_string = sqlite.sqlite_version

    class PyFormatCursor(sqlite.Cursor):
        def _rollback_on_error(self, function, *args, **kwargs):
            try:
                return function(self, *args, **kwargs)
            except sqlite.DatabaseError:
                self.cnx.rollback()
                raise
        def execute(self, sql, args=None):
            if args:
                sql = sql % (('?',) * len(args))
            return self._rollback_on_error(sqlite.Cursor.execute, sql,
                                           args or [])
        def executemany(self, sql, args):
            if not args:
                return
            sql = sql % (('?',) * len(args[0]))
            return self._rollback_on_error(sqlite.Cursor.executemany, sql,
                                           args)

    # EagerCursor taken from the example in pysqlite's repository:
    #
    #   http://code.google.com/p/pysqlite/source/browse/misc/eager.py
    #
    # Only change is to subclass it from PyFormatCursor instead of
    # sqlite.Cursor.

    class EagerCursor(PyFormatCursor):
        def __init__(self, con):
            PyFormatCursor.__init__(self, con)
            self.rows = []
            self.pos = 0

        def execute(self, *args):
            result = PyFormatCursor.execute(self, *args)
            self.rows = PyFormatCursor.fetchall(self)
            self.pos = 0
            return result

        def fetchone(self):
            try:
                row = self.rows[self.pos]
                self.pos += 1
                return row
            except IndexError:
                return None

        def fetchmany(self, num=None):
            if num is None:
                num = self.arraysize

            result = self.rows[self.pos:self.pos+num]
            self.pos += num
            return result

        def fetchall(self):
            result = self.rows[self.pos:]
            self.pos = len(self.rows)
            return result


# Mapping from "abstract" SQL types to DB-specific types
_type_map = {
    'int': 'integer',
    'int64': 'integer',
}


def _to_sql(table):
    sql = ["CREATE TABLE %s (" % table.name]
    coldefs = []
    for column in table.columns:
        ctype = column.type.lower()
        ctype = _type_map.get(ctype, ctype)
        if column.auto_increment:
            ctype = "integer PRIMARY KEY"
        elif len(table.key) == 1 and column.name in table.key:
            ctype += " PRIMARY KEY"
        coldefs.append("    %s %s" % (column.name, ctype))
    if len(table.key) > 1:
        coldefs.append("    UNIQUE (%s)" % ','.join(table.key))
    sql.append(',\n'.join(coldefs) + '\n);')
    yield '\n'.join(sql)
    for index in table.indices:
        unique = 'UNIQUE' if index.unique else ''
        yield "CREATE %s INDEX %s_%s_idx ON %s (%s);" % (unique, table.name,
              '_'.join(index.columns), table.name, ','.join(index.columns))


class SQLiteConnector(Component):
    """Database connector for SQLite.

    Database URLs should be of the form:
    {{{
    sqlite:path/to/trac.db
    }}}
    """
    implements(IDatabaseConnector)

    extensions = ListOption('sqlite', 'extensions',
        doc="""Paths to sqlite extensions, relative to Trac environment's
        directory or absolute. (''since 0.12'')""")

    memory_cnx = None

    def __init__(self):
        self._version = None
        self.error = None
        self._extensions = None

    def get_supported_schemes(self):
        if not have_pysqlite:
            self.error = _("Cannot load Python bindings for SQLite")
        elif sqlite_version >= (3, 3, 3) and sqlite.version_info[0] == 2 and \
                sqlite.version_info < (2, 0, 7):
            self.error = _("Need at least PySqlite %(version)s or higher",
                           version='2.0.7')
        elif (2, 5, 2) <= sqlite.version_info < (2, 5, 5):
            self.error = _("PySqlite 2.5.2 - 2.5.4 break Trac, please use "
                           "2.5.5 or higher")
        yield ('sqlite', -1 if self.error else 1)

    def get_connection(self, path, log=None, params={}):
        if not self._version:
            self._version = get_pkginfo(sqlite).get(
                'version', '%d.%d.%s' % sqlite.version_info)
            self.env.systeminfo.extend([('SQLite', sqlite_version_string),
                                        ('pysqlite', self._version)])
            self.required = True
        # construct list of sqlite extension libraries
        if self._extensions is None:
            self._extensions = []
            for extpath in self.extensions:
                if not os.path.isabs(extpath):
                    extpath = os.path.join(self.env.path, extpath)
                self._extensions.append(extpath)
        params['extensions'] = self._extensions
        if path == ':memory:':
            try:
                self.memory_cnx.cursor()
            except (AttributeError, sqlite.DatabaseError):
                # memory_cnx is None or database connection closed.
                self.memory_cnx = SQLiteConnection(path, log, params)
            return self.memory_cnx
        else:
            return SQLiteConnection(path, log, params)

    def get_exceptions(self):
        return sqlite

    def init_db(self, path, schema=None, log=None, params={}):
        if path != ':memory:':
            # make the directory to hold the database
            if os.path.exists(path):
                raise TracError(_("Database already exists at %(path)s",
                                  path=path))
            dir = os.path.dirname(path)
            if not os.path.exists(dir):
                os.makedirs(dir)
            if isinstance(path, unicode): # needed with 2.4.0
                path = path.encode('utf-8')
            # this direct connect will create the database if needed
            cnx = sqlite.connect(path, isolation_level=None,
                                 timeout=int(params.get('timeout', 10000)))
            cursor = cnx.cursor()
            _set_journal_mode(cursor, params.get('journal_mode'))
            _set_synchronous(cursor, params.get('synchronous'))
            cnx.isolation_level = 'DEFERRED'
        else:
            cnx = self.get_connection(path, log, params)
            cursor = cnx.cursor()
        if schema is None:
            from trac.db_default import schema
        for table in schema:
            for stmt in self.to_sql(table):
                cursor.execute(stmt)
        cursor.close()
        cnx.commit()

    def to_sql(self, table):
        return _to_sql(table)

    def alter_column_types(self, table, columns):
        """Yield SQL statements altering the type of one or more columns of
        a table.

        Type changes are specified as a `columns` dict mapping column names
        to `(from, to)` SQL type tuples.
        """
        for name, (from_, to) in sorted(columns.iteritems()):
            if _type_map.get(to, to) != _type_map.get(from_, from_):
                raise NotImplementedError('Conversion from %s to %s is not '
                                          'implemented' % (from_, to))
        return ()

    def backup(self, dest_file):
        """Simple SQLite-specific backup of the database.

        :param dest_file: Destination file basename
        """
        import shutil
        db_str = self.config.get('trac', 'database')
        try:
            db_str = db_str[:db_str.index('?')]
        except ValueError:
            pass
        db_name = os.path.join(self.env.path, db_str[7:])
        shutil.copy(db_name, dest_file)
        if not os.path.exists(dest_file):
            raise TracError(_("No destination file created"))
        return dest_file


class SQLiteConnection(ConnectionWrapper):
    """Connection wrapper for SQLite."""

    __slots__ = ['_active_cursors', '_eager']

    poolable = have_pysqlite and sqlite_version >= (3, 3, 8) \
                             and sqlite.version_info >= (2, 5, 0)

    def __init__(self, path, log=None, params={}):
        if have_pysqlite == 0:
            raise TracError(_("Cannot load Python bindings for SQLite"))
        self.cnx = None
        if path != ':memory:':
            if not os.access(path, os.F_OK):
                raise ConfigurationError(_('Database "%(path)s" not found.',
                                           path=path))

            dbdir = os.path.dirname(path)
            if not os.access(path, os.R_OK + os.W_OK) or \
                   not os.access(dbdir, os.R_OK + os.W_OK):
                raise ConfigurationError(tag_(
                    "The user %(user)s requires read _and_ write permissions "
                    "to the database file %(path)s and the directory it is "
                    "located in.", user=tag.tt(getuser()), path=tag.tt(path)))

        self._active_cursors = weakref.WeakKeyDictionary()
        timeout = int(params.get('timeout', 10.0))
        self._eager = params.get('cursor', 'eager') == 'eager'
        # eager is default, can be turned off by specifying ?cursor=
        if isinstance(path, unicode): # needed with 2.4.0
            path = path.encode('utf-8')
        cnx = sqlite.connect(path, detect_types=sqlite.PARSE_DECLTYPES,
                             isolation_level=None,
                             check_same_thread=sqlite_version < (3, 3, 1),
                             timeout=timeout)
        # load extensions
        extensions = params.get('extensions', [])
        if len(extensions) > 0:
            cnx.enable_load_extension(True)
            for ext in extensions:
                cnx.load_extension(ext)
            cnx.enable_load_extension(False)

        cursor = cnx.cursor()
        _set_journal_mode(cursor, params.get('journal_mode'))
        _set_synchronous(cursor, params.get('synchronous'))
        cursor.close()
        cnx.isolation_level = 'DEFERRED'
        ConnectionWrapper.__init__(self, cnx, log)

    def cursor(self):
        cursor = self.cnx.cursor((PyFormatCursor, EagerCursor)[self._eager])
        self._active_cursors[cursor] = True
        cursor.cnx = self
        return IterableCursor(cursor, self.log)

    def rollback(self):
        for cursor in self._active_cursors.keys():
            cursor.close()
        self.cnx.rollback()

    def cast(self, column, type):
        if sqlite_version >= (3, 2, 3):
            return 'CAST(%s AS %s)' % (column, _type_map.get(type, type))
        elif type == 'int':
            # hack to force older SQLite versions to convert column to an int
            return '1*' + column
        else:
            return column

    def concat(self, *args):
        return '||'.join(args)

    def drop_table(self, table):
        cursor = self.cursor()
        if sqlite_version < (3, 7, 6):
            # SQLite versions at least between 3.6.21 and 3.7.5 have a
            # buggy behavior with DROP TABLE IF EXISTS (#12298)
            try:
                cursor.execute("DROP TABLE " + self.quote(table))
            except sqlite.OperationalError: # "no such table"
                pass
        else:
            cursor.execute("DROP TABLE IF EXISTS " + self.quote(table))

    def get_column_names(self, table):
        cursor = self.cursor()
        cursor.execute("PRAGMA table_info(%s)" % self.quote(table))
        return [row[1] for row in cursor]

    def get_last_id(self, cursor, table, column='id'):
        return cursor.lastrowid

    def get_table_names(self):
        rows = self.execute("""
            SELECT name FROM sqlite_master WHERE type='table'""")
        return [row[0] for row in rows]

    def like(self):
        """Return a case-insensitive LIKE clause."""
        if sqlite_version >= (3, 1, 0):
            return "LIKE %s ESCAPE '/'"
        else:
            return 'LIKE %s'

    def like_escape(self, text):
        if sqlite_version >= (3, 1, 0):
            return _like_escape_re.sub(r'/\1', text)
        else:
            return text

    def prefix_match(self):
        """Return a case sensitive prefix-matching operator."""
        return 'GLOB %s'

    def prefix_match_value(self, prefix):
        """Return a value for case sensitive prefix-matching operator."""
        return _glob_escape_re.sub(lambda m: '[%s]' % m.group(0), prefix) + '*'

    def quote(self, identifier):
        """Return the quoted identifier."""
        return _quote(identifier)

    def update_sequence(self, cursor, table, column='id'):
        # SQLite handles sequence updates automagically
        # http://www.sqlite.org/autoinc.html
        pass


def _quote(identifier):
    return "`%s`" % identifier.replace('`', '``')


def _set_journal_mode(cursor, value):
    if not value:
        return
    value = value.upper()
    if value == 'OFF':
        raise TracError(_("PRAGMA journal_mode `%(value)s` cannot be used in "
                          "SQLite", value=value))
    cursor.execute('PRAGMA journal_mode = %s' % _quote(value))
    row = cursor.fetchone()
    if not row:
        raise TracError(_("PRAGMA journal_mode isn't supported by SQLite "
                          "%(version)s", version=sqlite_version_string))
    if (row[0] or '').upper() != value:
        raise TracError(_("PRAGMA journal_mode `%(value)s` isn't supported by "
                          "SQLite %(version)s",
                          value=value, version=sqlite_version_string))


def _set_synchronous(cursor, value):
    if not value:
        return
    if value.isdigit():
        value = str(int(value))
    cursor.execute('PRAGMA synchronous = %s' % _quote(value))
