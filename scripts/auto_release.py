#!/usr/bin/env python3
"""Open a release pull request automatically when changes land on the base branch.

Generalised, non-interactive replacement for the manual "check out latest
``main``, run ``invoke release``, open a PR, wait for CI" loop used by Eventbrite
libraries that release via ``invoke_release`` + the CircleCI
``invoke-release-pull-request`` pipeline.

Given a repo configured like::

    # tasks.py
    configure_release_parameters(
        module_name='permissions',
        display_name='Permissions Library',
        use_pull_request=True,
        use_tag=False,
    )

this script:

  * finds commits since the last released tag,
  * derives the next semantic version from the ``[MAJOR]`` / ``[MINOR]`` /
    ``[PATCH]`` tag in those commit subjects (falling back to ``PATCH``),
  * bumps the version file and prepends a changelog entry,
  * pushes ``invoke-release-<base>-<version>`` and opens a PR.

The PR is then picked up by the existing CircleCI ``invoke-release-pull-request``
workflow (``docker-dev/ci_cd`` ``release.py``), which approves, squash-merges,
tags the merged commit and publishes the wheel.

It reconciles the repo to exactly one release branch/PR, always rebuilt from the
current tip and covering every unreleased commit. It is idempotent and safe to
re-run; it skips when the base has moved, when there is nothing to release, when
the version is already tagged, or when HEAD is already a release commit.

Run it from a checkout of the target repo. Requires Py3 and a GitHub token for
the account that should *author* the release PR.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request


VERSION_INFO_RE = re.compile(r'^__version_info__\s*=\s*\((\d+),\s*(\d+),\s*(\d+)')
VERSION_TXT_RE = re.compile(r'^(\d+)\.(\d+)\.(\d+)')
CHANGELOG_VERSION_HEADER_RE = re.compile(r'^\d+\.\d+\.\d+ \(')
CHANGELOG_FILE_HEADER_RE = re.compile(r'^=+$')
SEMANTIC_TAG_RE = re.compile(r'\[(MAJOR|MINOR|PATCH)\]')

BUMP_PRIORITY = {'PATCH': 1, 'MINOR': 2, 'MAJOR': 3}
CHANGELOG_NAMES = ('CHANGELOG.txt', 'CHANGELOG.md', 'CHANGELOG.rst')


class AutoReleaseSkip(Exception):
    """Raised to abort cleanly (exit 0) without failing the build."""


def git(*args, **kwargs):
    # NB: keyword args go through **kwargs (not a keyword-only argument) so this
    # file stays parseable by Python 2 flake8 lint envs that lint the whole repo.
    extra_env = kwargs.pop('extra_env', None)
    if kwargs:
        raise TypeError('unexpected keyword arguments: {}'.format(sorted(kwargs)))
    env = dict(os.environ, **extra_env) if extra_env else None
    result = subprocess.run(
        ['git'] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError('git {} failed: {}'.format(' '.join(args), result.stderr.strip()))
    return result.stdout.strip()


def release_message(display_name, version=''):
    return 'Released {} version {}'.format(display_name, version).strip()


def version_file_path(module, python_directory):
    """Mirror invoke_release's version-file resolution."""
    prefix = os.path.join(*(part for part in (python_directory, module, 'version') if part))
    if _case_sensitive_exists(prefix + '.txt'):
        return prefix + '.txt'
    return prefix + '.py'


def _case_sensitive_exists(path):
    if not os.path.isfile(path):
        return False
    directory, filename = os.path.split(path)
    return filename in os.listdir(directory or '.')


def changelog_path(explicit):
    if explicit:
        return explicit
    for name in CHANGELOG_NAMES:
        if _case_sensitive_exists(name):
            return name
    raise AutoReleaseSkip('No CHANGELOG.txt/.md/.rst found.')


def current_version(path):
    with open(path) as version_file:
        contents = version_file.read()
    match = VERSION_INFO_RE.search(contents) if path.endswith('.py') else VERSION_TXT_RE.search(contents)
    if not match:
        raise AutoReleaseSkip('Could not read a version from {}.'.format(path))
    return tuple(int(part) for part in match.groups()[:3])


def last_released_tag():
    try:
        return git('describe', '--tags', '--abbrev=0')
    except RuntimeError:
        return None


def unreleased_subjects(tag, display_name):
    output = git('log', '{}..HEAD'.format(tag), '--format=%s') if tag else git('log', '--format=%s')
    prefix = release_message(display_name)
    subjects = []
    for line in output.splitlines():
        subject = line.strip()
        if not subject or subject.startswith('Merge pull request #') or subject.startswith(prefix):
            continue
        subjects.append(subject)
    return subjects


def determine_bump(subjects):
    bump = 'PATCH'
    for subject in subjects:
        for tag in SEMANTIC_TAG_RE.findall(subject):
            if BUMP_PRIORITY[tag] > BUMP_PRIORITY[bump]:
                bump = tag
    return bump


def next_version(version, bump):
    major, minor, patch = version
    if bump == 'MAJOR':
        return (major + 1, 0, 0)
    if bump == 'MINOR':
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def format_version(version):
    return '.'.join(str(part) for part in version)


def remote_ref_sha(ref):
    output = git('ls-remote', 'origin', ref)
    return output.split()[0] if output.strip() else None


def remote_release_branches(prefix):
    output = git('ls-remote', '--heads', 'origin', 'refs/heads/{}*'.format(prefix))
    branches = []
    for line in output.splitlines():
        if line.strip():
            sha, ref = line.split('\t')
            branches.append((ref.split('refs/heads/')[1], sha))
    return branches


def write_version_file(path, version):
    version_string = format_version(version)
    if path.endswith('.txt'):
        with open(path, 'w') as version_file:
            version_file.write(version_string)
        return
    with open(path) as version_file:
        lines = version_file.read().split('\n')
    for index, line in enumerate(lines):
        if VERSION_INFO_RE.match(line):
            lines[index] = '__version_info__ = ({}, {}, {})'.format(*version)
            break
    else:
        raise AutoReleaseSkip('Could not find __version_info__ in {}.'.format(path))
    with open(path, 'w') as version_file:
        version_file.write('\n'.join(lines))


def prepend_changelog(path, version, subjects, release_date):
    with open(path) as changelog_file:
        lines = changelog_file.read().split('\n')
    insert_at = len(lines)
    for index, line in enumerate(lines):
        if CHANGELOG_VERSION_HEADER_RE.match(line):
            insert_at = index
            break
    else:
        for index, line in enumerate(lines):
            if CHANGELOG_FILE_HEADER_RE.match(line):
                insert_at = index + 2
                break
    header = '{} ({})'.format(version, release_date)
    block = [header, '-' * len(header)] + ['- {}'.format(subject) for subject in subjects] + ['']
    lines[insert_at:insert_at] = block
    with open(path, 'w') as changelog_file:
        changelog_file.write('\n'.join(lines))


def build_release_commit(branch, paths, new_version, version, subjects, display_name, release_timestamp):
    """(Re)create ``branch`` on top of HEAD with the bump applied.

    The commit date is pinned and signing disabled so re-running with the same
    base produces the same commit (true no-op instead of a force-push that
    re-triggers CI).
    """
    git('checkout', '-B', branch, 'HEAD')
    write_version_file(paths['version'], new_version)
    prepend_changelog(paths['changelog'], version, subjects, release_timestamp[:10])
    git('add', paths['version'], paths['changelog'])
    message = [release_message(display_name, version), '', 'Changelog Details:']
    message.extend('- {}'.format(subject) for subject in subjects)
    git(
        '-c', 'commit.gpgsign=false',
        'commit', '-m', '\n'.join(message),
        extra_env={'GIT_AUTHOR_DATE': release_timestamp, 'GIT_COMMITTER_DATE': release_timestamp},
    )
    return git('rev-parse', 'HEAD')


def github(args, method, path, payload=None):
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    request = urllib.request.Request(
        'https://api.github.com/repos/{}/{}'.format(args.repository, path),
        data,
        headers={
            'Content-Type': 'application/json',
            'Authorization': 'token {}'.format(args.github_token),
            'Accept': 'application/vnd.github.v3+json',
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode('utf-8') or 'null')
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode('utf-8') or 'null')


def find_open_pull_request(args, branch):
    owner = args.repository.split('/')[0]
    _, pulls = github(args, 'GET', 'pulls?state=open&head={}:{}'.format(owner, branch))
    return pulls[0] if pulls else None


def ensure_pull_request(args, branch, version):
    existing = find_open_pull_request(args, branch)
    if existing:
        return existing['html_url']
    status, payload = github(args, 'POST', 'pulls', {
        'title': release_message(args.display_name, version),
        'head': branch,
        'base': args.base,
    })
    if status in (200, 201):
        return payload['html_url']
    if status == 422:  # a concurrent run may have created it
        existing = find_open_pull_request(args, branch)
        if existing:
            return existing['html_url']
    raise RuntimeError('Failed to open release PR: {} {}'.format(status, payload))


def reconcile_release_branches(args, prefix, keep):
    for branch, _ in remote_release_branches(prefix):
        if branch == keep:
            continue
        version = branch[len(prefix):]
        if remote_ref_sha('refs/tags/{}'.format(version)):
            continue  # already released; leave it
        print('Removing superseded release branch {}.'.format(branch))
        pull_request = find_open_pull_request(args, branch)
        if pull_request:
            github(args, 'PATCH', 'pulls/{}'.format(pull_request['number']), {'state': 'closed'})
        git('push', 'origin', '--delete', branch)


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module', required=True, help='module_name from tasks.py')
    parser.add_argument('--display-name', required=True, help='display_name from tasks.py')
    parser.add_argument('--python-directory', default='', help="python_directory from tasks.py (often empty)")
    parser.add_argument('--version-file', default='', help='override the version file path')
    parser.add_argument('--changelog-file', default='', help='override the changelog path')
    parser.add_argument('--base', default='main', help='base branch for the release PR')
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY', ''),
                        help='GitHub repo owner/name (defaults to $GITHUB_REPOSITORY)')
    parser.add_argument('--github-token', default='', help='token for the account that authors the PR')
    parser.add_argument('--dry-run', action='store_true', help='print the plan without changing anything')
    return parser.parse_args(argv)


def run(args):
    if not args.repository and not args.dry_run:
        raise RuntimeError('--repository (or $GITHUB_REPOSITORY) is required.')

    os.chdir(git('rev-parse', '--show-toplevel'))
    git('fetch', '--quiet', '--tags', 'origin')
    git('fetch', '--quiet', 'origin', args.base)

    if git('rev-parse', 'HEAD') != git('rev-parse', 'origin/{}'.format(args.base)):
        raise AutoReleaseSkip('{} moved since this build; a newer build will handle it.'.format(args.base))
    if git('log', '-1', '--format=%s', 'HEAD').startswith(release_message(args.display_name)):
        raise AutoReleaseSkip('HEAD is already a release commit; nothing to do.')
    if not args.github_token and not args.dry_run:
        raise RuntimeError('A GitHub token is required to open the release PR.')

    paths = {
        'version': args.version_file or version_file_path(args.module, args.python_directory),
        'changelog': changelog_path(args.changelog_file or None),
    }
    tag = last_released_tag()
    subjects = unreleased_subjects(tag, args.display_name)
    if not subjects:
        raise AutoReleaseSkip('Nothing to release: no commits since {} .'.format(tag))

    bump = determine_bump(subjects)
    new_version = next_version(current_version(paths['version']), bump)
    version = format_version(new_version)
    prefix = 'invoke-release-{}-'.format(args.base)
    branch = prefix + version

    print('{} unreleased commit(s) since {}; bumping {} -> {} ({}).'.format(
        len(subjects), tag, format_version(current_version(paths['version'])), version, bump))

    if remote_ref_sha('refs/tags/{}'.format(version)):
        raise AutoReleaseSkip('Tag {} already exists; nothing to do.'.format(version))
    if args.dry_run:
        print('Dry run: would update {} and open a PR to {}.'.format(branch, args.base))
        return

    desired_sha = build_release_commit(
        branch, paths, new_version, version, subjects, args.display_name,
        git('show', '-s', '--format=%cI', 'HEAD'))
    reconcile_release_branches(args, prefix, keep=branch)
    if remote_ref_sha('refs/heads/{}'.format(branch)) != desired_sha:
        git('push', '--force', 'origin', '{}:{}'.format(branch, branch))
    print('Release PR: {}'.format(ensure_pull_request(args, branch, version)))


def main(argv=None):
    args = parse_arguments(sys.argv[1:] if argv is None else argv)
    try:
        run(args)
    except AutoReleaseSkip as skip:
        print('Skipping automatic release: {}'.format(skip))
    return 0


if __name__ == '__main__':
    sys.exit(main())
