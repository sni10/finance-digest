from django.core.management import get_commands, load_command_class
from django.test import SimpleTestCase


class ManagementCommandsTests(SimpleTestCase):
    def test_every_project_command_loads(self):
        """A command module that does not even import fails here, not in the owner's terminal."""
        ours = {name: app for name, app in get_commands().items() if not app.startswith('django')}
        self.assertIn('pipeline', ours)
        for name, app in ours.items():
            with self.subTest(command=name):
                load_command_class(app, name).create_parser('manage.py', name)
