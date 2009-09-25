import logging
LOGGING_PLUGINS = ()
LOGGING_SETTINGS = { 'level': logging.DEBUG, }
LOGGING_LEVELS = {}
LOGGING_SYSLOG = False



VIGILO_CONNECTOR_DAEMONIZE = True
VIGILO_CONNECTOR_PIDFILE = '/var/lib/vigilo/connector/connector.pid'
VIGILO_CONNECTOR_XMPP_SERVER_HOST = 'tburguie3'
VIGILO_CONNECTOR_XMPP_PUBSUB_SERVICE = 'pubsub.tburguie3'
# Respect the ejabberd namespacing, for now. It will be too restrictive soon.
VIGILO_CONNECTOR_JID = 'connectorx@tburguie3'
VIGILO_CONNECTOR_PASS = 'connectorx'

VIGILO_CONNECTOR_TOPIC = '/home/tburguie3/connectorx/BUS'
VIGILO_SOCKETW = '/var/lib/vigilo/connector/recv.sock'
VIGILO_SOCKETR = '/var/lib/vigilo/connector/send.sock'
VIGILO_MESSAGE_BACKUP_FILE = '/var/lib/vigilo/connector/backup'
VIGILO_MESSAGE_BACKUP_TABLE_TOBUS = 'connector_tobus'
VIGILO_MESSAGE_BACKUP_TABLE_FROMBUS = 'connector_frombus'
