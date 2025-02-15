# -*- coding: utf-8 -*-
#
# Copyright (C) 2004-2020 Edgewall Software
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution. The terms
# are also available at http://trac.edgewall.com/license.html.
#
# This software consists of voluntary contributions made by many
# individuals. For the exact contribution history, see the revision
# history and logs, available at https://trac.edgewall.org/.

sql = """
CREATE TABLE attachment (
         type            text,
         id              text,
         filename        text,
         size            integer,
         time            integer,
         description     text,
         author          text,
         ipnr            text,
         UNIQUE(type,id,filename)
);
"""

def do_upgrade(env, ver, cursor):
    cursor.execute(sql)
    env.config.set('attachment', 'max_size', '262144')
    env.config.save()
