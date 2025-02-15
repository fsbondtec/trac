# -*- coding: utf-8 -*-
#
# Copyright (C) 2005-2020 Edgewall Software
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution. The terms
# are also available at http://trac.edgewall.com/license.html.
#
# This software consists of voluntary contributions made by many
# individuals. For the exact contribution history, see the revision
# history and logs, available at https://trac.edgewall.org/.

sql = [
#-- Some anonymous session might have been left over
"""DELETE FROM session WHERE username='anonymous';""",
#-- Schema change: use an authenticated flag instead of separate sid/username
#-- columns
"""CREATE TEMPORARY TABLE session_old AS SELECT * FROM session;""",
"""DROP TABLE session;""",
"""CREATE TABLE session (
        sid             text,
        authenticated   int,
        var_name        text,
        var_value       text,
        UNIQUE(sid, var_name)
);""",
"""INSERT INTO session(sid,authenticated,var_name,var_value)
    SELECT DISTINCT sid,0,var_name,var_value FROM session_old
    WHERE sid IS NULL;""",
"""INSERT INTO session(sid,authenticated,var_name,var_value)
    SELECT DISTINCT username,1,var_name,var_value FROM session_old
    WHERE sid IS NULL;""",
"""DROP TABLE session_old;"""
]

def do_upgrade(env, ver, cursor):
    for s in sql:
        cursor.execute(s)
