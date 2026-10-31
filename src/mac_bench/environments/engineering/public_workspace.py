"""Commands shared by software task loaders and exported Docker workspaces."""
import shlex


def initialization_commands(environment, metadata):
    commands = ['set -eu', 'mkdir -p /work/project']
    if metadata.get('fixture_archive'):
        destination = shlex.quote(metadata.get('fixture_directory', '/work'))
        commands += [f'mkdir -p {destination}',
                     f'tar xf /work/fixtures.tar -C {destination}']
    if environment.get('public_prepare'):
        commands.append(environment['public_prepare'])
    commands += ['cd /work/project', environment['build']]
    return '\n'.join(commands) + '\n'
