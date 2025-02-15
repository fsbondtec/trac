# -*- coding: utf-8 -*-
#
# Copyright (C) 2006-2020 Edgewall Software
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution. The terms
# are also available at http://trac.edgewall.com/license.html.
#
# This software consists of voluntary contributions made by many
# individuals. For the exact contribution history, see the revision
# history and logs, available at https://trac.edgewall.org/.

from trac.db import Table, Column, Index, DatabaseManager

def do_upgrade(env, ver, cursor):
    """Rename the columns `kind` and `change` in the `node_change` table for
    compatibity with MySQL.
    """
    cursor.execute("CREATE TEMPORARY TABLE nc_old AS SELECT * FROM node_change")
    cursor.execute("DROP TABLE node_change")

    table = Table('node_change', key=('rev', 'path', 'change_type'))[
        Column('rev'),
        Column('path'),
        Column('node_type', size=1),
        Column('change_type', size=1),
        Column('base_path'),
        Column('base_rev'),
        Index(['rev'])
    ]
    db_connector, _ = DatabaseManager(env).get_connector()
    for stmt in db_connector.to_sql(table):
        cursor.execute(stmt)

    cursor.execute("INSERT INTO node_change (rev,path,node_type,change_type,"
                   "base_path,base_rev) SELECT rev,path,kind,change,"
                   "base_path,base_rev FROM nc_old")
    cursor.execute("DROP TABLE nc_old")
