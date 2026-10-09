import argparse

from importlib import import_module

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from olympia.core import logger


log = logger.getLogger('z.cron')


class Command(BaseCommand):
    help = 'Run one of the predefined cron jobs'

    def add_arguments(self, parser):
        # We handle the case where 0 arguments are given specifically below,
        # so use nargs='?' for the first argument.
        parser.add_argument('name', nargs='?')
        parser.add_argument('cron_args', nargs=argparse.REMAINDER, default=[])

    def handle(self, *args, **options):
        if not options['name']:
            log.error("Cron called without args")
            raise CommandError('These jobs are available:\n%s' % '\n'.join(
                sorted(settings.CRON_JOBS.keys())))

        name, cron_args = options['name'], options['cron_args']

        path = settings.CRON_JOBS.get(name)
        if not path:
            log.error('Cron called with an unknown cron job: %s %s' %
                      (name, cron_args))
            raise CommandError(u'Unrecognized job name: %s' % name)

        module = import_module(path)

        log.info("Beginning job: %s %s" % (name, cron_args))
        getattr(module, name)(*cron_args)
        log.info("Ending job: %s %s" % (name, cron_args))
