import argparse
import base64
import email
import email.mime.text
import fnmatch
import httplib
import json
import logging
import os
from pandas import Timestamp
import smtplib
import sys
from textwrap import wrap, dedent
import time


log = logging


def iterpage():
    page = 1
    while True:
        yield page
        page += 1


def send_email(email_from, email_to, notification):
    notification_type = notification['subject']['type']
    title = notification['subject']['title']
    url = notification['subject']['url'].replace('api.', '', 1).\
        replace('/repos/', '/', 1)
    if notification_type == 'PullRequest':
        url = url.replace('/pulls/', '/pull/', 1)
    elif notification_type == 'Issue':
        pass
    elif notification_type == 'Commit':
        url = url.replace('/commits/', '/commit/', 1)
    else:
        log.error('Unknown notification type for emailing: {}'.format(
            notification_type))
        return

    body = dedent(u"""
        You have been unsubscribed from the {1} with the subject
        "{0}".

        Visit {2} to resubscribe.
       """).lstrip().format(
           title, notification_type.lower(), url)

    msg = email.mime.text.MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = u'Unsubscribed from "{}"'.format(title)
    msg['To'] = email_to

    smtp = smtplib.SMTP('localhost')
    smtp.sendmail(email_from, [email_to], msg.as_string())
    smtp.quit()


def repo_pattern_match(notification, pattern):
    name = notification['repository'][
        'full_name' if '/' in pattern else 'name']
    return fnmatch.fnmatchcase(name, pattern)


def repo_list_match(notification, patterns):
    return any(repo_pattern_match(notification, p) for p in patterns)


def gunsub(github_user, github_password,
           github_include_repos=[], github_exclude_repos=[],
           since=None, dryrun=False, email_from=None,
           email_to=None):

    def req(uri, method='GET', body=None, headers={}):
        auth = base64.encodestring('{0}:{1}'
                                   .format(github_user, github_password))
        headers = headers.copy()
        headers.update({
            'Authorization': 'Basic '+auth.strip(),
            'User-Agent': 'gunsub/0.2 (https://github.com/jpetazzo/gunsub)'
        })
        c = httplib.HTTPSConnection('api.github.com')
        log.debug('{0} {1}'.format(method, uri))
        if body is not None:
            body = json.dumps(body)
            log.debug('JSONified body: {0!r} ({1} bytes)'
                      .format(body, len(body)))
        c.request(method, uri, body, headers)
        r = c.getresponse()
        log.debug('x-ratelimit-remaining: {0}'
                  .format(r.getheader('x-ratelimit-remaining')))
        msg = r.read()
        result = json.loads(msg)
        return result, msg

    since_qs = ''
    if since is not None:
        since_qs = '&since=' + time.strftime('%FT%TZ', time.gmtime(since))
    else:
        log.info('Scanning all notifications (this could take a while)...')

    count = 0
    for page in iterpage():
        notifications, msg = req('/notifications?all=true&page={0}{1}'
                                 .format(page, since_qs))
        if not notifications:
            break
        for notification in notifications:
            # Check inclusion/exclusion rules.
            try:
                # Releases don't have subscribe or unsubscribe buttons on the
                # Github web site, so don't mess with them.
                if notification['subject']['type'] == 'Release':
                    continue
            except TypeError:
                # I once got "TypeError: string indices must be integers" from
                # the line of code above, which I couldn't debug because the
                # resulting log message didn't say what was actually in the
                # notification, so logging it here for the next time it
                # happens.
                log.error('Unexpected notification contents: {} '
                          '(raw response: {})'.format(notification, msg))
                raise
            if github_include_repos and \
               not repo_list_match(notification, github_include_repos):
                continue
            if repo_list_match(notification, github_exclude_repos):
                continue
            # If we were initially subscribed because mentioned/created/etc,
            # don't touch the subscription information.
            if notification['reason'] != 'subscribed':
                continue
            # Now check if we explicitly subscribed to this thing.
            subscription_uri = ('/notifications/threads/{0}/subscription'
                                .format(notification['id']))
            subscription, msg = req(subscription_uri)
            # If no subscription is found, then that subscription was implicit
            if 'url' not in subscription:
                # ... And we therefore unsubscribe from further notifications
                subject_url = notification['subject']['url']
                log.info('Unsubscribing from {0}...'.format(subject_url))
                if not args.dryrun:
                    result, msg = req(subscription_uri, 'PUT',
                                      dict(subscribed=False, ignored=True))
                    if 'subscribed' not in result:
                        log.warning('When unsubscribing from {0}, I got this: '
                                    '{1!r} and it does not contain {2!r}.'
                                    .format(subject_url, result, 'subscribed'))
                if email_from:
                    send_email(email_from, email_to, notification)
                count += 1
    log.info('Done; had to go through {0} page(s) of notifications, '
             'and unsubscribed from {1} thread(s).'
             .format(page, count))


def wrap_paragraphs(paragraphs):
    return '\n\n'.join('\n'.join(wrap(paragraph))
                       for paragraph in paragraphs.split('\n'))


def parse_args():
    description = wrap_paragraphs(
        'Unsubscribe automatically from Github threads after '
        'the initial thread notification')
    epilog = wrap_paragraphs(
        'Repository include and exclude names can optionally start with '
        '"owner/" or use shell wildcards.\n'
        'To read more about gunsub, check its project page on Github: '
        'http://github.com/jpetazzo/gunsub.')
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=description, epilog=epilog)

    parser.add_argument('--debug', action='store_true',
                        help='Enable debug logging')
    parser.add_argument('--dryrun', action='store_true',
                        help='Say what would be done without doing it')
    user_default = os.environ.get('GITHUB_USER', None)
    parser.add_argument('--user', action='store', default=user_default,
                        required=not user_default,
                        help='Github username (or set $GITHUB_USER')
    password_default = os.environ.get('GITHUB_PASSWORD', None)
    parser.add_argument('--password', action='store', default=password_default,
                        required=not password_default,
                        help='Github password (or set $GITHUB_PASSWORD')
    parser.add_argument('--interval', action='store', type=int,
                        default=int(os.environ.get('GITHUB_POLL_INTERVAL', 0)),
                        help='Poll interval in seconds for continuous '
                        'operation (or set $GITHUB_POLL_INTERVAL)')
    include_default = os.environ.get('GITHUB_INCLUDE_REPOS', '').split(',')
    parser.add_argument('--include', action='append', default=include_default,
                        help='List of repositories to include (or set '
                        '$GITHUB_INCLUDE_REPOS to comma-separated list)')
    exclude_default = os.environ.get('GITHUB_EXCLUDE_REPOS', '').split(',')
    parser.add_argument('--exclude', action='append', default=exclude_default,
                        help='List of repositories to exclude (or set '
                        '$GITHUB_EXCLUDE_REPOS to comma-separated list)')
    parser.add_argument('--since', metavar='TIME-STRING', action='store',
                        type=Timestamp, help='Examine notifications starting '
                        'at the specified time')
    parser.add_argument('--email-from', metavar='ADDRESS', action='store',
                        help='Email address from which to send notifications')
    parser.add_argument('--email-to', metavar='ADDRESS', action='store',
                        help='Email address to notify about unsubscribes')

    args = parser.parse_args()

    if int(not not args.email_from) + int(not not args.email_to) == 1:
        sys.exit('Must specify both --email-from and --email-to')

    return args


def main(args):
    github_user = args.user
    github_password = args.password
    github_include_repos = args.include
    github_exclude_repos = args.exclude
    interval = args.interval
    interval = interval and int(interval)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    since = None

    state_file = './next-since'

    if args.since:
        since = int(args.since.strftime('%s'))
    else:
        # Read application state
        if os.path.isfile(state_file):
            with open(state_file) as next_since_file:
                since = float(next_since_file.read().split()[0])
                log.info('Parsing events since {0}, {1}'.format(
                    time.strftime('%FT%TZ', time.gmtime(since)), since))

    while True:
        next_since = time.time()
        try:
            gunsub(github_user, github_password,
                   github_include_repos, github_exclude_repos,
                   since, dryrun=args.dryrun, email_from=args.email_from,
                   email_to=args.email_to)
            if not args.dryrun:
                with open(state_file, 'w') as next_since_file:
                    next_since_file.write(str(next_since))
            since = next_since
        except:
            log.exception('Error in main loop!')
        if not interval:
            break
        log.debug('Sleeping for {0} seconds.'.format(interval))
        time.sleep(interval)


if __name__ == '__main__':
    args = parse_args()
    main(args)
