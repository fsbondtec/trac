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

import re, os

from genshi import Markup

from trac.core import *
from trac.config import Option
from trac.db.api import IDatabaseConnector, _parse_db_str
from trac.db.util import ConnectionWrapper, IterableCursor
from trac.util import get_pkginfo, lazy
from trac.util.compat import close_fds
from trac.util.text import empty, exception_to_unicode, to_unicode
from trac.util.translation import _

has_psycopg = False
try:
    import psycopg2 as psycopg
    import psycopg2.extensions
    from psycopg2 import DataError, ProgrammingError
    from psycopg2.extensions import register_type, UNICODE, \
                                    register_adapter, AsIs, QuotedString
except ImportError:
    pass
else:
    has_psycopg = True
    register_type(UNICODE)
    register_adapter(Markup, lambda markup: QuotedString(unicode(markup)))
    register_adapter(type(empty), lambda empty: AsIs("''"))

_like_escape_re = re.compile(r'([/_%])')

# Mapping from "abstract" SQL types to DB-specific types
_type_map = {
    'int64': 'bigint',
}


def assemble_pg_dsn(path, user=None, password=None, host=None, port=None):
    """Quote the parameters and assemble the DSN."""
    def quote(value):
        if not isinstance(value, basestring):
            value = unicode(value)
        return "'%s'" % value.replace('\\', r'\\').replace("'", r"\'")

    dsn = {'dbname': path, 'user': user, 'password': password, 'host': host,
           'port': port}
    return ' '.join("%s=%s" % (name, quote(value))
                    for name, value in dsn.iteritems() if value)


def _quote(identifier):
    return '"%s"' % identifier.replace('"', '""')


class PostgreSQLConnector(Component):
    """Database connector for PostgreSQL.

    Database URLs should be of the form:
    {{{
    postgres://user[:password]@host[:port]/database[?schema=my_schema]
    }}}
    """
    implements(IDatabaseConnector)

    pg_dump_path = Option('trac', 'pg_dump_path', 'pg_dump',
        """Location of pg_dump for Postgres database backups""")

    def __init__(self):
        self._version = None
        self.error = None

    def get_supported_schemes(self):
        if not has_psycopg:
            self.error = _("Cannot load Python bindings for PostgreSQL")
        yield ('postgres', -1 if self.error else 1)

    def get_connection(self, path, log=None, user=None, password=None,
                       host=None, port=None, params={}):
        cnx = PostgreSQLConnection(path, log, user, password, host, port,
                                   params)
        if not self._version:
            self._version = get_pkginfo(psycopg).get('version',
                                                     psycopg.__version__)
            self.env.systeminfo.append(('psycopg2', self._version))
            self.required = True
        return cnx

    def get_exceptions(self):
        return psycopg

    def init_db(self, path, schema=None, log=None, user=None, password=None,
                host=None, port=None, params={}):
        cnx = self.get_connection(path, log, user, password, host, port,
                                  params)
        cursor = cnx.cursor()
        if cnx.schema:
            cursor.execute('CREATE SCHEMA ' + _quote(cnx.schema))
            cursor.execute('SET search_path TO %s', (cnx.schema,))
        if schema is None:
            from trac.db_default import schema
        for table in schema:
            for stmt in self.to_sql(table):
                cursor.execute(stmt)
        cnx.commit()

    def to_sql(self, table):
        sql = ['CREATE TABLE %s (' % _quote(table.name)]
        coldefs = []
        for column in table.columns:
            ctype = column.type
            ctype = _type_map.get(ctype, ctype)
            if column.auto_increment:
                ctype = 'SERIAL'
            if len(table.key) == 1 and column.name in table.key:
                ctype += ' PRIMARY KEY'
            coldefs.append('    %s %s' % (_quote(column.name), ctype))
        if len(table.key) > 1:
            coldefs.append('    CONSTRAINT %s PRIMARY KEY (%s)' %
                           (_quote(table.name + '_pk'),
                            ','.join(_quote(col) for col in table.key)))
        sql.append(',\n'.join(coldefs) + '\n)')
        yield '\n'.join(sql)
        for index in table.indices:
            unique = 'UNIQUE' if index.unique else ''
            yield 'CREATE %s INDEX %s ON %s (%s)' % \
                  (unique,
                   _quote('%s_%s_idx' % (table.name, '_'.join(index.columns))),
                   _quote(table.name),
                   ','.join(_quote(col) for col in index.columns))

    def alter_column_types(self, table, columns):
        """Yield SQL statements altering the type of one or more columns of
        a table.

        Type changes are specified as a `columns` dict mapping column names
        to `(from, to)` SQL type tuples.
        """
        alterations = []
        for name, (from_, to) in sorted(columns.iteritems()):
            to = _type_map.get(to, to)
            if to != _type_map.get(from_, from_):
                alterations.append((name, to))
        if alterations:
            yield 'ALTER TABLE %s %s' % \
                  (_quote(table),
                   ', '.join('ALTER COLUMN %s TYPE %s' % (_quote(name), type_)
                             for name, type_ in alterations))

    def backup(self, dest_file):
        from subprocess import Popen, PIPE
        db_url = self.env.config.get('trac', 'database')
        scheme, db_prop = _parse_db_str(db_url)
        db_params = db_prop.setdefault('params', {})
        db_name = os.path.basename(db_prop['path'])

        args = [self.pg_dump_path, '-C', '--inserts', '-x', '-Z', '8']
        if 'user' in db_prop:
            args.extend(['-U', db_prop['user']])
        if 'host' in db_params:
            host = db_params['host']
        else:
            host = db_prop.get('host')
        if host:
            args.extend(['-h', host])
            if '/' not in host:
                args.extend(['-p', str(db_prop.get('port', '5432'))])

        if 'schema' in db_params:
            try:
                p = Popen([self.pg_dump_path, '--version'], stdout=PIPE,
                          close_fds=close_fds)
            except OSError, e:
                raise TracError(_("Unable to run %(path)s: %(msg)s",
                                  path=self.pg_dump_path,
                                  msg=exception_to_unicode(e)))
            # Need quote for -n (--schema) option in PostgreSQL 8.2+
            version = p.communicate()[0]
            if re.search(r' 8\.[01]\.', version):
                args.extend(['-n', db_params['schema']])
            else:
                args.extend(['-n', '"%s"' % db_params['schema']])

        dest_file += ".gz"
        args.extend(['-f', dest_file, db_name])

        environ = os.environ.copy()
        if 'password' in db_prop:
            environ['PGPASSWORD'] = str(db_prop['password'])
        try:
            p = Popen(args, env=environ, stderr=PIPE, close_fds=close_fds)
        except OSError, e:
            raise TracError(_("Unable to run %(path)s: %(msg)s",
                              path=self.pg_dump_path,
                              msg=exception_to_unicode(e)))
        errmsg = p.communicate()[1]
        if p.returncode != 0:
            raise TracError(_("pg_dump failed: %(msg)s",
                              msg=to_unicode(errmsg.strip())))
        if not os.path.exists(dest_file):
            raise TracError(_("No destination file created"))
        return dest_file


class PostgreSQLConnection(ConnectionWrapper):
    """Connection wrapper for PostgreSQL."""

    poolable = True

    def __init__(self, path, log=None, user=None, password=None, host=None,
                 port=None, params={}):
        if path.startswith('/'):
            path = path[1:]
        if 'host' in params:
            host = params['host']

        cnx = psycopg.connect(assemble_pg_dsn(path, user, password, host,
                                              port))

        cnx.set_client_encoding('UNICODE')
        try:
            self.schema = None
            if 'schema' in params:
                self.schema = params['schema']
                cnx.cursor().execute('SET search_path TO %s', (self.schema,))
                cnx.commit()
        except (DataError, ProgrammingError):
            cnx.rollback()
        ConnectionWrapper.__init__(self, cnx, log)

    def cursor(self):
        return IterableCursor(self.cnx.cursor(), self.log)

    def cast(self, column, type):
        # Temporary hack needed for the union of selects in the search module
        return 'CAST(%s AS %s)' % (column, _type_map.get(type, type))

    def concat(self, *args):
        return '||'.join(args)

    def drop_table(self, table):
        if (self._version or '').startswith(('8.0.', '8.1.')):
            cursor = self.cursor()
            cursor.execute("""SELECT table_name FROM information_schema.tables
                              WHERE table_schema=current_schema()
                              AND table_name=%s""", (table,))
            for row in cursor:
                if row[0] == table:
                    self.execute("DROP TABLE " + self.quote(table))
                    break
        else:
            self.execute("DROP TABLE IF EXISTS " + self.quote(table))

    def get_column_names(self, table):
        rows = self.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name=%s
            """, (table,))
        return [row[0] for row in rows]

    def get_last_id(self, cursor, table, column='id'):
        cursor.execute("SELECT CURRVAL(%s)",
                       (self.quote(self._sequence_name(table, column)),))
        return cursor.fetchone()[0]

    def get_table_names(self):
        rows = self.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema=current_schema()""")
        return [row[0] for row in rows]

    def like(self):
        """Return a case-insensitive LIKE clause."""
        return "ILIKE %s ESCAPE '/'"

    def like_escape(self, text):
        return _like_escape_re.sub(r'/\1', text)

    def ping(self):
        self.cnx.isolation_level

    def prefix_match(self):
        """Return a case sensitive prefix-matching operator."""
        return "LIKE %s ESCAPE '/'"

    def prefix_match_value(self, prefix):
        """Return a value for case sensitive prefix-matching operator."""
        return self.like_escape(prefix) + '%'

    def quote(self, identifier):
        """Return the quoted identifier."""
        return _quote(identifier)

    def update_sequence(self, cursor, table, column='id'):
        cursor.execute("SELECT SETVAL(%%s, (SELECT MAX(%s) FROM %s))"
                       % (self.quote(column), self.quote(table)),
                       (self.quote(self._sequence_name(table, column)),))

    def _sequence_name(self, table, column):
        return '%s_%s_seq' % (table, column)

    @lazy
    def _version(self):
        cursor = self.cursor()
        cursor.execute('SELECT version()')
        for version, in cursor:
            # retrieve "8.1.23" from "PostgreSQL 8.1.23 on ...."
            if version.startswith('PostgreSQL '):
                return version.split(' ', 2)[1]
