# vim: set fileencoding=utf-8 sw=4 ts=4 et :
# Copyright (C) 2006-2011 CS-SI
# License: GNU GPL v2 <http://www.gnu.org/licenses/gpl-2.0.html>

""" generic vigilo connector """


def getSettings(options):
    from vigilo.common.conf import settings
    if options["config"] is not None:
        settings.load_file(options["config"])
    else:
        settings.load_module(__name__)
    return settings

