#!/usr/bin/python
""" Generate and optionally email information about pending github pull requests"""

import json
import re
import os
from collections import namedtuple
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
import ConfigParser
import sys
import requests

Summary = namedtuple('Summary', 'repo, id, url, ref, title, refs, owner, creator, age, lastmod')

class PullRequestReporter(object):
    """ Main class for generating pull request reports. """

    def __init__(self):

        def _read_config(config, section, name, default):
            if config.has_option(section, name):
                return config.get(section, name)
            else:
                return default

        def _read_config_bool(config, section, name, default):
            if config.has_option(section, name):
                return config.getboolean(section, name)
            else:
                return default

        config = ConfigParser.ConfigParser()
        config.optionxform = str  # case sensitive

        if not os.path.exists('pulls.ini'):
            raise RuntimeError("pulls.ini not found")
        config.read('pulls.ini')

        self.verbose = _read_config_bool(config, "options", "verbose", False)
        self.email_user = _read_config(config, "email", "user", None)
        self.email_password = _read_config(config, "email", "password", None)
        self.email_server = _read_config(config, "email", "SMTP", None)
        self.email_from = _read_config(config, "email", "from", "GitHubPullBot")
        self.github_user = _read_config(config, "GitHub", "user", None)
        self.github_password = _read_config(config, "GitHub", "password", None)

        if not config.has_section('repositories'):
            print "pulls.ini has no repositories section"
            sys.exit(1)

        self.repositories = {}
        for repo in config.items('repositories'):
            self.repositories[repo[0]] = repo[1]

        # Can restrict to specified PRs (mostly for debugging purposes
        self.pulls = {}
        if config.has_section('pulls'):
            for pull in config.items('pulls'):
                self.pulls[int(pull[0])] = pull[1]

        self.emails = {}
        if config.has_section('emails'):
            for user in config.items('emails'):
                self.emails[user[0]] = user[1]
        else:
            print "pulls.ini has no emails section"
            sys.exit(1)

        self.jira_regex = _read_config(config, "jira", "regex", None)
        self.jira_url = _read_config(config, "jira", "url", None)

        self.session = requests.Session()
        if self.github_user and self.github_password:
            # Unauthenticated will still work, but will hit github throttle limits
            # pretty quickly
            self.session.auth = (self.github_user, self.github_password)

    def _fetch_email(self, user_id):
        if user_id in self.emails:
            return self.emails[user_id]
        else:
            # Too many false positives on the mentions to want to lookup users in
            # github (and it's slow)
            return None

    def _make_jira_link(self, instring):
        if self.jira_regex and self.jira_url:
            match = re.search(self.jira_regex, instring)
            if match:
                return self.jira_url + '/' + match.group(0)
        return None

    def _output_row(self, row):
        summary_text = '%-16s %-5d %-5d %-16s %-60s %s\n' % (
            row.owner, row.age.days, row.lastmod.days, row.ref, row.url, row.title)
        summary_html = """ \
            <tr><td>%s</td><td><a href="%s" target="_blank">#%d</a></td>
            <td>%s</td>""" % (row.repo, row.url, row.id, row.ref)
        if self._make_jira_link(row.title):
            summary_html += '<td class="leftaligned"><a href="%s" target="_blank">%s</a></td> ' % (
                self._make_jira_link(row.title), row.title)
        else:
            summary_html += '<td class="leftaligned">%s</td> ' % (row.title)
        if self._fetch_email(row.owner):
            summary_html += '<td><a href="mailto:%s">%s</a></td>' % (
                self._fetch_email(row.owner), row.owner)
        else:
            summary_html += '<td>%s</td>' % (row.owner)
        if self._fetch_email(row.creator):
            summary_html += '<td><a href="mailto:%s">%s</a></td>' % (
                self._fetch_email(row.creator), row.creator)
        else:
            summary_html += '<td>%s</td>' % (row.creator)
        summary_html += '<td>%d</td><td>%d</td></tr>\n' % (
            row.age.days, row.lastmod.days)
        return (summary_text, summary_html)

    def _fetch_events(self, github_repo, pull):
        all_events = []
        page = 1
        while True:
            events_url = "https://api.github.com/repos/%s/issues/%d/events?page=%d" % (
                github_repo, pull['number'], page)
            retcode = self.session.get(events_url, \
                headers={'Accept':'application/vnd.github.black-cat-preview+json'})
            events = json.loads(retcode.text or retcode.content)
            if self.verbose:
                print "Processing %d events" % len(events)
            for event in events:
                actor = event['actor']['login']
                action = event['event']
                event_time = datetime.strptime(event['created_at'], "%Y-%m-%dT%H:%M:%SZ")
                all_events.append((actor, action, event_time))
            if len(events) < 30:
                break
            page += 1
        return all_events

    def _fetch_reviews(self, github_repo, pull):
        all_events = []
        page = 1
        while True:
            reviews_url = "https://api.github.com/repos/%s/pulls/%d/reviews?page=%d" % (
                github_repo, pull['number'], page)
            retcode = self.session.get(reviews_url, \
                headers={'Accept':'application/vnd.github.black-cat-preview+json'})
            reviews = json.loads(retcode.text or retcode.content)
            if self.verbose:
                print "Processing %d reviews" % len(reviews)
            cpage = 1
            for review in reviews:
                body = review['body']
                mentions = re.findall(r"@(\w+)", body)
                mention_time = datetime.strptime(review['submitted_at'], "%Y-%m-%dT%H:%M:%SZ")
                for mention in mentions:
                    all_events.append((mention, "mentioned", mention_time))
                # Comments on reviews can also have mentions in...
                review_id = review['id']
                comments_url = \
                    "https://api.github.com/repos/%s/pulls/%d/reviews/%d/comments?page=%d" % \
                    (github_repo, pull['number'], review_id, cpage)
                retcode = self.session.get(comments_url, \
                    headers={'Accept':'application/vnd.github.black-cat-preview+json'})
                comments = json.loads(retcode.text or retcode.content)
                if self.verbose:
                    print "Processing %d review comments" % len(comments)
                for comment in comments:
                    body = comment['body']
                    mentions = re.findall(r"@(\w+)", body)
                    comment_time = datetime.strptime(comment['created_at'], "%Y-%m-%dT%H:%M:%SZ")
                    if 'modified_at' in comment:
                        comment_time = datetime.strptime(review.get('modified_at'),
                                                         "%Y-%m-%dT%H:%M:%SZ")
                    for mention in mentions:
                        all_events.append((mention, "mentioned", comment_time))
                if len(comments) < 30:
                    break
                cpage += 1
            if len(reviews) < 30:
                break
            page += 1
        return all_events

    def _fetch_repo(self, repo_id, github_repo, summaries):
        page = 1
        while True:
            retcode = self.session.get('https://api.github.com/repos/%s/pulls?page=%d' % (github_repo, page))
            if retcode.ok:
                pulls = json.loads(retcode.text or retcode.content)
                now = datetime.now()
                # loop through pull requests, gathering summary info into summaries
                if self.verbose:
                    print "Processing %d pull requests" % len(pulls)
                for pull in pulls:
                    if self.pulls and not pull['number'] in self.pulls:
                        continue
                    if self.verbose:
                        print "Processing pull request %s" % pull['number']
                    creator = pull['user']['login']
                    user_id_list = [creator]
                    created = datetime.strptime(pull['created_at'], "%Y-%m-%dT%H:%M:%SZ")
                    if 'modified_at' in pull:
                        lastmodified = datetime.strptime(
                            pull.get('modified_at'), "%Y-%m-%dT%H:%M:%SZ")
                    else:
                        lastmodified = created
                    events = self._fetch_events(github_repo, pull) + \
                            self._fetch_reviews(github_repo, pull)
                    events.sort(key=lambda x: x[2])  # Sort by event time
                    owner = None
                    last_mentioned = None
                    lastevent = created
                    for actor, action, lastevent in events:
                        if action == "unassigned":
                            owner = None
                        elif action == "assigned":
                            owner = actor
                        elif action == "mentioned":
                            last_mentioned = actor
                        if self.verbose:
                            print "Processing event %s %s" % (action, actor)
                        if not actor in user_id_list:
                            user_id_list.append(actor)
                    owner = owner or last_mentioned or creator
                    user_id_list.remove(owner)
                    if lastevent > lastmodified:
                        lastmodified = lastevent
                    if self.verbose:
                        print "Owner %s" % owner
                    summaries.append(Summary(repo=repo_id,
                                            id=pull['number'], url=pull['html_url'],
                                            ref=pull['base']['ref'], title=pull['title'],
                                            refs=user_id_list, owner=owner,
                                            creator=creator, age=now - created,
                                            lastmod=now - lastmodified))
                if len(pulls)< 30:
                    break
                page += 1

    def _generate_one(self, gmail, email, summaries):
        summary_text = ''
        summary_html = ''
        owner_text = ''
        owner_html = ''
        creator_text = ''
        creator_html = ''
        for row in sorted(summaries, key=lambda x: x.ref):
            text, html = self._output_row(row)
            if row.owner == email:
                owner_text += text
                owner_html += html
            elif row.creator == email:
                creator_text += text
                creator_html += html
            summary_text += text
            summary_html += html

        msg = MIMEMultipart('alternative')
        msg['Subject'] = 'GitHub pull requests summary'
        msg['From'] = self.email_from
        msg['To'] = self.emails[email]

        # Generate plaintext email contents

        text = ""
        if owner_text:
            text += """\
The following pull requests appear to be waiting for your attention:
%-16s %-5s %-5s %-16s %-60s %s
%s
""" % ('Owner', 'Age', 'Idle', 'Target', 'URL', 'Title', owner_text)
        if creator_text:
            text += """\
The following pull requests created by you appear to be awaiting attention from someone else:
%-16s %-5s %-5s %-16s %-60s %s
%s
""" % ('Owner', 'Age', 'Idle', 'Target', 'URL', 'Title', creator_text)
        text += """\
The full list of pull requests is as follows:
%-16s %-5s %-5s %-16s %-60s %s
%s
""" % ('Owner', 'Age', 'Idle', 'Target', 'URL', 'Title', summary_text)

    # Generate HTML email contents

        html = u"""\
<html>
  <head>
  <style>
  table { 
    color: #333; /* Lighten up font color */
    font-family: Helvetica, Arial, sans-serif; /* Nicer font */
    width: 100%%;
    border-collapse: 
    collapse; border-spacing: 0; 
  }
  td, th { border: 1px solid #CCC; height: 30px; } /* Make cells a bit taller */
  th {
    background: #F3F3F3; /* Light grey background */
    font-weight: bold; /* Make sure they're bold */
  }

  td {
    background: #FAFAFA; /* Lighter grey background */
    text-align: center;
    padding-left: 10px;
    padding-right: 10px;
  }
  .leftaligned {
    text-align: left;
  }

  </style>   
  </head>
  <body>
    <p>The following pull requests appear to be waiting for your atttention:</p>
    <p>
    <table>
      <tr><th>Repo</th><th>Id</th><th>Target</th><th>Title</th><th>Owner</th><th>Creator</th><th>Age</th><th>Stalled for</th></tr>
      %s
    </table>
    </p>
    <p>The following pull requests created by you appear to be awaiting attention from someone else:</p>
    <p>
    <table>
      <tr><th>Repo</th><th>Id</th><th>Target</th><th>Title</th><th>Owner</th><th>Creator</th><th>Age</th><th>Stalled for</th></tr>
      %s
    </table>
    </p>
    <p>The full list of pull requests is as follows:</p>
    <p>
    <table>
      <tr><th>Repo</th><th>Id</th><th>Target</th><th>Title</th><th>Owner</th><th>Creator</th><th>Age</th><th>Stalled for</th></tr>
      %s
    </table>
    </p>
  </body>
</html>""" % (owner_html, creator_html, summary_html)

        # And send the email

        text = text.encode('ascii', 'ignore').decode('ascii')
        html = html.encode('ascii', 'ignore').decode('ascii')
        if gmail:
            msg.attach(MIMEText(text, 'plain'))
            msg.attach(MIMEText(html, 'html'))
            gmail.sendmail(msg['From'], msg['To'], msg.as_string())
        else:
            print html

    def generate_all(self, for_user=None):
        """ Generate and optionally email reports """
        summaries = []
        for repo in self.repositories:
            self._fetch_repo(repo, self.repositories[repo], summaries)

        # Now send out emails

        if self.email_user and self.email_password and not for_user:
            gmail = smtplib.SMTP(self.email_server)
            gmail.starttls()
            gmail.login(self.email_user, self.email_password)
        else:
            gmail = None

        # One email per person in the dictionary
        if for_user:
            if not for_user in self.emails:
                raise RuntimeError("User %s not found in emails section" % for_user)
            generate_for = [for_user]
        else:
            generate_for = self.emails.keys()
        for email in generate_for:
            self._generate_one(gmail, email, summaries)

        # All done

        if gmail:
            gmail.quit()

def _main():
    for_user = None
    if len(sys.argv) > 1:
        for_user = sys.argv[1]
    reporter = PullRequestReporter()
    reporter.generate_all(for_user)

if __name__ == '__main__':
    _main()
