import logging

from reconcile import queries

from reconcile.utils.gitlab_api import GitLabApi

QONTRACT_INTEGRATION = 'jenkins-webhooks-cleaner'


def run(dry_run):
    instance = queries.get_gitlab_instance()
    settings = queries.get_app_interface_settings()
    gl = GitLabApi(instance, settings=settings)
    previous_urls = queries.get_jenkins_instances_previous_urls()
    repos = queries.get_repos(server=gl.server)

    for repo in repos:
        hooks = gl.get_project_hooks(repo)
        for hook in hooks:
            hook_url = hook.url
            for previous_url in previous_urls:
                if hook_url.startswith(previous_url):
                    logging.info(['delete_hook', repo, hook_url])
                    if not dry_run:
                        hook.delete()
