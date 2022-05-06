import argparse
import logging
import os
import datetime
import textwrap
from ruamel.yaml import YAML  # Used instead of comments to preserve comments
from ruamel.yaml.compat import StringIO

# 3rd party imports go her
from github import Github
from github.GithubException import UnknownObjectException


class YAMLWithStringDump(YAML):
    """
    Utility class to be able to dump the yaml to a string

    See
    https://yaml.readthedocs.io/en/latest/example.html#output-of-dump-as-a-string
    """
    def dump(self, data, stream=None, **kw):
        inefficient = False
        if stream is None:
            inefficient = True
            stream = StringIO()
        YAML.dump(self, data, stream, **kw)
        if inefficient:
            return stream.getvalue()


def parse_args():
    parser = argparse.ArgumentParser(
        description='Manages updating west manifest files')

    parser.add_argument('-v', '--verbose',
                        help='enable verbose logging',
                        action='store_true')
    parser.add_argument('--module-path',
                        help='Url to the repo that is updated',
                        required=True)
    parser.add_argument('--manifest-repo-path',
                        help='Url to the repo that contains the manifest file',
                        required=True)
    parser.add_argument('--manifest-file',
                        help='Path to the manifest file in the manifest repo',
                        required=True)
    parser.add_argument('--branch',
                        help='create pull request to the specified branch')
    parser.add_argument('-n', '--dry-run',
                        help='do not create new pull request',
                        action='store_true',
                        default=False)
    parser.add_argument('--module-pull-nr',
                        help='Number of the pull request to the module',
                        required=True)
    parser.add_argument('--draft-pr',
                        help='Create a draft PR to the manifest repo',
                        required=False,
                        default=False)

    return parser.parse_args()


def get_manifest_repo_branch_name(module_path):
    """
    Generate a new branch name for a pull request.

    The branch name is expected to be somewhat resistant to collisions.
    """
    formatted_module_path = module_path.replace('/', '_')

    return datetime.datetime.utcnow().strftime(f'update-{formatted_module_path}-rev_%Y%m%d%H%M')


def get_updated_manifest_str(west_yml_content, module_path, module_pull_nr):
    module_remote, module_repo = module_path.split('/')

    print('Updating remote:', module_remote, 'repo:', module_repo)

    yaml = YAMLWithStringDump()
    west_yml_updated = yaml.load(west_yml_content)
    
    try:
        default_remote_alias = west_yml_updated['manifest']['defaults']['remote']

        for remote in west_yml_updated['manifest']['remotes']:
            if remote['name'] == default_remote_alias:
                default_remote = remote['url-base'].split('/')[-1]
    except KeyError:
        default_remote = None

    print("Default remote is", default_remote)

    for project in west_yml_updated['manifest']['projects']:
        if 'repo-path' in project.keys():
            project_module_path = project['repo-path']
        else:
            project_module_path = project['name']
    
        if 'remote' in project.keys():
            project_remote = project['remote']
        else:
            project_remote = default_remote

        if project_module_path == module_repo and project_remote == module_remote:
            project['revision'] = f'pull/{module_pull_nr}/head'

    return yaml.dump(west_yml_updated)

def main():
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    print(f'Running {__file__} with args {args}')

    github_token = os.getenv('GITHUB_TOKEN')
    
    if github_token is None:
        raise Exception('Specify Github API token using GITHUB_TOKEN environment variable')

    print("What is this?", github_token[-4:])
    client = Github(github_token)

    manifest_repo = client.get_repo(args.manifest_repo_path)
    print('Manifest repo:', manifest_repo)
    
    fork_repo = manifest_repo.create_fork()
    print('Fork repo:', fork_repo)
    
    target_branch_name = args.branch or manifest_repo.default_branch
    print('Target branch is:', target_branch_name)

    manifest_repo_main_ref = manifest_repo.get_git_ref(f'heads/{target_branch_name}')
    manifest_repo_revision = manifest_repo_main_ref.object.sha

    west_yml_blob = fork_repo.get_contents(args.manifest_file, manifest_repo_revision)
    west_yml_content = west_yml_blob.decoded_content.decode()
    west_yml_content = get_updated_manifest_str(west_yml_content, args.module_path, args.module_pull_nr)

    user = client.get_user()
    commit_title = f'manifest: Auto update of {args.module_path} revision'
    commit_body = textwrap.dedent(f"""\
    This commit is automatically created as result of
    Pull request #{args.module_pull_nr} to {args.module_path}.
    
    Signed-off-by: {user.name} <{user.email}>
    """)

    commit_message = textwrap.dedent(f"""\
    {commit_title}

    {commit_body}
    """)

    # Update or create the target branch in fork, point it to the same commit
    # as in the upstream repo.
    try:
        manifest_repo_ref = fork_repo.get_git_ref(f'heads/{target_branch_name}')
        manifest_repo_ref.edit(manifest_repo_revision)
    except UnknownObjectException:
        manifest_repo_ref = fork_repo.create_git_ref(f'refs/heads/{target_branch_name}',
                                                     manifest_repo_revision)

    new_branch_name = get_manifest_repo_branch_name(args.module_path)
    print("Creating ref to fork, branch name: ", new_branch_name, "Ref:", manifest_repo_revision)
    new_ref = fork_repo.create_git_ref(f'refs/heads/{new_branch_name}', manifest_repo_revision)

    updated_file = fork_repo.update_file(path=args.manifest_file, 
                                         message=commit_message, 
                                         content=west_yml_content,
                                         sha=west_yml_blob.sha,
                                         branch=new_branch_name)
    new_ref.edit(updated_file['commit'].sha)

    if not args.dry_run:
        pull = fork_repo.create_pull(
            title=commit_title,
            body=commit_body,
            base=target_branch_name,
            head=new_branch_name,
            draft=True)
        print("Created pull request:", pull.html_url)
    else:
        print('Dry run, skipping pull request creation')


if __name__ == '__main__':
    main()
