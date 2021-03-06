""" Provides Access to the GitHub API """
from __future__ import absolute_import
from __future__ import print_function, unicode_literals

from datetime import datetime, timedelta, time
import logging
import os
import socket
import backoff

from github import Github
from github.Commit import Commit
from github.GitCommit import GitCommit
from github.GithubException import UnknownObjectException, GithubException
from github.InputGitAuthor import InputGitAuthor
from pytz import timezone
import six
from validators import url as url_validator

from .exception import InvalidUrlException
from .utils import envvar_get_int
from .git_repo import LocalGitAPI

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

PR_ON_STAGE_BASE_MESSAGE = '**EdX Release Notice**: This PR has been deployed to the staging environment '
PR_ON_STAGE_DATE_MESSAGE = 'in preparation for a release to production on {date:%A, %B %d, %Y}.'
PR_ON_PROD_MESSAGE = '**EdX Release Notice**: This PR has been deployed to the production environment.'
PR_RELEASE_CANCELED_MESSAGE = '**EdX Release Notice**: This PR has been rolled back from the production environment.'

DEFAULT_TAG_USERNAME = 'no_user'
DEFAULT_TAG_EMAIL_ADDRESS = 'no.public.email@edx.org'

# Day of week constant
_MONDAY = 0
_FRIDAY = 4
_NORMAL_RELEASE_WEEKDAYS = tuple(range(_MONDAY, _FRIDAY + 1))
RELEASE_TZ = timezone('US/Eastern')
RELEASE_CUTOFF = time(10, tzinfo=RELEASE_TZ)

# Defaults for the polling of a PR's tests.
MAX_PR_TEST_TRIES_DEFAULT = 5
PR_TEST_INITIAL_WAIT_INTERVAL_DEFAULT = 10
PR_TEST_POLL_INTERVAL_DEFAULT = 10


class NoValidCommitsError(Exception):
    """
    Error indicating that there are no commits with valid statuses
    """
    pass


class InvalidPullRequestError(Exception):
    """
    Error indicating that a PR could not be found
    """
    pass


class GitTagMismatchError(Exception):
    """
    Error indicating that a tag is pointing at an incorrect SHA.
    """
    pass


def extract_message_summary(message, max_length=50):
    """
    Take a commit message and return the first part of it.
    """
    title = message.split('\n')[0] or ''
    if len(title) < max_length:
        return title
    else:
        return title[0:max_length] + '...'


def default_expected_release_date(at_time=None, release_days=_NORMAL_RELEASE_WEEKDAYS):
    """
    Returns the default expected release date given the current date.
    Currently the nearest weekday in the future (can't be today).
    """
    if at_time is None:
        at_time = datetime.now(RELEASE_TZ)

    if at_time.timetz() < RELEASE_CUTOFF:
        proposal = at_time.date()
    else:
        proposal = at_time.date() + timedelta(days=1)

    while proposal.weekday() not in release_days:
        proposal = proposal + timedelta(days=1)
    return datetime.combine(proposal, RELEASE_CUTOFF)


def rc_branch_name_for_date(date):
    """
    Returns the standard release candidate branch name
    """
    return 'rc/{date}'.format(date=date.isoformat())


def _backoff_handler(details):
    """
    Simple logging handler for when polling backoff occurs.
    """
    LOGGER.info('Trying again in {wait:0.1f} seconds after {tries} tries calling {target}'.format(**details))


def _constant_with_initial_wait(initial_wait=0, interval=1):
    """
    Generator with initial wait (after the first request) built-in.
    The first request is made immediately.
    The second request is made after "initial_wait" seconds.
    All remaining requests made after "interval" seconds.

    Useful for polling processes expected to not have results for a substantial interval from process start.

    Arguments:
        initial_wait: Number of seconds to wait between the first and second requests.
        interval: Constant value in seconds to yield after second request.
    """
    yield initial_wait
    while True:
        yield interval


class GitHubAPI(object):
    """
    Manages requests to the GitHub api for a given org/repo
    """

    def __init__(self, org, repo, token):
        """
        Creates a new API access object.

        Arguments:
            org (string): Github org to access
            repo (string): Github repo to access
            token (string): Github API access token

        """
        self.github_connection = Github(token)
        self.github_repo = self.github_connection.get_repo('{org}/{repo}'.format(org=org, repo=repo))
        self.github_org = self.github_connection.get_organization(org)
        self.org = org
        self.repo = repo

    def clone(self, branch=None, reference_repo=None):
        """
        Clone this Github repo as a LocalGitAPI instance.
        """
        clone_url = self.github_repo.ssh_url
        return LocalGitAPI.clone(clone_url, branch, reference_repo)

    def user(self):
        """
        Calls GitHub's '/user' endpoint.
            See
            https://developer.github.com/v3/users/#get-the-authenticated-user

        Returns:
            github.NamedUser.NamedUser: Information about the current user.

        Raises:
            RequestFailed: If the response fails validation.
        """
        return self.github_connection.get_user()

    def get_head_commit_from_pull_request(self, pr_number):
        """
        Given a PR number, return the HEAD commit hash.

        Arguments:
            pr_number (int): Number of PR to check.

        Returns:
            Commit SHA of the PR HEAD.

        Raises:
            github.GithubException.GithubException: If the response fails.
            github.GithubException.UnknownObjectException: If the branch does not exist
        """
        return self.get_pull_request(pr_number).head.sha

    def get_diff_url(self, organization, repository, base_sha, head_sha):
        """
        Given the organization and repository, generate a github URL that will compare the provided SHAs.

        Arguments:
            organization (str): An organization name as it will appear in github
            repository (str): The organization's repository name
            base_sha (str): The base commit's SHA
            head_sha (str): Compare the base SHA with this commit

        Returns:
            A string constaining the URL

        Raises:
            InvalidUrlException: If the basic validator does not believe this to be a valid URL
        """
        calculated_url = 'https://github.com/{}/{}/compare/{}...{}'.format(
            organization, repository, base_sha, head_sha
        )

        if not url_validator(calculated_url):
            raise InvalidUrlException(calculated_url)

        return calculated_url

    def get_head_commit_from_branch_name(self, branch_name):
        """
        Given a branch name, return the HEAD commit hash.

        Arguments:
            branch_name (str): Name of branch from which to extract HEAD commit hash.

        Returns:
            Commit SHA of the branch HEAD.

        Raises:
            github.GithubException.GithubException: If the response fails.
            github.GithubException.UnknownObjectException: If the branch does not exist
        """
        return self.get_commits_by_branch(branch_name)[0].sha

    def get_commit_combined_statuses(self, commit):
        """
        Calls GitHub's '<commit>/statuses' endpoint for a given commit. See
        https://developer.github.com/v3/repos/statuses/#get-the-combined-status-for-a-specific-ref

        Returns:
            github.CommitCombinedStatus.CommitCombinedStatus

        Raises:
            RequestFailed: If the response fails validation.
        """
        if isinstance(commit, six.string_types):
            commit = self.github_repo.get_commit(commit)
        elif isinstance(commit, GitCommit):
            commit = self.github_repo.get_commit(commit.sha)
        elif not isinstance(commit, Commit):
            raise UnknownObjectException(500, 'commit is neither a valid sha nor github.Commit.Commit object.')

        return commit.get_combined_status()

    def check_pull_request_test_status(self, pr_number):
        """
        Given a PR number, query the current combined status of the PR's tests.

        Arguments:
            pr_number (int): Number of PR to check.

        Returns:
            True if all tests have passed successfully, else False.

        Raises:
            github.GithubException.GithubException: If the response fails.
            github.GithubException.UnknownObjectException: If the branch does not exist
        """
        return self.get_commit_combined_statuses(
            self.get_head_commit_from_pull_request(pr_number)
        ).state.lower() == 'success'

    @backoff.on_exception(
        backoff.expo,
        socket.timeout,
        max_tries=5
    )
    @backoff.on_predicate(
        _constant_with_initial_wait,
        lambda x: x not in ('success', 'failure'),
        max_tries=envvar_get_int("MAX_PR_TEST_POLL_TRIES", MAX_PR_TEST_TRIES_DEFAULT),
        initial_wait=envvar_get_int("PR_TEST_INITIAL_WAIT_INTERVAL", PR_TEST_INITIAL_WAIT_INTERVAL_DEFAULT),
        interval=envvar_get_int("PR_TEST_POLL_INTERVAL", PR_TEST_POLL_INTERVAL_DEFAULT),
        jitter=None,
        on_backoff=_backoff_handler
    )
    def _poll_commit(self, sha):
        """
        Poll whether the passed commit has passed all its tests.
        Ensures there is at least one status update so that
        commits whose tests haven't started yet are not valid.

        Arguments:
            sha (str): The SHA of which to get the status.

        Returns:
            bool: true when the combined state equals 'success'
        """
        commit_status = self.get_commit_combined_statuses(sha)

        # Ensure that at least one status update exists to guard against commits whose tests haven't started yet.
        if len(commit_status.statuses) < 1 or commit_status.state is None:
            return 'not_started'

        return commit_status.state.lower()

    def poll_pull_request_test_status(self, pr_number):
        """
        Given a PR number, poll the combined status of the PR's tests.

        Arguments:
            pr_number (int): Number of PR to check.

        Returns:
            True if all tests have passed successfully, else False.

        Raises:
            github.GithubException.GithubException: If the response fails.
            github.GithubException.UnknownObjectException: If the branch does not exist
        """
        commit_sha = self.get_head_commit_from_pull_request(pr_number)
        return self.poll_for_commit_successful(commit_sha)

    def poll_for_commit_successful(self, sha):
        """
        Poll whether the passed commit has passed all its tests.

        Arguments:
            sha (str): The SHA of which to get the status.

        Returns:
            True when the commit's combined state equals 'success', else False.
        """
        return self._poll_commit(sha) == 'success'

    def is_branch_base_of_pull_request(self, pr_number, branch_name):
        """
        Check if the PR is against the specified branch,
        i.e. if the base of the PR is the specified branch.

        Arguments:
            pr_number (int): Number of PR to check.
            branch_name (str): Name of branch to check.

        Returns:
            True if PR is opened against the branch, else False.

        Raises:
            github.GithubException.GithubException: If the response fails.
            github.GithubException.UnknownObjectException: If the branch does not exist
        """
        pull_request = self.get_pull_request(pr_number)
        repo_branch_name = '{}:{}'.format(self.org, branch_name)
        return pull_request.base.label == repo_branch_name

    def get_commits_by_branch(self, branch):
        """
        Calls GitHub's 'commits' endpoint for master.
        See
        https://developer.github.com/v3/repos/commits/#list-commits-on-a-repository

        Arguments:
            branch (str): branch to search for commits.

        Returns:
            github.PaginatedList.PaginatedList: of github.GitCommit.GitCommit

        Raises:
            github.GithubException: If the response fails validation.
        """
        branch = self.github_repo.get_branch(branch)
        return self.github_repo.get_commits(branch.commit.sha)

    def delete_branch(self, branch_name):
        """
        Call GitHub's delete ref (branch) API

        Args:
            branch_name (str): The name of the branch to delete

        Raises:
            github.GithubException.GithubException: If the response fails.
            github.GithubException.UnknownObjectException: If the branch does not exist
        """
        ref = self.github_repo.get_git_ref(
            ref='heads/{ref}'.format(ref=branch_name)
        )
        ref.delete()

    def create_branch(self, branch_name, sha):
        """
        Calls GitHub's create ref (branch) API

        Arguments:
            branch_name (str): The name of the branch to create
            sha (str): The commit to base the branch off of

        Returns:
            github.GitRef.GitRef

        Raises:
            github.GithubException.GithubException: If the branch isn't created/already exists.
            github.GithubException.UnknownObjectException: if the branch can not be fetched after creation
        """
        return self.github_repo.create_git_ref(
            ref='refs/heads/{}'.format(branch_name),
            sha=sha
        )

    def create_pull_request(
            self,
            head,
            base='release',
            title='',
            body=''):
        """
        Creates a new pull request from a branch

        Arguments:
            head (str): The name of the branch to create the PR from
            base (str): The Branch the PR will be merged in to
            title (str): Title of the pull request
            body (str): Text body of the pull request

        Returns:
            github.PullRequest.PullRequest

        Raises:
            github.GithubException.GithubException:

        """
        return self.github_repo.create_pull(
            title=title,
            body=body,
            head=head,
            base=base
        )

    def get_pull_request(self, pr_number):
        """
        Given a PR number, return the PR object.

        Arguments:
            pr_number (int): Number of PR to get.

        Returns:
            github.PullRequest.PullRequest

        Raises:
            github.GithubException.GithubException: If the response fails.
            github.GithubException.UnknownObjectException: If the PR ID does not exist
        """
        return self.github_repo.get_pull(pr_number)

    def merge_pull_request(self, pr_number):
        """
        Given a PR number, merge the pull request (if possible).

        Arguments:
            pr_number (int): Number of PR to merge.

        Raises:
            github.GithubException.GithubException: If the PR merge fails.
            github.GithubException.UnknownObjectException: If the PR ID does not exist.
        """
        pull_request = self.get_pull_request(pr_number)
        pull_request.merge()

    def create_tag(
            self,
            sha,
            tag_name,
            message='',
            tag_type='commit'):
        """
        Creates a tag associated with the sha provided

        Arguments:
            sha (str): The commit we references by the newly created tag
            tag_name (str): The name of the tag
            message (str): The optional description of the tag
            tag_type (str): The type of the tag. Could be 'tree' or 'blob'. Default is 'commit'.

        Returns:
            github.GitTag.GitTag

        Raises:
            github.GithubException.GithubException:
        """
        tag_user = self.user()
        tagger = InputGitAuthor(
            name=tag_user.name or DEFAULT_TAG_USERNAME,
            # GitHub users without a public email address will use a default address.
            email=tag_user.email or DEFAULT_TAG_EMAIL_ADDRESS,
            date=datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')
        )

        created_tag = self.github_repo.create_git_tag(
            tag=tag_name,
            message=message,
            object=sha,
            type=tag_type,
            tagger=tagger
        )

        try:
            # We need to create a reference based on the tag
            self.github_repo.create_git_ref(ref='refs/tags/{}'.format(tag_name), sha=sha)
        except GithubException as exc:
            # Upon trying to create a tag with a tag name that already exists,
            # an "Unprocessable Entity" error with a status code of 422 is returned
            # with a message of 'Reference already exists'.
            # https://developer.github.com/v3/#client-errors
            if exc.status != 422:
                raise
            # Tag is already created. Verify it's on the correct hash.
            existing_tag = self.github_repo.get_git_ref('tags/{}'.format(tag_name))
            if existing_tag.object.sha != sha:
                # The tag is already created and pointed to a different SHA than requested.
                raise GitTagMismatchError(
                    "Tag '{}' exists but points to SHA {} instead of requested SHA {}.".format(
                        tag_name, existing_tag.object.sha, sha
                    )
                )
        return created_tag

    def have_branches_diverged(self, base_branch, compare_branch):
        """
        Checks to see if all the commits that are in the compare_branch are already in the base_branch.

        Arguments:
            base_branch (str): Branch to use as a base when comparing.
            compare_branch (str): Branch to compare against base to see if it contains commits the base does not.

        Returns:
            bool: False if all commits in the compare_branch are already in the base_branch.
                  True if the compare_branch contains commits which the base_branch does not.

        Raises:
            github.GithubException.GithubException: If the call fails.
            github.GithubException.UnknownObjectException: If either branch does not exist.
        """
        return self.github_repo.compare(
            base='refs/heads/{}'.format(base_branch),
            head='refs/heads/{}'.format(compare_branch)
        ).status == 'diverged'

    def is_commit_successful(self, sha):
        """
        Returns whether the passed commit has passed all its tests.
        Ensures there is at least one status update so that
        commits whose tests haven't started yet are not valid.

        Arguments:
            sha (str): The SHA of which to get the status.

        Returns:
            bool: true when the combined state equals 'success'
        """
        commit_status = self.get_commit_combined_statuses(sha)

        # Determine if the commit has passed all checks
        if len(commit_status.statuses) < 1 or commit_status.state is None:
            return False

        return commit_status.state.lower() == 'success'

    def most_recent_good_commit(self, branch):
        """
        Returns the most recent commit on master that has passed the tests

        Arguments:
            branch (str): branch name to check for valid commits

        Returns:
            github.GitCommit.GitCommit

        Raises:
            NoValidCommitsError: When no commit is found

        """
        commits = self.get_commits_by_branch(branch)

        result = None
        for commit in commits:
            if self.is_commit_successful(commit.sha):
                result = commit
                return result

        # no result
        raise NoValidCommitsError()

    def get_pr_range(self, start_sha, end_sha):
        """
        Given a start SHA and an end SHA, returns a list of PRs between the two,
        excluding the start SHA and including the end SHA.

        This has been done in the past by parsing PR numbers out of merge commit
        messages. However, merge commits are becoming less common on GitHub with
        the advent of new PR merge strategies (i.e., squash merge, rebase merge).
        If you merge a PR using either squash or rebase merging, there will be no
        merge commit corresponding to your PR on master. The merge commit message
        parsing approach will subsequently fail to locate your PR.

        The GitHub Search API helps us address this by allowing us to search issues
        by SHA. Note that the GitHub Search API has custom rate limit rules (30 RPM).
        For more, see https://developer.github.com/v3/search.

        Arguments:
            start_sha (str): SHA from which to begin the PR search, exclusive.
            end_sha (str): SHA at which to conclude the PR search, inclusive.

        Returns:
            list: of github.PullRequest.PullRequest
        """
        # The Search API limits search queries to 256 characters. Untrimmed SHA1s
        # are 40 characters long. To avoid exceeding the rate and search query size
        # limits, we can batch SHAs in our searches. Reserving 56 characters for
        # qualifiers (i.e., type, base, user, repo) leaves us with 200 characters.
        # As with all other terms in the query, the batched SHAs need to be separated
        # by a character of whitespace. A batch size of 18 10-character SHAs requires
        # 17 characters of whitespace, for a total of 18*10 + 17 = 197 characters.
        # We'd need to search for >540 commits in a minute to exceed the rate limit.
        sha_length = int(os.environ.get('SHA_LENGTH', 10))
        batch_size = int(os.environ.get('BATCH_SIZE', 18))

        def batch(batchable):
            """
            Utility to facilitate batched iteration over a list.

            Arguments:
                batchable (list): The list to break into batches.

            Yields:
                list
            """
            length = len(batchable)
            for index in range(0, length, batch_size):
                yield batchable[index:index + batch_size]

        comparison = self.github_repo.compare(start_sha, end_sha)
        shas = [commit.sha[:sha_length] for commit in comparison.commits]

        issues = []
        for sha_batch in batch(shas):
            # For more about searching issues,
            # see https://help.github.com/articles/searching-issues.
            issues += self.github_connection.search_issues(
                ' '.join(sha_batch),
                type='pr',
                base='master',
                user=self.org,
                repo=self.repo,
            )

        pulls = {}
        for issue in issues:
            # Merge commits link back to the same PR as the actual commits merged
            # by that PR. We want to avoid listing the PR twice in this situation,
            # and also when a PR includes more than one commit.
            if not pulls.get(issue.number):
                pulls[issue.number] = self.github_repo.get_pull(issue.number)

        return list(pulls.values())

    def message_pull_request(self, pr_number, message, message_filter, force_message=False):
        """
        Messages a pull request. Will only message the PR if the message has not already been posted to the discussion

        Args:
            pr_number (int): the number of the pull request
            message (str): the message to post to the pull request
            message_filter (str): the message filter used to avoid duplicate messages
            force_message (bool): if set true the message will be posted without duplicate checking

        Returns:
            github.IssueComment.IssueComment

        Raises:
            github.GithubException.GithubException:
            InvalidPullRequestError: When the PR does not exist

        """
        def _not_duplicate(pr_messages, new_message):
            """
            Returns True if the comment does not exist on the PR
            Returns False if the comment exists on the PR

            Args:
                pr_messages (list<str>)
                new_message (str):
                existing_messages (str):

            Returns:
                bool

            """
            new_message = new_message.lower()
            result = False
            for comment in pr_messages:
                if new_message in comment.body.lower():
                    break
            else:
                result = True
            return result

        try:
            pull_request = self.github_repo.get_pull(pr_number)
        except UnknownObjectException:
            raise InvalidPullRequestError('PR #{pr_number} does not exist'.format(pr_number=pr_number))

        if force_message or _not_duplicate(pull_request.get_issue_comments(), message_filter):
            return pull_request.create_issue_comment(message)
        else:
            return None

    def message_pr_deployed_stage(self, pr_number, deploy_date=None, force_message=False):
        """
        Sends a message that this PRs commits have been deployed to the staging environment

        Args:
            pr_number (int): The number of the pull request
            force_message (bool): if set true the message will be posted without duplicate checking

        Returns:
            github.IssueComment.IssueComment

        """
        if deploy_date is None:
            deploy_date = default_expected_release_date()

        return self.message_pull_request(
            pr_number,
            (PR_ON_STAGE_BASE_MESSAGE + PR_ON_STAGE_DATE_MESSAGE).format(date=deploy_date),
            PR_ON_STAGE_BASE_MESSAGE,
            force_message,
        )

    def message_pr_deployed_prod(self, pr_number, force_message=False):
        """
        sends a message that this PRs commits have been deployed to the production environment

        Args:
            pr_number (int): The number of the pull request
            force_message (bool): if set true the message will be posted without duplicate checking

        Returns:
            github.IssueComment.IssueComment

        """
        return self.message_pull_request(
            pr_number,
            PR_ON_PROD_MESSAGE,
            PR_ON_PROD_MESSAGE,
            force_message
        )

    def message_pr_release_canceled(self, pr_number, force_message=False):
        """
        Sends a message that this PRs commits have not made it to production as the release was canceled

        Args:
            pr_number (int): The number of the pull request
            force_message (bool): if set true the message will be posted without duplicate checking

        Returns:
            github.IssueComment.IssueComment

        """
        return self.message_pull_request(
            pr_number,
            PR_RELEASE_CANCELED_MESSAGE,
            PR_RELEASE_CANCELED_MESSAGE,
            force_message
        )

    def has_been_merged(self, base, candidate):
        """
        Return whether ``candidate`` has been merged into ``base``.
        """
        try:
            comparison = self.github_repo.compare(base, candidate)
        except UnknownObjectException:
            return False

        return comparison.status in ('behind', 'identical')

    def find_approved_not_closed_prs(self, pr_base):
        """
        Yield all pull requests in the repo against ``pr_base`` that are approved and not closed.
        """
        query = "type:pr review:approved base:{} state:open state:merged".format(pr_base)
        for issue in self.github_connection.search_issues(query):
            yield self.github_repo.get_pull(issue.number)
