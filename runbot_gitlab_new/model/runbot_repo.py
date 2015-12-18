# -*- coding: utf-8 -*-
#
# Copyright (C) 2015 NDP Systèmes (<http://www.ndp-systemes.fr>).
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
import dateutil
import datetime
import time
import logging
import os
import re
import requests

import openerp.addons.runbot.runbot as runbot

from openerp import models, fields, api

_logger = logging.getLogger(__name__)


def get_gitlab_params(base):
    mo = re.search(r'([^/]+)(/(\d+))?/([^/]+)/([^/.]+)(\.git)?', base)
    if not mo:
        return
    domain = mo.group(1)
    port = mo.group(3)
    namespace = mo.group(4)
    name = mo.group(5)
    prefix = 'http' if base.startswith('http/') else 'https'
    if port:
        domain += ":%d" % int(port)
    domain = "%s://%s" % (prefix, domain)
    name = '%s/%s' % (namespace, name)
    return domain, name


class RunbotRepo(models.Model):
    _inherit = "runbot.repo"

    uses_gitlab = fields.Boolean(string='Use Gitlab')

    gitlab_project_id = fields.Char(
            string="Gitlab Project ID",
            compute='_compute_gitlab_project_id',
            store=True)

    mr_only = fields.Boolean(
        string="MR Only",
        default=False,
        help="Build only merge requests and skip regular branches")

    sticky_protected = fields.Boolean(
        string="Sticky for Protected Branches",
        default=True,
        help="Set all protected branches on the repository as sticky")

    ignore_duplicates = fields.Boolean(
        string="Ignore Duplicates",
        default=False,
        help="Build only commits that are not currently running in "
             "another branch. Sticky branches are always built.")

    @api.depends('name', 'uses_gitlab')
    def _compute_gitlab_project_id(self):
        """Computes the project_id of this repo in Gitlab
        """
        for repo in self:
            if repo.uses_gitlab:
                domain, name = get_gitlab_params(repo.base)
                url = "%s/api/v3/projects/%s" % (domain, name.replace("/", "%2F"))
                r = requests.get(url, params={'private_token': repo.token})
                r.raise_for_status()
                repo.gitlab_project_id = r.json().get('id')

    @api.one
    def github(self, url, payload=None, ignore_errors=False, delete=False):
        if not self.uses_gitlab:
            return super(RunbotRepo, self).github(url, payload, ignore_errors, delete)
        else:
            return {}

    @api.model
    def update_git(self, repo):
        if not repo.uses_gitlab:
            return super(RunbotRepo, self).update_git(repo)

        _logger.debug('repo %s updating branches', repo.name)

        build_obj = self.env['runbot.build']
        branch_obj = self.env['runbot.branch']

        if not os.path.isdir(os.path.join(repo.path)):
            os.makedirs(repo.path)
        if not os.path.isdir(os.path.join(repo.path, 'refs')):
            runbot.run(['git', 'clone', '--bare', repo.name, repo.path])

        # check for mode == hook
        fname_fetch_head = os.path.join(repo.path, 'FETCH_HEAD')
        if os.path.isfile(fname_fetch_head):
            fetch_time = os.path.getmtime(fname_fetch_head)
            if repo.mode == 'hook' and repo.hook_time and runbot.dt2time(repo.hook_time) < fetch_time:
                t0 = time.time()
                _logger.debug('repo %s skip hook fetch fetch_time: %ss ago hook_time: %ss ago',
                              repo.name, int(t0 - fetch_time), int(t0 - runbot.dt2time(repo.hook_time)))
                return

        repo.git(['gc', '--auto', '--prune=all'])
        repo.git(['fetch', '-p', 'origin', '+refs/heads/*:refs/heads/*'])
        repo.git(['fetch', '-p', 'origin', '+refs/merge-requests/*/head:refs/pull/*'])

        fields = ['refname', 'objectname', 'committerdate:iso8601', 'authorname', 'authoremail', 'subject',
                  'committername',
                  'committeremail']
        fmt = "%00".join(["%(" + field + ")" for field in fields])
        git_refs = repo.git(['for-each-ref', '--format', fmt, '--sort=-committerdate', 'refs/heads', 'refs/pull'])
        git_refs = git_refs.strip()

        refs = [[runbot.decode_utf(field) for field in line.split('\x00')] for line in git_refs.split('\n')]

        for name, sha, date, author, author_email, subject, committer, committer_email in refs:
            # create or get branch
            branches = branch_obj.search([('repo_id', '=', repo.id), ('name', '=', name)])
            if branches:
                branch = branches[0]
            else:
                _logger.debug('repo %s found new branch %s', repo.name, name)
                branch = branch_obj.create({'repo_id': repo.id, 'name': name})
            # skip build for old branches
            if dateutil.parser.parse(date[:19]) + datetime.timedelta(30) < datetime.datetime.now():
                continue
            # create build (and mark previous builds as skipped) if not found
            builds = build_obj.search([('branch_id', '=', branch.id), ('name', '=', sha)])
            if not builds:
                _logger.debug('repo %s branch %s new build found revno %s', branch.repo_id.name, branch.name, sha)
                build_info = {
                    'branch_id': branch.id,
                    'name': sha,
                    'author': author,
                    'author_email': author_email,
                    'committer': committer,
                    'committer_email': committer_email,
                    'subject': subject,
                    'date': dateutil.parser.parse(date[:19]),
                }

                if not branch.sticky:
                    skipped_builds = build_obj.search(
                            [('branch_id', '=', branch.id), ('state', '=', 'pending')],
                            order='sequence asc'
                    )
                    if skipped_builds:
                        skipped_builds.skip()
                        # new order keeps lowest skipped sequence
                        build_info['sequence'] = skipped_builds[0].sequence

                    if repo.ignore_duplicates:
                        duplicates = build_obj.search(
                                [('name', '=', sha), ('branch_id', '!=', branch.id),
                                 ('state', 'in', ['running'])])
                        if duplicates:
                            build_info['duplicate_id'] = duplicates[0].id
                            build_info['state'] = 'duplicate'

                new_build = build_obj.create(build_info)
                # Send info to gitlab that we take this into account
                new_build.github_status()

        # skip old builds (if their sequence number is too low, they will not ever be built)
        skippable_domain = [('repo_id', '=', repo.id), ('state', '=', 'pending')]
        icp = self.env['ir.config_parameter']
        running_max = int(icp.get_param('runbot.running_max', default=75))
        to_be_skipped_builds = build_obj.search(skippable_domain, order='sequence desc', offset=running_max)
        to_be_skipped_builds.skip()

        if repo.sticky_protected:
            # Put all protected branches as sticky
            domain, name = get_gitlab_params(repo.base)
            url = "%s/api/v3/projects/%s/repository/branches" % (domain, repo.gitlab_project_id)
            r = requests.get(url, params={'private_token': repo.token})
            r.raise_for_status()
            all_branches = r.json()
            protected_branche_names = [b['name'] for b in all_branches if b['protected']]

            sticky_protected_branches = branch_obj.search([
                ('repo_id', '=', repo.id),
                ('branch_name', 'in', protected_branche_names),
                ('sticky', '=', False),
            ])

            sticky_protected_branches.write({'sticky': True})

        if repo.mr_only:
            # Skip non-sticky non-merge proposal builds
            branches = branch_obj.search([
                ('sticky', '=', False),
                ('repo_id', '=', repo.id),
                '!', ('name', '=like', "refs/pull/%")
            ])
            for build in self.env['runbot.build'].search([
                    ('branch_id', 'in', branches.ids)]):
                build.skip()
